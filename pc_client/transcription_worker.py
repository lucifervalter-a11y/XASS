"""Opt-in song transcription worker for the XASS Windows client.

Setting «Использовать этот ПК для расшифровки текста» (config key
``transcription_enabled``, default off). When on, the client:

1. reports capabilities (GPU + VRAM, CPU cores, RAM) and current CPU/GPU load
   to ``/agent/transcription/poll`` with the same per-PC agent key used for
   heartbeats;
2. on first enable installs a separate runtime (Python venv with torch,
   demucs, faster-whisper) under ``%LOCALAPPDATA%\\XASS\\transcription`` and
   pre-downloads htdemucs + Whisper large-v3; nothing heavy ships with XASS;
3. when the server leases a job, downloads the track via the short-lived
   agent-bound stream ticket, runs ``transcribe_runner.py`` at BELOW_NORMAL
   (or IDLE) priority with limited threads, renews the lease every minute and
   posts the timed lines back. Any failure is reported so the server can
   reassign the job to the next PC.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable
import zipfile

import psutil

try:
    from network_client import create_http_client
except ModuleNotFoundError:
    from pc_client.network_client import create_http_client

CONFIG_KEY = "transcription_enabled"
IS_WINDOWS = os.name == "nt"
SETTING_LABEL = "Использовать этот ПК для расшифровки текста"
POLL_SEC = 20
DISABLED_POLL_SEC = 300
RENEW_SEC = 60
MAX_AUDIO_BYTES = 256 * 1024 * 1024
BUSY_PERCENT = 70.0
# torch 2.5.1 cu121 bundles cuDNN 9 / cuBLAS 12, which CTranslate2 4.x
# (faster-whisper) needs; wheels exist for Python 3.10-3.12. demucs 4.1 does
# its audio I/O without torchaudio.
TORCH_PACKAGES = ["torch==2.5.1", "torchaudio==2.5.1"]  # torchaudio pinned so demucs never pulls another torch
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu121"
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
RUNTIME_PACKAGES = ["demucs==4.1.0", "faster-whisper==1.2.1"]
# Official python.org Windows embeddable build; SHA-256 computed from the file
# whose MD5 (6d9aa08531d48fcc261ba667e2df17c4) matches python.org's release page.
PYTHON_VERSION = "3.11.9"
PYTHON_EMBED_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
PYTHON_EMBED_SHA256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"
PIP_WHEEL_URL = ("https://files.pythonhosted.org/packages/b7/3f/945ef7ab14dc4f9d7f40288d2df998d1837ee0888ec3659c813487572faa/"
                 "pip-25.2-py3-none-any.whl")
PIP_WHEEL_SHA256 = "6d67a2b4e7f14d8b31b8b52648866fa717f45a1eb70e83002f4331d07e953717"  # PyPI digest
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040
CREATE_NO_WINDOW = 0x08000000
EXTENSIONS = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac",
              "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/aac": ".aac"}


class TranscriptionError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason[:200]


class LeaseLost(Exception):
    pass


# ------------------------------------------------------------------ hardware

def _run_quiet(args: list[str], timeout: float = 5.0) -> str:
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def gpu_info(run: Callable[[list[str]], str] = _run_quiet) -> dict:
    """NVIDIA GPU via nvidia-smi (ships with the driver); no torch needed."""
    binary = "nvidia-smi"
    if run is _run_quiet:
        binary = nvidia_smi_path() or ""
        if not binary:
            return {"gpu": False, "gpu_name": "", "vram_mb": 0, "gpu_percent": None}
    output = run([binary, "--query-gpu=name,memory.total,utilization.gpu", "--format=csv,noheader,nounits"])
    best = None
    for row in output.splitlines():
        parts = [part.strip() for part in row.split(",")]
        if len(parts) < 3:
            continue
        try:
            item = {"gpu": True, "gpu_name": parts[0][:120], "vram_mb": int(float(parts[1])), "gpu_percent": float(parts[2])}
        except ValueError:
            continue
        if best is None or item["vram_mb"] > best["vram_mb"]:
            best = item
    return best or {"gpu": False, "gpu_name": "", "vram_mb": 0, "gpu_percent": None}


def capabilities(gpu: dict | None = None) -> dict:
    gpu = gpu if gpu is not None else gpu_info()
    return {"gpu": bool(gpu.get("gpu")), "gpu_name": gpu.get("gpu_name", ""), "vram_mb": int(gpu.get("vram_mb") or 0),
            "cpu_cores": int(psutil.cpu_count(logical=False) or psutil.cpu_count() or 1),
            "cpu_threads": int(psutil.cpu_count() or 1), "ram_mb": int(psutil.virtual_memory().total // (1024 * 1024))}


def current_load(gpu: dict | None = None, *, running: bool = False, interval: float = 1.0) -> dict:
    gpu = gpu if gpu is not None else gpu_info()
    cpu = float(psutil.cpu_percent(interval=interval))
    gpu_percent = gpu.get("gpu_percent")
    busy = running or cpu > BUSY_PERCENT or (gpu_percent is not None and float(gpu_percent) > BUSY_PERCENT)
    return {"cpu_percent": round(cpu, 1), "gpu_percent": None if gpu_percent is None else round(float(gpu_percent), 1), "busy": busy}


def thread_limit(caps: dict) -> int:
    """Leave most cores to the user: half the physical cores, at most 4."""
    return max(1, min(4, int(caps.get("cpu_cores") or 1) // 2))


# ------------------------------------------------------------------ runtime

# Peak use, not the final size: the cu121 wheel stays in the pip cache while it is
# unpacked, and the models download after that. A real install failed at 9 GB free.
CUDA_INSTALL_FREE_BYTES = 14 * 1024 ** 3


def torch_variant(gpu: dict | None, wmi_names: list[str] | None = None) -> str:
    """CUDA wheels (~2.5 GB) only for an NVIDIA GPU; everything else gets the CPU wheel."""
    if gpu and gpu.get("gpu"):
        return "cu121"
    if any("nvidia" in str(name).lower() for name in (wmi_names or [])):
        return "cu121"
    return "cpu"


def free_bytes(path: Path) -> int:
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return 0


def select_variant(gpu: dict | None, wmi_names: list[str] | None, free: int) -> str:
    """Keep the CPU wheel when the CUDA download and unpack do not fit."""
    variant = torch_variant(gpu, wmi_names)
    if variant == "cu121" and free < CUDA_INSTALL_FREE_BYTES:
        return "cpu"
    return variant


def torch_index(variant: str) -> str:
    return TORCH_CUDA_INDEX if variant == "cu121" else TORCH_CPU_INDEX


def install_plan(variant: str) -> list[tuple[str, str, float]]:
    """(stage, Russian label, weight) — weights follow the approximate download size."""
    cuda = variant == "cu121"
    return [
        ("python", "Python 3.11", 1.0),
        ("pip", "Установщик пакетов", 0.5),
        ("torch", "PyTorch (CUDA)" if cuda else "PyTorch (CPU)", 45.0 if cuda else 10.0),
        ("deps", "Demucs и Whisper", 8.0),
        ("models", "Модели htdemucs и Whisper large-v3", 45.0),
    ]


def overall_percent(plan: list[tuple[str, str, float]], stage: str, fraction: float) -> int:
    total = sum(weight for _, _, weight in plan) or 1.0
    done = 0.0
    for name, _, weight in plan:
        if name == stage:
            done += weight * max(0.0, min(1.0, fraction))
            break
        done += weight
    return max(0, min(100, int(done * 100 / total)))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def http_fetch(url: str, destination: Path, on_progress: Callable[[int, int], None]) -> None:
    """Plain HTTPS download with progress; the caller verifies the pinned SHA-256."""
    import httpx
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        done = 0
        with partial.open("wb") as stream:
            for chunk in response.iter_bytes(chunk_size=256 * 1024):
                stream.write(chunk)
                done += len(chunk)
                on_progress(done, total)
    os.replace(partial, destination)


_PIP_PROGRESS = re.compile(r"^Progress (\d+) of (\d+)")
_RUNNER_PROGRESS = re.compile(r"^PROGRESS (\w+) ([0-9.]+)")


class Runtime:
    """Private Python + packages + model cache under %LOCALAPPDATA%\\XASS\\transcription.

    Nothing is required from the user: the official python.org embeddable
    Python 3.11 (SHA-256 pinned) is unpacked per user without admin rights,
    pip is bootstrapped from its pinned wheel, then torch (CUDA only with an
    NVIDIA GPU), demucs, faster-whisper and both models are downloaded.
    """

    def __init__(self, data_root: Path, resource_root: Path, config: dict | None = None):
        config = config or {}
        self.root = Path(data_root) / "transcription"
        self.python_dir = self.root / "python"
        self.downloads = self.root / "downloads"
        self.models = self.root / "models"
        self.jobs = self.root / "jobs"
        self.marker = self.root / "ready.json"
        self.log_path = self.root / "install.log"
        self.runner = Path(resource_root) / "transcribe_runner.py"
        self.priority = IDLE_PRIORITY_CLASS if config.get("transcription_priority") == "idle" else BELOW_NORMAL_PRIORITY_CLASS

    @property
    def python(self) -> Path:
        return self.python_dir / ("python.exe" if IS_WINDOWS else "bin/python3")

    def ready(self) -> bool:
        return self.marker.is_file() and self.python.is_file() and self.runner.is_file()

    def variant(self) -> str:
        try:
            return str(json.loads(self.marker.read_text(encoding="utf-8")).get("variant") or "cpu")
        except (OSError, ValueError, AttributeError):
            return "cpu"

    def environment(self, threads: int) -> dict:
        env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "CT2_INTER_THREADS"):
            env[key] = str(threads)
        env.update(TORCH_HOME=str(self.models / "torch"), HF_HOME=str(self.models / "hf"),
                   HF_HUB_DISABLE_TELEMETRY="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1")
        return env

    def install_environment(self, threads: int) -> dict:
        """Keep the wheel cache and unpack directory on the same volume as the runtime."""
        env = self.environment(threads)
        cache = self.root / "pip-cache"
        tmp = self.root / "tmp"
        cache.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(parents=True, exist_ok=True)
        env["PIP_CACHE_DIR"] = str(cache)
        env["TEMP"] = str(tmp)
        env["TMP"] = str(tmp)
        return env

    def popen_kwargs(self) -> dict:
        if IS_WINDOWS:
            return {"creationflags": self.priority | CREATE_NO_WINDOW}
        return {"preexec_fn": lambda: os.nice(19 if self.priority == IDLE_PRIORITY_CLASS else 10)}

    def pip_bootstrap(self, wheel: Path) -> list[str]:
        """Offline self-install. argv[0] must not be named pip, or Windows pip exits."""
        code = (
            "import sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "from pip._internal.cli.main import main; "
            "sys.exit(main(['install', '--no-index', '--no-warn-script-location', sys.argv[1]]))"
        )
        return [str(self.python), "-c", code, str(wheel)]

    def pip_commands(self, variant: str) -> dict[str, list[list[str]]]:
        python = str(self.python)
        pip = [python, "-m", "pip", "install", "--progress-bar", "raw", "--no-warn-script-location"]
        return {
            "torch": [pip + [*TORCH_PACKAGES, "--index-url", torch_index(variant)]],
            # The embeddable Python ignores PYTHONPATH, so pip's isolated sdist builds cannot
            # see their build deps: install setuptools first and build without isolation.
            "deps": [pip + ["setuptools>=70", "wheel"], pip + ["--no-build-isolation", *RUNTIME_PACKAGES]],
            "models": [[python, str(self.runner), "--warmup", "--device", "cuda" if variant == "cu121" else "cpu",
                        "--threads", "2", "--models", str(self.models)]],
        }

    def _download(self, url: str, sha256: str, name: str, fetch, report: Callable[[float], None]) -> Path:
        target = self.downloads / name
        if not (target.is_file() and sha256_file(target) == sha256):
            fetch(url, target, lambda done, total: report(done / total if total else 0.0))
        if sha256_file(target) != sha256:
            target.unlink(missing_ok=True)
            raise TranscriptionError(f"hash_mismatch {name}")
        return target

    def _install_python(self, fetch, report: Callable[[float], None]) -> None:
        archive = self._download(PYTHON_EMBED_URL, PYTHON_EMBED_SHA256, Path(PYTHON_EMBED_URL).name, fetch,
                                 lambda f: report(0.9 * f))
        shutil.rmtree(self.python_dir, ignore_errors=True)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                    raise TranscriptionError("unsafe_python_archive")
            bundle.extractall(self.python_dir)
        # The embeddable build ignores site-packages until `import site` is enabled.
        for pth in self.python_dir.glob("python*._pth"):
            lines = [line for line in pth.read_text(encoding="utf-8").splitlines() if line.strip() != "#import site"]
            for extra in ("Lib\\site-packages", "import site"):
                if extra not in lines:
                    lines.append(extra)
            pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report(1.0)

    def _run(self, command: list[str], log, popen, on_line: Callable[[str], None]) -> int:
        log.write(f"\n$ {' '.join(command)}\n"); log.flush()
        process = popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                        errors="replace", env=self.install_environment(2), **self.popen_kwargs())
        for line in process.stdout:
            on_line(line.rstrip())
            if not line.startswith(("Progress ", "PROGRESS ")):
                log.write(line)
        return process.wait()

    def install(self, variant: str, report: Callable[[str, float], None] = lambda *_: None,
                popen: Callable[..., Any] = subprocess.Popen, fetch: Callable[..., None] = http_fetch) -> None:
        """Idempotent: already finished steps are skipped on a retry."""
        if not IS_WINDOWS and popen is subprocess.Popen:
            raise TranscriptionError("windows_only")
        self.root.mkdir(parents=True, exist_ok=True)
        expected = {"torch": 2600e6 if variant == "cu121" else 250e6, "deps": 350e6}
        with self.log_path.open("a", encoding="utf-8") as log:
            report("python", 0.0)
            if not self.python.is_file():
                self._install_python(fetch, lambda f: report("python", f))
            report("pip", 0.0)
            wheel = self._download(PIP_WHEEL_URL, PIP_WHEEL_SHA256, Path(PIP_WHEEL_URL).name, fetch, lambda f: report("pip", 0.5 * f))
            # `python <wheel>/pip` makes argv[0] end in "pip". Pip 25 then refuses
            # to modify itself on Windows. Import the wheel and call main() instead.
            if self._run(self.pip_bootstrap(wheel), log, popen, lambda _line: None) != 0:
                raise TranscriptionError("install_pip_failed")
            report("pip", 1.0)
            if variant == "cu121" and free_bytes(self.root) < CUDA_INSTALL_FREE_BYTES:
                free = free_bytes(self.root)
                log.write(f"\n# cu121 needs {CUDA_INSTALL_FREE_BYTES} free bytes, have {free}; using cpu\n")
                log.flush()
                variant = "cpu"
            for stage, commands in self.pip_commands(variant).items():
                state = {"done": 0, "current": 0}

                def on_line(line: str, stage=stage, state=state) -> None:
                    match = _PIP_PROGRESS.match(line)
                    if match:
                        current = int(match.group(1))
                        if current < state["current"]:
                            state["done"] += state["current"]  # a new file started
                        state["current"] = current
                        report(stage, min(0.99, (state["done"] + current) / expected.get(stage, 1e9)))
                        return
                    match = _RUNNER_PROGRESS.match(line)
                    if match and match.group(1) == "models":
                        report(stage, min(0.99, float(match.group(2))))

                report(stage, 0.0)
                for command in commands:
                    if self._run(command, log, popen, on_line) != 0:
                        raise TranscriptionError(f"install_{stage}_failed")
                report(stage, 1.0)
        self.marker.write_text(json.dumps({"installed_at": time.time(), "variant": variant, "python": PYTHON_VERSION,
                                           "packages": TORCH_PACKAGES + RUNTIME_PACKAGES}), encoding="utf-8")


def nvidia_smi_path() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for candidate in (Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "nvidia-smi.exe",
                      Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def wmi_gpu_names(run: Callable[[list[str]], str] = _run_quiet) -> list[str]:
    """Display adapters via WMI (Win32_VideoController); works without the NVIDIA tools on PATH."""
    if not IS_WINDOWS and run is _run_quiet:
        return []
    output = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                  "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"])
    return [line.strip()[:120] for line in output.splitlines() if line.strip()][:8]


def status_text(payload: dict | None, enabled: bool) -> str:
    """One line under the desktop checkbox."""
    if not enabled or not isinstance(payload, dict):
        return ""
    state = payload.get("state")
    if state == "installing":
        label = payload.get("stage_label") or "компоненты"
        return f"Подготовка к расшифровке: {label} — {int(payload.get('percent') or 0)}%"
    if state == "error":
        if payload.get("detail") == "install_pip_failed":
            return "Не удалось установить компоненты, мы уже чиним"
        return f"Не удалось подготовить расшифровку ({payload.get('detail') or 'ошибка'}). Проверьте интернет и место на диске (~6 ГБ)."
    if state == "running":
        return str(payload.get("detail") or "Идёт расшифровка")
    if state in {"idle", "ready"}:
        return "Готов к расшифровке"
    if state == "retrying":
        return "Нет связи с сервером расшифровки, повторим позже"
    return "Запуск…"


def read_status_file(data_root: Path) -> dict | None:
    try:
        value = json.loads((Path(data_root) / "transcription" / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ------------------------------------------------------------------ worker

class TranscriptionWorker:
    def __init__(self, config: dict, data_root: Path, resource_root: Path, *, client_factory: Callable[..., Any] | None = None,
                 runtime: Runtime | None = None, popen: Callable[..., Any] = subprocess.Popen,
                 probe_gpu: Callable[[], dict] = gpu_info, probe_wmi: Callable[[], list[str]] = wmi_gpu_names,
                 sleep: Callable[[float], None] = time.sleep, fetch: Callable[..., None] = http_fetch,
                 install_popen: Callable[..., Any] = subprocess.Popen):
        self.config = dict(config)
        self.enabled = bool(config.get(CONFIG_KEY, False))
        self.base = str(config.get("server_url") or "").rstrip("/")
        self.runtime = runtime or Runtime(data_root, resource_root, config)
        self.client_factory = client_factory or (lambda: create_http_client(
            self.base, timeout=30, trust_env=bool(config.get("trust_env_proxy", False)), follow_redirects=False))
        self.popen = popen
        self.probe_gpu = probe_gpu
        self.probe_wmi = probe_wmi
        self.sleep = sleep
        self.fetch = fetch
        self.install_popen = install_popen
        self._setup: dict = {}
        self._written: tuple = ()
        self.stop = threading.Event()
        self._guard = threading.Lock()
        self._status = {"state": "disabled" if not self.enabled else "starting", "detail": "", "job_id": None}
        self._install_thread: threading.Thread | None = None
        self._install_error = ""
        self._running_job: int | None = None
        self._stage = ("download", 0.0)
        self._gpu = {"gpu": False, "vram_mb": 0, "gpu_percent": None}

    # -- status for the desktop UI
    def snapshot(self) -> dict:
        with self._guard:
            return dict(self._status)

    def _set(self, **values) -> None:
        with self._guard:
            self._status.update(values)
            status = dict(self._status)
        self._write_status(status)

    def _write_status(self, status: dict) -> None:
        """status.json lets the desktop window show stage + percent under the checkbox."""
        key = (status.get("state"), status.get("stage"), status.get("percent"), status.get("detail"))
        if key == self._written:
            return
        self._written = key
        try:
            self.runtime.root.mkdir(parents=True, exist_ok=True)
            target = self.runtime.root / "status.json"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps({**status, "enabled": self.enabled, "updated_at": time.time()}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, target)
        except OSError:
            pass

    def runtime_state(self) -> tuple[str, str]:
        if self.runtime.ready():
            return "ready", ""
        if self._install_thread and self._install_thread.is_alive():
            return "installing", self.snapshot().get("detail", "")
        if self._install_error:
            return "error", self._install_error
        return "missing", ""

    def ensure_runtime(self) -> None:
        state, _ = self.runtime_state()
        if state != "missing":
            return
        self._gpu = self.probe_gpu()
        variant = select_variant(self._gpu, [] if self._gpu.get("gpu") else self.probe_wmi(), free_bytes(self.runtime.root))
        plan = install_plan(variant)
        labels = {name: label for name, label, _ in plan}

        def report(stage: str, fraction: float) -> None:
            percent = overall_percent(plan, stage, fraction)
            self._setup = {"stage": stage, "percent": percent, "variant": variant}
            self._set(state="installing", stage=stage, stage_label=labels.get(stage, stage), percent=percent,
                      detail=f"{labels.get(stage, stage)} — {percent}%")

        def install():
            try:
                self.runtime.install(variant, report=report, popen=self.install_popen, fetch=self.fetch)
                self._setup = {}
                self._set(state="idle", stage="", stage_label="", percent=100, detail="")
            except Exception as exc:
                self._install_error = getattr(exc, "reason", type(exc).__name__)
                self._set(state="error", detail=self._install_error)

        report("python", 0.0)

        self._install_thread = threading.Thread(target=install, name="xass-transcription-install", daemon=True)
        self._install_thread.start()

    def poll_body(self) -> dict:
        self._gpu = self.probe_gpu()
        state, detail = self.runtime_state() if self.enabled else ("ready", "")
        if state == "missing":
            state = "installing"
        body = {"enabled": self.enabled, "state": state, "detail": str(detail)[:300], "capabilities": capabilities(self._gpu),
                "load": current_load(self._gpu, running=self._running_job is not None, interval=0.5),
                "running_job_id": self._running_job}
        if state == "installing":
            body["setup"] = {"stage": str(self._setup.get("stage") or "python"), "percent": int(self._setup.get("percent") or 0)}
        return body

    def poll_once(self, client) -> dict | None:
        response = client.post(self.base + "/agent/transcription/poll", json=self.poll_body())
        if response.status_code == 404:
            return None  # server not upgraded yet
        response.raise_for_status()
        body = response.json()
        return body.get("job") if isinstance(body.get("job"), dict) else None

    def run_forever(self) -> None:
        if not self.base or not str(self.config.get("api_key", "")).startswith("ag_"):
            return
        while not self.stop.is_set():
            delay = POLL_SEC if self.enabled else DISABLED_POLL_SEC
            try:
                with self.client_factory() as client:
                    client.headers.update({"X-Api-Key": str(self.config["api_key"]), "Accept-Encoding": "identity"})
                    if self.enabled:
                        self.ensure_runtime()
                    job = self.poll_once(client)
                    state, detail = self.runtime_state() if self.enabled else ("disabled", "")
                    if state != "installing":  # the installer thread reports its own stage + percent
                        self._set(state="idle" if state == "ready" else state, detail=detail)
                    if job is not None and self.enabled:
                        self.process(client, job)
                        delay = 1
            except Exception as exc:
                self._set(state="retrying", detail=f"Нет связи с сервером расшифровки ({type(exc).__name__})")
            if not self.enabled:
                return  # one "disabled" report per agent start is enough
            self.stop.wait(delay)

    # -- one job
    def _renew(self, client, job_id: int) -> None:
        stage, fraction = self._stage
        response = client.post(f"{self.base}/agent/transcription/jobs/{job_id}/progress",
                               json={"stage": stage, "fraction": fraction})
        if response.status_code == 409:
            raise LeaseLost()
        response.raise_for_status()

    def download(self, client, job: dict, target: Path) -> Path:
        path = str(job.get("media_path") or "")
        if not path.startswith("/agent/music/tracks/") or "://" in path or ".." in path:
            raise TranscriptionError("invalid_media_path")
        suffix = EXTENSIONS.get(str(job.get("mime") or ""), Path(str(job.get("filename") or "")).suffix[:6] or ".bin")
        destination = target / ("input" + suffix)
        size = 0
        with client.stream("GET", self.base + path) as response:
            if response.status_code != 200:
                raise TranscriptionError(f"download_http_{response.status_code}")
            with destination.open("wb") as stream:
                for chunk in response.iter_bytes(chunk_size=256 * 1024):
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise TranscriptionError("audio_too_large")
                    stream.write(chunk)
        if size == 0:
            raise TranscriptionError("empty_audio")
        return destination

    def runner_command(self, job: dict, source: Path, output: Path, workdir: Path, threads: int) -> list[str]:
        # CUDA only when an NVIDIA GPU is present *and* the CUDA torch wheel was installed.
        device = "cuda" if self._gpu.get("gpu") and self.runtime.variant() == "cu121" else "cpu"
        language = str(job.get("language") or "ru")
        return [str(self.runtime.python), str(self.runtime.runner), "--input", str(source), "--output", str(output),
                "--language", language, "--device", device, "--threads", str(threads),
                "--models", str(self.runtime.models), "--workdir", str(workdir), "--parent-pid", str(os.getpid())]

    def run_runner(self, client, job_id: int, command: list[str], threads: int, deadline: float) -> None:
        process = self.popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             errors="replace", env=self.runtime.environment(threads), **self.runtime.popen_kwargs())
        stderr_tail: list[str] = []

        def read_stdout():
            for line in process.stdout:
                parts = line.split()
                if len(parts) == 3 and parts[0] == "PROGRESS":
                    try:
                        self._stage = (parts[1], float(parts[2]))
                    except ValueError:
                        pass

        def read_stderr():
            for line in process.stderr:
                stderr_tail.append(line.strip()); del stderr_tail[:-20]

        readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
        for reader in readers:
            reader.start()
        last_renew = time.monotonic()
        try:
            while process.poll() is None:
                if self.stop.is_set():
                    raise TranscriptionError("client_stopped")
                if time.monotonic() > deadline:
                    raise TranscriptionError("local_timeout")
                if time.monotonic() - last_renew >= RENEW_SEC:
                    self._renew(client, job_id)
                    last_renew = time.monotonic()
                self.sleep(1)
        except BaseException:
            _kill_tree(process)
            raise
        for reader in readers:
            reader.join(timeout=5)
        if process.returncode != 0:
            detail = next((line for line in reversed(stderr_tail) if line.startswith("ERROR")), "")
            raise TranscriptionError(("runner_failed " + detail)[:200].strip())

    def process(self, client, job: dict) -> str:
        job_id = int(job["id"])
        self._running_job = job_id
        self._stage = ("download", 0.0)
        self._set(state="running", job_id=job_id, detail=f"Расшифровка: {job.get('title') or 'трек'}")
        workdir = self.runtime.jobs / str(job_id)
        started = time.monotonic()
        try:
            if not self.runtime.ready():
                raise TranscriptionError("runtime_not_ready")
            shutil.rmtree(workdir, ignore_errors=True)
            workdir.mkdir(parents=True, exist_ok=True)
            source = self.download(client, job, workdir)
            self._stage = ("separate", 0.0)
            self._renew(client, job_id)
            threads = thread_limit(capabilities(self._gpu))
            output = workdir / "result.json"
            budget = max(30 * 60, 12 * float(job.get("duration") or 0)) - 60
            self.run_runner(client, job_id, self.runner_command(job, source, output, workdir, threads), threads,
                            started + budget)
            result = json.loads(output.read_text(encoding="utf-8"))
            lines = [row for row in result.get("lines", []) if isinstance(row, dict) and str(row.get("text") or "").strip()]
            if not lines:
                raise TranscriptionError("no_speech_detected")
            response = client.post(f"{self.base}/agent/transcription/jobs/{job_id}/complete", json={
                "lines": lines[:2000], "language": str(result.get("language") or "")[:8], "model": str(result.get("model") or "")[:64],
                "device": str(result.get("device") or "")[:16], "elapsed_sec": round(time.monotonic() - started, 1)})
            if response.status_code == 409:
                raise LeaseLost()
            response.raise_for_status()
            return "done"
        except LeaseLost:
            return "lease_lost"  # the server already reassigned it; nothing to report
        except Exception as exc:
            reason = exc.reason if isinstance(exc, TranscriptionError) else f"client_error {type(exc).__name__}"
            try:
                client.post(f"{self.base}/agent/transcription/jobs/{job_id}/fail", json={"reason": reason[:200]})
            except Exception:
                pass  # the lease will expire and the server reassigns anyway
            return "failed"
        finally:
            self._running_job = None
            self._set(state="idle", job_id=None, detail="")
            shutil.rmtree(workdir, ignore_errors=True)


def _kill_tree(process) -> None:
    try:
        if not isinstance(process.pid, int) or process.pid <= 0:
            raise ValueError("no pid")
        parent = psutil.Process(process.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except (psutil.Error, OSError, AttributeError, TypeError, ValueError):
        try:
            process.kill()
        except Exception:
            pass


_worker: TranscriptionWorker | None = None


def start_transcription_worker(config: dict, data_root: Path, resource_root: Path) -> TranscriptionWorker | None:
    """Started once per agent run. When disabled it only reports "off" once."""
    global _worker
    if _worker is not None:
        return _worker
    worker = TranscriptionWorker(config, data_root, resource_root)
    threading.Thread(target=worker.run_forever, name="xass-transcription", daemon=True).start()
    _worker = worker
    return worker


def transcription_snapshot() -> dict:
    return _worker.snapshot() if _worker else {"state": "disabled", "detail": "", "job_id": None}
