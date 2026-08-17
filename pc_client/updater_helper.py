from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


MANAGED_FILES_NAME = ".xass-managed-files.json"
PROTECTED_PARTS = {".venv", ".updates", "archive", "data"}
PROTECTED_NAMES = {"config.json", ".command-results.json", ".agent-status.json", ".installed-revision"}


def _requirements_digest(requirements: Path) -> str:
    return hashlib.sha256(requirements.read_bytes()).hexdigest()


def _requirements_need_install(target: Path, requirements: Path) -> bool:
    stamp = target / ".venv" / ".requirements.sha256"
    try:
        return stamp.read_text(encoding="utf-8").strip() != _requirements_digest(requirements)
    except OSError:
        return True


def _wait_for_exit(pid: int, timeout: int = 20) -> bool:
    if os.name == "nt":
        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return True
        try:
            return kernel32.WaitForSingleObject(handle, max(0, timeout) * 1000) == 0
        finally:
            kernel32.CloseHandle(handle)

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.5)
    return False


def _safe_files(stage: Path) -> list[Path]:
    result: list[Path] = []
    root = stage.resolve()
    for path in stage.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if root not in resolved.parents:
            raise RuntimeError("staged update escaped its directory")
        result.append(path)
    return result


def _managed_files(path: Path) -> set[Path]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    raw_files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(raw_files, list):
        return set()
    result: set[Path] = set()
    for raw in raw_files:
        relative = Path(str(raw or "").replace("\\", "/"))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            continue
        if relative.name in PROTECTED_NAMES or any(part in PROTECTED_PARTS for part in relative.parts):
            continue
        result.add(relative)
    return result


def _health_check(target: Path, expected_version: str) -> None:
    version_path = target / "version.json"
    try:
        installed_version = str(json.loads(version_path.read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("updated version metadata is invalid") from exc
    if installed_version != expected_version:
        raise RuntimeError(f"updated version mismatch: expected {expected_version}, got {installed_version or 'empty'}")
    core_files = [target / name for name in ("client_agent.py", "desktop_app.py", "client_update.py")]
    missing = [path.name for path in core_files if not path.is_file()]
    if missing:
        raise RuntimeError("updated runtime is incomplete: " + ", ".join(missing))
    completed = subprocess.run(
        [sys.executable, "-m", "py_compile", *[str(path) for path in core_files]],
        cwd=str(target),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    if completed.returncode != 0:
        raise RuntimeError("updated runtime health-check failed: " + (completed.stderr or "compile error")[-500:])


def _write_result(target: Path, command_id: int | None, ok: bool, message: str) -> None:
    if command_id is None:
        return
    result_path = target / ".command-results.json"
    try:
        rows = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else []
    except (OSError, ValueError, TypeError):
        rows = []
    if not isinstance(rows, list):
        rows = []
    rows = [row for row in rows if int(row.get("id", -1)) != command_id]
    rows.append({"id": command_id, "ok": ok, "message": message[:1000], "details": {}})
    result_path.write_text(json.dumps(rows[-50:], ensure_ascii=False, indent=2), encoding="utf-8")


def _restart(target: Path, restart_target: str, minimized: bool) -> subprocess.Popen[bytes] | None:
    if restart_target == "none":
        return None
    if restart_target == "desktop":
        args = [sys.executable, str(target / "desktop_app.py")]
        if minimized:
            args.append("--minimized")
    else:
        args = [sys.executable, str(target / "client_agent.py")]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" and minimized else 0
    return subprocess.Popen(args, cwd=str(target), creationflags=creationflags)


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.updating")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_update_state(path: Path, phase: str, message: str, **details: object) -> None:
    payload = {
        "pid": os.getpid(),
        "phase": phase,
        "message": message,
        "updated_at": time.time(),
        **details,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_update_result(target: Path, ok: bool, message: str, **details: object) -> None:
    result = target / ".updates" / ".last-result.json"
    _write_update_state(result, "done" if ok else "rollback", message, ok=ok, finished_at=time.time(), **details)


def main() -> int:
    parser = argparse.ArgumentParser(description="XASS PC client update helper")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--restart-target", choices=["agent", "desktop", "none"], default="agent")
    parser.add_argument("--command-id", type=int)
    parser.add_argument("--minimized", action="store_true")
    args = parser.parse_args()

    stage = Path(args.stage).resolve()
    target = Path(args.target).resolve()
    marker = target / ".updates" / ".in-progress"
    if not stage.is_dir() or not target.is_dir():
        marker.unlink(missing_ok=True)
        return 2
    try:
        if not _wait_for_exit(args.pid):
            raise RuntimeError(f"process {args.pid} still blocks the update after 20 seconds")
    except Exception:
        marker.unlink(missing_ok=True)
        return 2

    backup = target / ".updates" / f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=True)
    revision_path = target / ".installed-revision"
    previous_revision = revision_path.read_bytes() if revision_path.is_file() else None
    copied: list[Path] = []
    created: list[Path] = []
    try:
        _write_update_state(marker, "installing", "Копирование новой версии", version=args.version, revision=args.revision)
        previous_managed = _managed_files(target / MANAGED_FILES_NAME)
        next_managed = _managed_files(stage / MANAGED_FILES_NAME)
        for source in _safe_files(stage):
            relative = source.relative_to(stage)
            destination = target / relative
            if destination.exists():
                backup_path = backup / relative
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup_path)
            else:
                created.append(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_copy(source, destination)
            copied.append(relative)

        for relative in sorted(previous_managed - next_managed, key=lambda item: item.as_posix(), reverse=True):
            destination = target / relative
            if not destination.is_file():
                continue
            backup_path = backup / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup_path)
            destination.unlink()
            copied.append(relative)

        requirements = target / "requirements.txt"
        if requirements.exists() and _requirements_need_install(target, requirements):
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--retries",
                    "2",
                    "--timeout",
                    "30",
                    "-r",
                    str(requirements),
                ],
                cwd=str(target),
                check=True,
                timeout=180,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
            stamp = target / ".venv" / ".requirements.sha256"
            if stamp.parent.is_dir():
                stamp.write_text(_requirements_digest(requirements), encoding="utf-8")
        _write_update_state(marker, "health-check", "Проверка новой версии", version=args.version, revision=args.revision)
        _health_check(target, args.version)
        (target / ".installed-revision").write_text(args.revision, encoding="utf-8")
        _write_result(target, args.command_id, True, f"Обновлено до {args.version}")
    except Exception as exc:
        for relative in reversed(copied):
            saved = backup / relative
            destination = target / relative
            if saved.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                _atomic_copy(saved, destination)
        for relative in reversed(created):
            destination = target / relative
            if destination.is_file():
                destination.unlink()
        if previous_revision is None:
            revision_path.unlink(missing_ok=True)
        else:
            revision_path.write_bytes(previous_revision)
        _write_result(target, args.command_id, False, f"Ошибка обновления: {exc}")
        _write_update_result(target, False, f"Ошибка обновления: {exc}", version=args.version, revision=args.revision)
        marker.unlink(missing_ok=True)
        _restart(target, args.restart_target, args.minimized)
        return 1

    shutil.rmtree(stage.parent, ignore_errors=True)
    backups = sorted((target / ".updates").glob("backup-*"), reverse=True)
    for old_backup in backups[1:]:
        if old_backup.is_dir():
            shutil.rmtree(old_backup, ignore_errors=True)
    _write_update_state(marker, "restarting", "Запуск новой версии", version=args.version, revision=args.revision)
    restarted = _restart(target, args.restart_target, args.minimized)
    if restarted is not None:
        try:
            restarted.wait(timeout=3)
            for relative in reversed(copied):
                saved = backup / relative
                destination = target / relative
                if saved.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_copy(saved, destination)
            for relative in reversed(created):
                destination = target / relative
                if destination.is_file():
                    destination.unlink()
            if previous_revision is None:
                revision_path.unlink(missing_ok=True)
            else:
                revision_path.write_bytes(previous_revision)
            _restart(target, args.restart_target, args.minimized)
            _write_update_result(target, False, "Новая версия завершилась сразу после запуска", version=args.version)
            marker.unlink(missing_ok=True)
            return 1
        except subprocess.TimeoutExpired:
            pass
    if backup.is_dir():
        shutil.rmtree(backup, ignore_errors=True)
    _write_update_result(target, True, f"Обновлено до {args.version}", version=args.version, revision=args.revision)
    marker.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
