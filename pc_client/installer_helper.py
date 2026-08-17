from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import psutil


def _wait_for_process(pid: int, timeout_sec: int = 20) -> bool:
    if pid <= 0 or os.name != "nt":
        return True
    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, max(0, int(timeout_sec * 1000))) == 0
    finally:
        kernel32.CloseHandle(handle)


def _install_paths() -> tuple[Path, Path]:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "Programs" / "XASS", local / "XASS" / ".updates"


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_state(updates_root: Path, phase: str, message: str, **details: object) -> None:
    _atomic_json(
        updates_root / ".in-progress",
        {
            "pid": os.getpid(),
            "phase": phase,
            "message": message,
            "updated_at": time.time(),
            **details,
        },
    )


def _write_result(updates_root: Path, ok: bool, message: str, **details: object) -> None:
    _atomic_json(
        updates_root / ".last-result.json",
        {"ok": ok, "message": message[:1000], "finished_at": time.time(), **details},
    )


def _stop_installed_processes(installed_root: Path, *, exclude_pid: int = 0) -> list[int]:
    executable = str((installed_root / "XASS.exe").resolve()).casefold()
    targets: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            if process.pid in {os.getpid(), exclude_pid}:
                continue
            if str(process.info.get("exe") or "").casefold() != executable:
                continue
            process.terminate()
            targets.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    _gone, alive = psutil.wait_procs(targets, timeout=6)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return [process.pid for process in targets]


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


def _runtime_is_healthy(installed_exe: Path, expected_version: str = "") -> bool:
    try:
        args = [str(installed_exe), "--health-check"]
        if expected_version:
            args.extend(["--expected-version", expected_version])
        completed = subprocess.run(
            args,
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


def _launch_and_verify(installed_exe: Path) -> bool:
    try:
        process = subprocess.Popen(
            [str(installed_exe), "--minimized"],
            cwd=str(installed_exe.parent),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            process.wait(timeout=3)
            return False
        except subprocess.TimeoutExpired:
            return True
    except OSError:
        return False


def _restore_and_launch(installed_root: Path, backup: Path | None) -> bool:
    if not _restore_installed_runtime(installed_root, backup):
        return False
    previous = installed_root / "XASS.exe"
    subprocess.Popen([str(previous)], cwd=str(previous.parent), close_fds=True)
    return True


def install_update(
    installer: Path,
    wait_pid: int,
    expected_version: str = "",
    expected_revision: str = "",
) -> int:
    if os.name != "nt" or not installer.is_file():
        return 1
    installed_root, updates_root = _install_paths()
    updates_root.mkdir(parents=True, exist_ok=True)
    marker = updates_root / ".in-progress"
    _write_state(updates_root, "restarting", "Остановка старой версии", version=expected_version)
    if not _wait_for_process(wait_pid):
        try:
            process = psutil.Process(wait_pid)
            process.terminate()
            process.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
        except psutil.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=3)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired, OSError):
                pass
    stopped = _stop_installed_processes(installed_root)
    backup: Path | None = None
    revision_path = updates_root.parent / ".installed-revision"
    previous_revision = revision_path.read_bytes() if revision_path.is_file() else None
    try:
        _write_state(
            updates_root,
            "installing",
            "Создание резервной копии",
            version=expected_version,
            stopped_processes=stopped,
        )
        backup = _backup_installed_runtime(installed_root, updates_root)
        _write_state(updates_root, "installing", "Установка новой версии", version=expected_version)
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
            timeout=180,
        )
        if completed.returncode not in {0, 1641, 3010}:
            _restore_and_launch(installed_root, backup)
            _write_result(updates_root, False, f"Установщик завершился с кодом {completed.returncode}", phase="rollback")
            marker.unlink(missing_ok=True)
            return completed.returncode or 1
        installed_exe = installed_root / "XASS.exe"
        _write_state(updates_root, "health-check", "Проверка новой версии", version=expected_version)
        if not installed_exe.is_file() or not _runtime_is_healthy(installed_exe, expected_version):
            _restore_and_launch(installed_root, backup)
            _write_result(updates_root, False, "Новая версия не прошла локальную проверку", phase="rollback")
            marker.unlink(missing_ok=True)
            return 1
        if expected_revision:
            revision_path.write_text(expected_revision, encoding="utf-8")
        _write_state(updates_root, "restarting", "Запуск новой версии", version=expected_version)
        if not _launch_and_verify(installed_exe):
            if previous_revision is None:
                revision_path.unlink(missing_ok=True)
            else:
                revision_path.write_bytes(previous_revision)
            _restore_and_launch(installed_root, backup)
            _write_result(updates_root, False, "Новая версия завершилась сразу после запуска", phase="rollback")
            marker.unlink(missing_ok=True)
            return 1
        if backup and backup.is_dir():
            shutil.rmtree(backup, ignore_errors=True)
        backups = sorted(updates_root.glob("installer-backup-*"), reverse=True)
        for old in backups[1:]:
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        _write_result(
            updates_root,
            True,
            f"XASS обновлён до {expected_version or 'новой версии'}",
            phase="done",
            version=expected_version,
            revision=expected_revision,
        )
        marker.unlink(missing_ok=True)
        return 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        if previous_revision is None:
            revision_path.unlink(missing_ok=True)
        else:
            revision_path.write_bytes(previous_revision)
        _restore_and_launch(installed_root, backup)
        _write_result(updates_root, False, f"Ошибка установки: {exc}", phase="rollback")
        marker.unlink(missing_ok=True)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a verified XASS installer update")
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--wait-pid", type=int, default=0)
    parser.add_argument("--expected-version", default="")
    parser.add_argument("--expected-revision", default="")
    args = parser.parse_args()
    return install_update(args.installer, args.wait_pid, args.expected_version, args.expected_revision)


if __name__ == "__main__":
    raise SystemExit(main())
