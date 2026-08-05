from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
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


def install_update(installer: Path, wait_pid: int) -> int:
    if os.name != "nt" or not installer.is_file():
        return 1
    _wait_for_process(wait_pid)
    try:
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
            return completed.returncode or 1
        installed_exe = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Programs" / "XASS" / "XASS.exe"
        if installed_exe.is_file():
            subprocess.Popen([str(installed_exe)], cwd=str(installed_exe.parent), close_fds=True)
        return 0
    except OSError:
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a verified XASS installer update")
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--wait-pid", type=int, default=0)
    args = parser.parse_args()
    return install_update(args.installer, args.wait_pid)


if __name__ == "__main__":
    raise SystemExit(main())
