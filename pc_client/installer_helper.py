from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


def _wait_for_process(pid: int, timeout_sec: int = 90) -> None:
    if pid <= 0 or os.name != "nt":
        return
    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject(handle, max(0, int(timeout_sec * 1000)))
    finally:
        kernel32.CloseHandle(handle)


def _install_paths() -> tuple[Path, Path]:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "Programs" / "XASS", local / "XASS" / ".updates"


def _backup_installed_runtime(installed_root: Path, updates_root: Path) -> Path | None:
    if not (installed_root / "XASS.exe").is_file():
        return None
    updates_root.mkdir(parents=True, exist_ok=True)
    backup = updates_root / f"installer-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(installed_root, backup)
    return backup


def _restore_installed_runtime(installed_root: Path, backup: Path | None) -> bool:
    if backup is None or not (backup / "XASS.exe").is_file():
        return False
    if installed_root.name != "XASS" or installed_root.parent.name != "Programs":
        return False
    shutil.rmtree(installed_root, ignore_errors=True)
    shutil.copytree(backup, installed_root)
    return True


def _runtime_is_healthy(installed_exe: Path) -> bool:
    try:
        completed = subprocess.run(
            [str(installed_exe), "--health-check"],
            cwd=str(installed_exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _restore_and_launch(installed_root: Path, backup: Path | None) -> bool:
    if not _restore_installed_runtime(installed_root, backup):
        return False
    previous = installed_root / "XASS.exe"
    subprocess.Popen([str(previous)], cwd=str(previous.parent), close_fds=True)
    return True


def install_update(installer: Path, wait_pid: int) -> int:
    if os.name != "nt" or not installer.is_file():
        return 1
    _wait_for_process(wait_pid)
    installed_root, updates_root = _install_paths()
    backup: Path | None = None
    try:
        backup = _backup_installed_runtime(installed_root, updates_root)
        completed = subprocess.run(
            [
                str(installer),
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                "/CLOSEAPPLICATIONS",
            ],
            cwd=str(installer.parent),
            check=False,
        )
        if completed.returncode not in {0, 1641, 3010}:
            _restore_and_launch(installed_root, backup)
            return completed.returncode or 1
        installed_exe = installed_root / "XASS.exe"
        if not installed_exe.is_file() or not _runtime_is_healthy(installed_exe):
            _restore_and_launch(installed_root, backup)
            return 1
        subprocess.Popen([str(installed_exe)], cwd=str(installed_exe.parent), close_fds=True)
        backups = sorted(updates_root.glob("installer-backup-*"), reverse=True)
        for old in backups[2:]:
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        return 0
    except OSError:
        _restore_and_launch(installed_root, backup)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a verified XASS installer update")
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--wait-pid", type=int, default=0)
    args = parser.parse_args()
    return install_update(args.installer, args.wait_pid)


if __name__ == "__main__":
    raise SystemExit(main())
