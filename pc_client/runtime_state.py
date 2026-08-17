from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, TextIO


def data_root() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "XASS"
    root.mkdir(parents=True, exist_ok=True)
    return root


LOG_ROOT = data_root() / "logs"
APP_LOG_PATH = LOG_ROOT / "xass.log"


def _valid_json_object(path: Path) -> bool:
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8-sig")), dict)
    except (OSError, ValueError, TypeError, UnicodeError):
        return False


def atomic_write_json(path: Path, payload: Any, *, backup: bool = False) -> None:
    """Durably replace a JSON file while preserving its last valid version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    # Validate the exact representation before touching the destination.
    json.loads(encoded)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    backup_path = path.with_suffix(path.suffix + ".bak")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if backup and path.is_file() and _valid_json_object(path):
            shutil.copy2(path, backup_path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json_object(path: Path, *, restore_backup: bool = False) -> dict[str, Any]:
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError, UnicodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if candidate != path and restore_backup:
            atomic_write_json(path, payload, backup=False)
        return payload
    return {}


def append_log(line: str) -> None:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    cleaned = str(line).replace("\x00", "").rstrip("\r\n")
    if not cleaned:
        return
    try:
        if APP_LOG_PATH.is_file() and APP_LOG_PATH.stat().st_size > 5 * 1024 * 1024:
            rotated = APP_LOG_PATH.with_suffix(".log.1")
            rotated.unlink(missing_ok=True)
            os.replace(APP_LOG_PATH, rotated)
        with APP_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(cleaned + "\n")
    except OSError:
        return


def read_log_tail(limit: int = 120) -> list[str]:
    try:
        rows = APP_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return rows[-max(1, min(int(limit), 1000)) :]


class _TeeStream:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self._buffer = ""

    def write(self, value: str) -> int:
        text = str(value)
        written = self.stream.write(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            append_log(line)
        return written

    def flush(self) -> None:
        self.stream.flush()
        if self._buffer:
            append_log(self._buffer)
            self._buffer = ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


def configure_utf8_logging() -> None:
    """Keep console, captured subprocess output and persisted logs in UTF-8."""
    if getattr(sys, "_xass_utf8_logging", False):
        return
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass
        setattr(sys, name, _TeeStream(stream))
    setattr(sys, "_xass_utf8_logging", True)


class SingleInstance:
    def __init__(self, name: str, handle: int | None, lock_path: Path | None = None) -> None:
        self.name = name
        self.handle = handle
        self.lock_path = lock_path

    def close(self) -> None:
        if self.handle and os.name == "nt":
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None
        if self.lock_path is not None:
            self.lock_path.unlink(missing_ok=True)
            self.lock_path = None


def acquire_single_instance(name: str) -> SingleInstance | None:
    """Acquire a process-wide instance guard (separate names for GUI and agent)."""
    safe_name = "".join(char if char.isalnum() else "-" for char in name)
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, f"Local\\{safe_name}")
        if not handle:
            return None
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return None
        return SingleInstance(name, int(handle))

    lock_path = data_root() / f".{safe_name}.instance"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
    except FileExistsError:
        try:
            pid = int(lock_path.read_text(encoding="ascii").strip())
            os.kill(pid, 0)
            return None
        except (OSError, ValueError):
            lock_path.unlink(missing_ok=True)
            return acquire_single_instance(name)
    return SingleInstance(name, None, lock_path)
