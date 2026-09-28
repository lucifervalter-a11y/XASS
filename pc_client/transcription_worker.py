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

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable

import psutil

try:
    from network_client import create_http_client
except ModuleNotFoundError:
    from pc_client.network_client import create_http_client

CONFIG_KEY = "transcription_enabled"
SETTING_LABEL = "Использовать этот ПК для расшифровки текста"
POLL_SEC = 20
DISABLED_POLL_SEC = 300
RENEW_SEC = 60
MAX_AUDIO_BYTES = 256 * 1024 * 1024
BUSY_PERCENT = 70.0
# torch 2.5.1 cu121 bundles cuDNN 9 / cuBLAS 12, which CTranslate2 4.x
# (faster-whisper) needs; wheels exist for Python 3.10-3.12. demucs 4.1 does
# its audio I/O without torchaudio.
TORCH_PACKAGES = ["torch==2.5.1"]
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu121"
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
RUNTIME_PACKAGES = ["demucs==4.1.0", "faster-whisper==1.2.1"]
SUPPORTED_PYTHON = ((3, 10), (3, 11), (3, 12))
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
    if run is _run_quiet and not shutil.which("nvidia-smi"):
        return {"gpu": False, "gpu_name": "", "vram_mb": 0, "gpu_percent": None}
    output = run(["nvidia-smi", "--query-gpu=name,memory.total,utilization.gpu", "--format=csv,noheader,nounits"])
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

class Runtime:
    """Separate venv + model cache; installed on first enable, never bundled."""

    def __init__(self, data_root: Path, resource_root: Path, config: dict | None = None):
        config = config or {}
        self.root = Path(data_root) / "transcription"
        self.env = self.root / "env"
        self.models = self.root / "models"
        self.jobs = self.root / "jobs"
        self.marker = self.root / "ready.json"
        self.log_path = self.root / "install.log"
        self.runner = Path(resource_root) / "transcribe_runner.py"
        self.base_python = str(config.get("transcription_python") or "")
        self.priority = IDLE_PRIORITY_CLASS if config.get("transcription_priority") == "idle" else BELOW_NORMAL_PRIORITY_CLASS

    @property
    def python(self) -> Path:
        return self.env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def ready(self) -> bool:
        return self.marker.is_file() and self.python.is_file() and self.runner.is_file()

    def environment(self, threads: int) -> dict:
        env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "CT2_INTER_THREADS"):
            env[key] = str(threads)
        env.update(TORCH_HOME=str(self.models / "torch"), HF_HOME=str(self.models / "hf"),
                   HF_HUB_DISABLE_TELEMETRY="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        return env

    def popen_kwargs(self) -> dict:
        if os.name == "nt":
            return {"creationflags": self.priority | CREATE_NO_WINDOW}
        return {"preexec_fn": lambda: os.nice(19 if self.priority == IDLE_PRIORITY_CLASS else 10)}

    def find_base_python(self, run: Callable[[list[str]], str] = _run_quiet) -> list[str] | None:
        candidates = []
        if self.base_python:
            candidates.append([self.base_python])
        if os.name == "nt":
            candidates += [["py", f"-{major}.{minor}"] for major, minor in reversed(SUPPORTED_PYTHON)]
        candidates += [["python3.12"], ["python3.11"], ["python3.10"], ["python"], ["python3"]]
        for command in candidates:
            version = run(command + ["-c", "import sys;print('%d.%d'%sys.version_info[:2])"]).strip()
            if version and tuple(int(x) for x in version.split(".")[:2]) in SUPPORTED_PYTHON:
                return command
        return None

    def install_commands(self, base: list[str], gpu: bool) -> list[list[str]]:
        python = str(self.python)
        index = TORCH_CUDA_INDEX if gpu else TORCH_CPU_INDEX
        return [
            base + ["-m", "venv", str(self.env)],
            [python, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"],
            [python, "-m", "pip", "install", "--disable-pip-version-check", *TORCH_PACKAGES, "--index-url", index],
            [python, "-m", "pip", "install", "--disable-pip-version-check", *RUNTIME_PACKAGES],
            [python, str(self.runner), "--warmup", "--device", "cuda" if gpu else "cpu", "--threads", "2", "--models", str(self.models)],
        ]

    def install(self, gpu: bool, report: Callable[[str], None] = lambda _: None,
                popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        base = self.find_base_python()
        if base is None:
            raise TranscriptionError("python_3_10_to_3_12_not_found")
        commands = self.install_commands(base, gpu)
        with self.log_path.open("a", encoding="utf-8") as log:
            for index, command in enumerate(commands, 1):
                report(f"Установка компонентов расшифровки: шаг {index} из {len(commands)}")
                log.write(f"\n$ {' '.join(command)}\n"); log.flush()
                process = popen(command, stdout=log, stderr=subprocess.STDOUT, env=self.environment(2), **self.popen_kwargs())
                if process.wait() != 0:
                    raise TranscriptionError(f"install_step_{index}_failed")
        self.marker.write_text(json.dumps({"installed_at": time.time(), "gpu": gpu, "packages": RUNTIME_PACKAGES}), encoding="utf-8")


# ------------------------------------------------------------------ worker

class TranscriptionWorker:
    def __init__(self, config: dict, data_root: Path, resource_root: Path, *, client_factory: Callable[..., Any] | None = None,
                 runtime: Runtime | None = None, popen: Callable[..., Any] = subprocess.Popen,
                 probe_gpu: Callable[[], dict] = gpu_info, sleep: Callable[[float], None] = time.sleep):
        self.config = dict(config)
        self.enabled = bool(config.get(CONFIG_KEY, False))
        self.base = str(config.get("server_url") or "").rstrip("/")
        self.runtime = runtime or Runtime(data_root, resource_root, config)
        self.client_factory = client_factory or (lambda: create_http_client(
            self.base, timeout=30, trust_env=bool(config.get("trust_env_proxy", False)), follow_redirects=False))
        self.popen = popen
        self.probe_gpu = probe_gpu
        self.sleep = sleep
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
        gpu = bool(self._gpu.get("gpu"))

        def install():
            try:
                self.runtime.install(gpu, report=lambda text: self._set(state="installing", detail=text))
            except Exception as exc:
                self._install_error = getattr(exc, "reason", type(exc).__name__)
                self._set(state="error", detail=self._install_error)

        self._install_thread = threading.Thread(target=install, name="xass-transcription-install", daemon=True)
        self._install_thread.start()

    def poll_body(self) -> dict:
        self._gpu = self.probe_gpu()
        state, detail = self.runtime_state() if self.enabled else ("ready", "")
        if state == "missing":
            state = "installing"
        return {"enabled": self.enabled, "state": state, "detail": str(detail)[:300], "capabilities": capabilities(self._gpu),
                "load": current_load(self._gpu, running=self._running_job is not None, interval=0.5),
                "running_job_id": self._running_job}

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
        device = "cuda" if self._gpu.get("gpu") else "cpu"
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
