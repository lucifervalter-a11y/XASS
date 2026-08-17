from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import psutil

try:
    from runtime_state import atomic_write_json, load_json_object
except ModuleNotFoundError:  # Imported as pc_client.client_update in tests/tools.
    from pc_client.runtime_state import atomic_write_json, load_json_object

CLIENT_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", CLIENT_ROOT))
DATA_ROOT = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "XASS"
    if getattr(sys, "frozen", False)
    else CLIENT_ROOT
)
DATA_ROOT.mkdir(parents=True, exist_ok=True)
VERSION_PATH = RESOURCE_ROOT / "version.json"
BUILD_INFO_PATH = RESOURCE_ROOT / "build-info.json"
REVISION_PATH = DATA_ROOT / ".installed-revision"
RESULTS_PATH = DATA_ROOT / ".command-results.json"
AGENT_STATUS_PATH = DATA_ROOT / ".agent-status.json"
UPDATE_ROOT = DATA_ROOT / ".updates"
UPDATE_MARKER = UPDATE_ROOT / ".in-progress"
UPDATE_LOCK = UPDATE_ROOT / ".operation.lock"
UPDATE_RESULT = UPDATE_ROOT / ".last-result.json"
ACTIVE_UPDATE_PHASES = {"checking", "downloading", "verifying", "installing", "restarting", "health-check"}


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        return psutil.Process(pid).is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


def load_update_state() -> dict[str, Any]:
    return load_json_object(UPDATE_MARKER)


def write_update_state(phase: str, **details: Any) -> None:
    payload = load_update_state()
    now = time.time()
    payload.update(details)
    payload.update({"phase": str(phase), "updated_at": now})
    payload.setdefault("started_at", now)
    payload.setdefault("pid", os.getpid())
    atomic_write_json(UPDATE_MARKER, payload, backup=False)


def store_update_result(ok: bool, message: str, **details: Any) -> None:
    atomic_write_json(
        UPDATE_RESULT,
        {"ok": bool(ok), "message": str(message)[:1000], "finished_at": time.time(), **details},
        backup=False,
    )


def update_in_progress() -> bool:
    state = load_update_state()
    if state:
        try:
            pid = int(state.get("pid") or 0)
            updated_at = float(state.get("updated_at") or state.get("started_at") or 0)
        except (TypeError, ValueError):
            pid, updated_at = 0, 0.0
        phase = str(state.get("phase") or "")
        if phase in ACTIVE_UPDATE_PHASES and (_process_is_alive(pid) or time.time() - updated_at < 30):
            return True
        UPDATE_MARKER.unlink(missing_ok=True)
    if UPDATE_LOCK.is_file():
        lock = load_json_object(UPDATE_LOCK)
        try:
            pid = int(lock.get("pid") or 0)
            created_at = float(lock.get("created_at") or 0)
        except (TypeError, ValueError):
            pid, created_at = 0, 0.0
        if _process_is_alive(pid) or time.time() - created_at < 30:
            return True
        UPDATE_LOCK.unlink(missing_ok=True)
    return False


class UpdateOperation:
    def __init__(self, version: str, revision: str) -> None:
        self.version = str(version or "")
        self.revision = str(revision or "")
        self.acquired = False

    def __enter__(self) -> "UpdateOperation":
        UPDATE_ROOT.mkdir(parents=True, exist_ok=True)
        if update_in_progress():
            raise RuntimeError("другое обновление XASS уже выполняется")
        payload = json.dumps(
            {"pid": os.getpid(), "created_at": time.time(), "version": self.version, "revision": self.revision},
            ensure_ascii=True,
        ).encode("ascii")
        try:
            descriptor = os.open(UPDATE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RuntimeError("другое обновление XASS уже выполняется") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        self.acquired = True
        write_update_state(
            "checking",
            pid=os.getpid(),
            version=self.version,
            revision=self.revision,
            message="Проверка обновления",
            progress_percent=0,
        )
        return self

    def phase(self, phase: str, message: str, **details: Any) -> None:
        write_update_state(
            phase,
            pid=os.getpid(),
            version=self.version,
            revision=self.revision,
            message=message,
            **details,
        )

    def __exit__(self, exc_type: Any, exc: BaseException | None, _traceback: Any) -> None:
        try:
            state = load_update_state()
            state_pid = int(state.get("pid") or 0) if state else 0
            if exc is not None:
                store_update_result(False, str(exc), version=self.version, revision=self.revision, phase="error")
            # A launcher transfers the marker to its helper PID. Leave that marker
            # intact; otherwise remove only the state owned by this process.
            if state_pid in {0, os.getpid()}:
                UPDATE_MARKER.unlink(missing_ok=True)
        finally:
            if self.acquired:
                UPDATE_LOCK.unlink(missing_ok=True)
                self.acquired = False


def update_operation(version: str, revision: str) -> UpdateOperation:
    return UpdateOperation(version, revision)


def is_installer_build() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_version() -> str:
    try:
        payload = json.loads(VERSION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "0.0.0"
    return str(payload.get("version") or "0.0.0").strip() or "0.0.0"


def current_revision() -> str:
    try:
        return REVISION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        try:
            payload = json.loads(BUILD_INFO_PATH.read_text(encoding="utf-8"))
            return str(payload.get("revision") or "").strip()
        except (OSError, ValueError, TypeError):
            return ""


def write_agent_status(
    state: str,
    *,
    detail: str = "",
    server_time: str = "",
    process_id: int | None = None,
    **details: Any,
) -> None:
    """Publish agent connectivity for the desktop UI without relying on stdout."""
    payload = {
        "state": str(state).strip().lower(),
        "detail": str(detail)[:1000],
        "server_time": str(server_time)[:128],
        "process_id": int(process_id if process_id is not None else os.getpid()),
        "updated_at": time.time(),
        **details,
    }
    temporary = AGENT_STATUS_PATH.with_name(f"{AGENT_STATUS_PATH.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(AGENT_STATUS_PATH)
    except OSError:
        temporary.unlink(missing_ok=True)


def load_agent_status() -> dict[str, Any] | None:
    try:
        payload = json.loads(AGENT_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _manifest_message(manifest: dict[str, Any]) -> bytes:
    return (
        f"{manifest.get('version', '')}\n{manifest.get('revision', '')}\n"
        f"{manifest.get('sha256', '')}\n{manifest.get('url', '')}"
    ).encode("utf-8")


def verify_manifest(manifest: dict[str, Any], api_key: str) -> bool:
    signature = str(manifest.get("signature") or "")
    if len(signature) != 64 or not api_key:
        return False
    expected = hmac.new(api_key.encode("utf-8"), _manifest_message(manifest), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_sha256(value: object) -> str:
    normalized = re.sub(r"\s+", "", str(value or "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise RuntimeError("update manifest contains an invalid SHA-256")
    return normalized


def _manifest_size(manifest: dict[str, Any]) -> int:
    raw = manifest.get("size")
    if raw in {None, ""}:
        return 0
    try:
        size = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("update manifest contains an invalid package size") from exc
    if size <= 0 or size > 500 * 1024 * 1024:
        raise RuntimeError("update manifest contains an invalid package size")
    return size


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > 250:
        raise RuntimeError("update package contains too many files")
    total_size = 0
    for info in infos:
        raw_name = info.filename.replace("\\", "/")
        name = PurePosixPath(raw_name)
        has_drive = bool(name.parts and ":" in name.parts[0])
        if name.is_absolute() or ".." in name.parts or not name.parts or has_drive:
            raise RuntimeError("unsafe path in update package")
        total_size += max(0, int(info.file_size))
    if total_size > 150 * 1024 * 1024:
        raise RuntimeError("update package is too large")


def _write_stream_with_progress(
    response: Any,
    destination: Path,
    *,
    label: str,
    progress: Callable[[str], None] | None,
) -> tuple[int, int]:
    try:
        total = max(0, int(response.headers.get("content-length") or 0))
    except (TypeError, ValueError):
        total = 0
    received = 0
    last_percent = -1
    if progress:
        progress(f"{label} 0%" if total else f"{label}…")
    with destination.open("wb") as handle:
        for chunk in response.iter_bytes():
            if not chunk:
                continue
            handle.write(chunk)
            received += len(chunk)
            if progress and total:
                percent = min(100, int(received * 100 / total))
                if percent == 100 or percent >= last_percent + 2:
                    last_percent = percent
                    progress(f"{label} {percent}% · {received:,} / {total:,} байт")
        handle.flush()
        os.fsync(handle.fileno())
    if total and received != total:
        raise RuntimeError(f"download interrupted: expected {total} bytes, received {received}")
    if progress and not total:
        progress(f"{label} · получено {received:,} байт")
    return received, total


def download_update(
    manifest: dict[str, Any],
    *,
    api_key: str,
    trust_env: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    import httpx

    if not verify_manifest(manifest, api_key):
        raise RuntimeError("update manifest signature is invalid")
    url = str(manifest.get("url") or "").strip()
    expected_sha = _normalize_sha256(manifest.get("sha256"))
    expected_size = _manifest_size(manifest)
    revision = str(manifest.get("revision") or "").strip()
    if not url or not revision:
        raise RuntimeError("update manifest is incomplete")

    # Every downloader gets a private staging directory. The desktop UI and its
    # background agent can briefly overlap while an old version is restarting;
    # sharing package.zip made those two writers corrupt each other's download.
    attempt_id = uuid4().hex
    stage_root = UPDATE_ROOT / f"{revision[:16]}-{attempt_id[:8]}"
    stage_root.mkdir(parents=True, exist_ok=False)
    package_path = stage_root / "package.zip"
    extract_path = stage_root / "staged"
    try:
        with httpx.Client(timeout=httpx.Timeout(90, connect=15), trust_env=trust_env, follow_redirects=True) as client:
            last_error: Exception | None = None
            for attempt in range(2):
                label = "Скачивание обновления" if attempt == 0 else "Повторная загрузка обновления"
                download_url = _cache_busted_url(url, f"{attempt_id}-{attempt}")
                headers = {
                    "X-Api-Key": api_key,
                    "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                }
                try:
                    with client.stream("GET", download_url, headers=headers) as response:
                        response.raise_for_status()
                        received, _content_size = _write_stream_with_progress(
                            response, package_path, label=label, progress=progress
                        )
                    if expected_size and received != expected_size:
                        raise RuntimeError(
                            f"update package size mismatch: expected {expected_size}, received {received}"
                        )
                    actual_sha = _sha256(package_path)
                    if not hmac.compare_digest(actual_sha, expected_sha):
                        raise RuntimeError(
                            f"update package checksum mismatch: expected {expected_sha}, got {actual_sha}"
                        )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    package_path.unlink(missing_ok=True)
            else:
                raise RuntimeError(str(last_error or "update download failed"))

        extract_path.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(package_path, "r") as archive:
            _validate_archive(archive)
            archive.extractall(extract_path)
        if progress:
            progress("Пакет проверен")
        return extract_path
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def _cache_busted_url(url: str, token: str) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("download", token))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def download_installer_update(
    manifest: dict[str, Any],
    *,
    api_key: str,
    trust_env: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    import httpx

    if not is_installer_build():
        raise RuntimeError("installer update is only supported by the installed XASS app")
    if not verify_manifest(manifest, api_key):
        raise RuntimeError("installer manifest signature is invalid")
    url = str(manifest.get("url") or "").strip()
    expected_sha = _normalize_sha256(manifest.get("sha256"))
    expected_size = _manifest_size(manifest)
    version = str(manifest.get("version") or "").strip()
    revision = str(manifest.get("revision") or "").strip()
    if not url or not version or not revision:
        raise RuntimeError("installer manifest is incomplete")

    UPDATE_ROOT.mkdir(parents=True, exist_ok=True)
    target = UPDATE_ROOT / f"XASS-Setup-{version}-{revision[:12]}.exe"
    temporary = target.with_suffix(".download")
    temporary.unlink(missing_ok=True)
    try:
        last_error: Exception | None = None
        with httpx.Client(timeout=httpx.Timeout(180, connect=15), trust_env=trust_env, follow_redirects=True) as client:
            for attempt in range(2):
                label = "Скачивание установщика XASS" if attempt == 0 else "Повторная загрузка установщика XASS"
                try:
                    with client.stream(
                        "GET",
                        _cache_busted_url(url, f"{uuid4().hex}-{attempt}"),
                        headers={"X-Api-Key": api_key, "Accept-Encoding": "identity", "Cache-Control": "no-cache"},
                    ) as response:
                        response.raise_for_status()
                        received, _content_size = _write_stream_with_progress(
                            response,
                            temporary,
                            label=label,
                            progress=progress,
                        )
                    if expected_size and received != expected_size:
                        raise RuntimeError(
                            f"installer size mismatch: expected {expected_size}, received {received}"
                        )
                    actual_sha = _sha256(temporary)
                    if not hmac.compare_digest(actual_sha, expected_sha):
                        raise RuntimeError(
                            f"installer checksum mismatch: expected {expected_sha}, got {actual_sha}"
                        )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    temporary.unlink(missing_ok=True)
            else:
                raise RuntimeError(str(last_error or "installer download failed"))
        temporary.replace(target)
        if progress:
            progress("Установщик проверен")
        return target
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def launch_installer_update(
    installer_path: Path,
    *,
    wait_pid: int | None = None,
    expected_version: str = "",
    expected_revision: str = "",
) -> None:
    if not is_installer_build() or os.name != "nt":
        raise RuntimeError("installer update requires the installed Windows app")
    bundled_helper = Path(sys.executable).with_name("XASSUpdater.exe")
    if not bundled_helper.is_file():
        raise RuntimeError("XASS update helper is missing")
    helper_root = UPDATE_ROOT / "helpers"
    helper_root.mkdir(parents=True, exist_ok=True)
    for stale_helper in helper_root.glob("XASSUpdater-*.exe"):
        try:
            stale_helper.unlink()
        except OSError:
            pass
    detached_helper = helper_root / f"XASSUpdater-{uuid4().hex}.exe"
    shutil.copy2(bundled_helper, detached_helper)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    args = [
        str(detached_helper),
        "--installer",
        str(installer_path),
        "--wait-pid",
        str(wait_pid or os.getppid()),
    ]
    if expected_version:
        args.extend(["--expected-version", expected_version])
    if expected_revision:
        args.extend(["--expected-revision", expected_revision])
    helper_process = subprocess.Popen(
        args,
        cwd=str(installer_path.parent),
        creationflags=creationflags,
        close_fds=True,
    )
    try:
        helper_pid = int(helper_process.pid)
    except (TypeError, ValueError):
        helper_pid = 0
    write_update_state(
        "installing",
        pid=helper_pid,
        version=expected_version,
        revision=expected_revision,
        message="Установка проверенного пакета",
        progress_percent=100,
    )


def launch_update_helper(
    stage_path: Path,
    manifest: dict[str, Any],
    *,
    command_id: int | None,
    restart_target: str,
    minimized: bool = False,
) -> None:
    helper = CLIENT_ROOT / "updater_helper.py"
    args = [
        sys.executable,
        str(helper),
        "--pid",
        str(os.getpid()),
        "--stage",
        str(stage_path),
        "--target",
        str(CLIENT_ROOT),
        "--version",
        str(manifest.get("version") or "0.0.0"),
        "--revision",
        str(manifest.get("revision") or ""),
        "--restart-target",
        restart_target,
    ]
    if command_id is not None:
        args.extend(["--command-id", str(command_id)])
    if minimized:
        args.append("--minimized")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    UPDATE_ROOT.mkdir(parents=True, exist_ok=True)
    marker_payload = {
        "pid": 0,
        "revision": str(manifest.get("revision") or ""),
        "started_at": time.time(),
    }
    atomic_write_json(UPDATE_MARKER, marker_payload, backup=False)
    try:
        helper_process = subprocess.Popen(args, cwd=str(CLIENT_ROOT), creationflags=creationflags)
    except Exception:
        UPDATE_MARKER.unlink(missing_ok=True)
        raise
    marker_payload["pid"] = helper_process.pid
    try:
        marker_payload.update({"phase": "installing", "message": "Установка обновления", "updated_at": time.time()})
        atomic_write_json(UPDATE_MARKER, marker_payload, backup=False)
    except OSError:
        pass


def load_command_results() -> list[dict[str, Any]]:
    try:
        payload = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return payload if isinstance(payload, list) else []


def store_command_result(command_id: int, ok: bool, message: str, details: dict[str, Any] | None = None) -> None:
    rows = [row for row in load_command_results() if int(row.get("id", -1)) != int(command_id)]
    rows.append({"id": int(command_id), "ok": bool(ok), "message": str(message)[:1000], "details": details or {}})
    atomic_write_json(RESULTS_PATH, rows[-50:], backup=False)


def clear_command_results(command_ids: list[int]) -> None:
    ids = {int(item) for item in command_ids}
    remaining = [row for row in load_command_results() if int(row.get("id", -1)) not in ids]
    if remaining:
        atomic_write_json(RESULTS_PATH, remaining, backup=False)
    elif RESULTS_PATH.exists():
        RESULTS_PATH.unlink()
