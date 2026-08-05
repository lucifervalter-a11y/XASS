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


def _requirements_digest(requirements: Path) -> str:
    return hashlib.sha256(requirements.read_bytes()).hexdigest()


def _requirements_need_install(target: Path, requirements: Path) -> bool:
    stamp = target / ".venv" / ".requirements.sha256"
    try:
        return stamp.read_text(encoding="utf-8").strip() != _requirements_digest(requirements)
    except OSError:
        return True


def _wait_for_exit(pid: int, timeout: int = 45) -> None:
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
            return
        try:
            kernel32.WaitForSingleObject(handle, max(0, timeout) * 1000)
        finally:
            kernel32.CloseHandle(handle)
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.5)


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


def _restart(target: Path, restart_target: str, minimized: bool) -> None:
    if restart_target == "none":
        return
    if restart_target == "desktop":
        args = [sys.executable, str(target / "desktop_app.py")]
        if minimized:
            args.append("--minimized")
    else:
        args = [sys.executable, str(target / "client_agent.py")]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" and minimized else 0
    subprocess.Popen(args, cwd=str(target), creationflags=creationflags)


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
        _wait_for_exit(args.pid)
    except Exception:
        marker.unlink(missing_ok=True)
        return 2

    backup = target / ".updates" / f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    created: list[Path] = []
    try:
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
            shutil.copy2(source, destination)
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
            )
            stamp = target / ".venv" / ".requirements.sha256"
            if stamp.parent.is_dir():
                stamp.write_text(_requirements_digest(requirements), encoding="utf-8")
        (target / ".installed-revision").write_text(args.revision, encoding="utf-8")
        _write_result(target, args.command_id, True, f"Обновлено до {args.version}")
    except Exception as exc:
        for relative in reversed(copied):
            saved = backup / relative
            destination = target / relative
            if saved.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved, destination)
        for relative in reversed(created):
            destination = target / relative
            if destination.is_file():
                destination.unlink()
        _write_result(target, args.command_id, False, f"Ошибка обновления: {exc}")
        marker.unlink(missing_ok=True)
        _restart(target, args.restart_target, args.minimized)
        return 1

    shutil.rmtree(stage.parent, ignore_errors=True)
    backups = sorted((target / ".updates").glob("backup-*"), reverse=True)
    for old_backup in backups[3:]:
        if old_backup.is_dir():
            shutil.rmtree(old_backup, ignore_errors=True)
    marker.unlink(missing_ok=True)
    _restart(target, args.restart_target, args.minimized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
