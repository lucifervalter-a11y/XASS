"""Native-test installer coordinator with verified file backup and rollback.

Runs from a copied companion runtime outside the directory being replaced.
Never reads agent credentials, never invokes the legacy Tk updater, and never
modifies the agent data or native preferences. Failed runtimes are quarantined,
not deleted. All actions require a native UI-created bounded request.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import contextlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from installer_process import InstallerProcessError, run_installer

APP_ID = "B4D7E8B9-9C58-4C36-A432-D114393006D8"
REQUEST_KEYS = {"schema", "job_id", "install_root", "installer", "sha256", "size", "version", "revision", "parent_pid", "parent_created", "automatic"}
ACTIVE = {"preparing", "backing-up", "ready", "installing", "health-check", "rolling-back", "rollback-health"}


def object_file(path: Path, maximum: int = 1024 * 1024) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > maximum:
        raise ValueError("Invalid local manifest")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict): raise ValueError("Invalid manifest object")
    return value


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, allow_nan=False); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): value.update(chunk)
    return value.hexdigest()


def files(root: Path):
    for path in root.rglob("*"):
        # Junctions/reparse points can escape roots on Windows. Reject them even
        # when pathlib.is_symlink alone does not identify that reparse type.
        if path.is_symlink() or (getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Reparse points are not valid installed payload")
        if path.is_file():
            if not path.resolve().is_relative_to(root.resolve()): raise ValueError("Unsafe payload path")
            yield path


def copy_tree_verified(source: Path, destination: Path) -> dict:
    if destination.exists(): raise ValueError("Backup destination already exists")
    destination.mkdir(parents=True)
    inventory = {}
    for path in files(source):
        relative = path.relative_to(source).as_posix(); target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, target)
        expected = digest(path)
        if digest(target) != expected: raise ValueError("Backup checksum mismatch")
        inventory[relative] = {"sha256": expected, "size": target.stat().st_size}
    if not inventory: raise ValueError("Empty installation")
    return inventory


def verify_tree(root: Path, inventory: dict):
    for relative, record in inventory.items():
        part = Path(relative)
        if part.is_absolute() or ".." in part.parts: raise ValueError("Unsafe backup entry")
        path = root / part
        if not path.resolve().is_relative_to(root.resolve()): raise ValueError("Unsafe backup path")
        if not path.is_file() or path.is_symlink() or path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
            raise ValueError("Backup integrity check failed")


class NativeUpdater:
    def __init__(self, request_path: Path, *, popen=subprocess.Popen, run=subprocess.run, sleep=time.sleep, clock=time.monotonic, installer_run=None):
        self.request_path = request_path.resolve(strict=True); self.job = self.request_path.parent
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "XASS.Native" / "updates"
        if self.job.parent.resolve() != root.resolve() or not re.fullmatch(r"job-[a-f0-9]{32}", self.job.name):
            raise ValueError("Untrusted update job location")
        self.request = object_file(self.request_path, 16384)
        if set(self.request) != REQUEST_KEYS or self.request["schema"] != 1:
            raise ValueError("Unknown update request schema")
        r = self.request
        if r["job_id"] != self.job.name or type(r["automatic"]) is not bool or type(r["parent_pid"]) is not int or r["parent_pid"] <= 0:
            raise ValueError("Invalid update request")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", str(r["sha256"])) or not re.fullmatch(r"[a-fA-F0-9]{40}", str(r["revision"])):
            raise ValueError("Invalid release checksum/revision")
        if isinstance(r["parent_created"], bool) or not isinstance(r["parent_created"], (int, float)) or not math.isfinite(r["parent_created"]) or r["parent_created"] <= 0:
            raise ValueError("Invalid parent identity")
        if type(r["size"]) is not int or not 1024 <= r["size"] <= 8 * 1024**3:
            raise ValueError("Invalid installer size")
        self.install = Path(r["install_root"])
        self.installer = Path(r["installer"])
        if not self.install.is_absolute() or not self.installer.is_absolute(): raise ValueError("Absolute paths required")
        self.install = self.install.resolve(strict=True); self.installer = self.installer.resolve(strict=True)
        if self.install.is_symlink() or getattr(self.install.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Unsafe installation root")
        if self.install in {self.job, self.job.parent} or self.install.is_relative_to(self.job) or self.job.is_relative_to(self.install):
            raise ValueError("Update workspace overlaps installation")
        expected_names = {"XASS-Native-Test-" + r["revision"] + ".exe": "native-test",
                          "XASS-Native-" + r["revision"] + ".exe": "native"}
        if self.installer.parent != root.resolve() or self.installer.name not in expected_names:
            raise ValueError("Installer outside verified update folder")
        data = root.parent.parent / "XASS"
        if self.install == data or self.install.is_relative_to(data) or data.is_relative_to(self.install):
            raise ValueError("Installation overlaps agent data")
        self.previous = object_file(self.install / "native-install.json", 16384)
        self.release_distribution = expected_names[self.installer.name]
        if self.previous.get("app_id") != APP_ID or self.previous.get("distribution") not in {"native-test", "native"}:
            raise ValueError("Not a native installation")
        if self.previous.get("distribution") == "native" and self.release_distribution != "native":
            raise ValueError("A stable installation cannot move to the test channel")
        if not re.fullmatch(r"[a-fA-F0-9]{40}", str(self.previous.get("revision") or "")):
            raise ValueError("Installed revision is unknown")
        if self.installer.stat().st_size != r["size"] or digest(self.installer) != r["sha256"].lower():
            raise ValueError("Installer verification failed")
        self.backup = self.job / "backup"
        self.inventory = {}
        self.popen, self.run, self.sleep, self.clock = popen, run, sleep, clock
        # Preserve the existing explicit test-runner injection. Production always
        # supervises the installer tree; subprocess.run remains for health checks.
        self.installer_run = installer_run if installer_run is not None else (run_installer if run is subprocess.run else run)
        self.installer_shutdown_confirmed = True
        self.applied = False
        self.registry = {}

    def state(self, phase: str, message: str, **extra):
        write_json(self.job / "state.json", {"phase":phase,"message":message,"updated_at":time.time(),"pid":os.getpid(),
            "version":self.request["version"],"revision":self.request["revision"],"sha256":self.request["sha256"], **extra})
        write_json(self.job.parent / "last-result.json", {"phase":phase,"message":message,"updated_at":time.time(),
            "version":self.request["version"],"revision":self.request["revision"],"sha256":self.request["sha256"],"job_id":self.job.name, **extra})
    def check_cancel(self):
        if (self.job / "cancel").exists(): raise InterruptedError("Update cancelled before installation")

    def prepare(self):
        self.state("preparing", "Проверка установленной версии и свободного места")
        previous_payload = object_file(self.install / "payload-manifest.json", 16 * 1024 * 1024)
        if previous_payload.get("revision") != self.previous["revision"] or not isinstance(previous_payload.get("files"), list):
            raise ValueError("Current payload manifest is unavailable")
        verify_tree(self.install, {r["path"]: {"size":r["bytes"], "sha256":r["sha256"]} for r in previous_payload["files"]})
        required = sum(p.stat().st_size for p in files(self.install))
        if shutil.disk_usage(self.job).free < required * 2 + 256 * 1024**2:
            raise OSError("Insufficient space for verified backup and rollback")
        self.registry = self.read_uninstall_registration()
        write_json(self.job / "uninstall-registration.json", self.registry)
        self.check_cancel()
        self.state("backing-up", "Создание проверенной резервной копии программы")
        self.inventory = copy_tree_verified(self.install, self.backup)
        write_json(self.job / "backup-manifest.json", {"schema":1,"revision":self.previous["revision"],"files":self.inventory})
        self.check_cancel()
        write_json(self.job / "ready.json", {"ready":True,"job_id":self.job.name})
        self.state("ready", "Резервная копия проверена. Ожидание завершения XASS")

    def read_uninstall_registration(self):
        if os.name != "nt": return {}
        import winreg
        values = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{" + APP_ID + "}_is1") as key:
                for name in ("DisplayVersion", "DisplayName", "InstallDate", "InstallLocation", "UninstallString", "QuietUninstallString"):
                    try:
                        value, kind = winreg.QueryValueEx(key, name)
                        if kind == winreg.REG_SZ and isinstance(value, str): values[name] = value
                    except FileNotFoundError: pass
        except FileNotFoundError: pass
        return values

    def restore_uninstall_registration(self):
        if os.name != "nt" or not self.registry: return
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{" + APP_ID + "}_is1", 0, winreg.KEY_SET_VALUE) as key:
            for name, value in self.registry.items():
                if name in {"DisplayVersion", "DisplayName", "InstallDate", "InstallLocation", "UninstallString", "QuietUninstallString"}:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def wait_for_parent(self):
        import psutil
        deadline = self.clock() + 120
        while self.clock() < deadline:
            self.check_cancel()
            try:
                parent = psutil.Process(self.request["parent_pid"])
                if abs(parent.create_time() - float(self.request["parent_created"])) > 1: return
                if not parent.is_running(): return
            except psutil.NoSuchProcess: return
            self.sleep(0.2)
        raise TimeoutError("Native UI did not exit")

    def stop_install_processes(self):
        import psutil
        owned = []
        for process in psutil.process_iter(["pid", "exe", "create_time"]):
            try:
                executable = Path(process.info.get("exe") or "")
                if process.pid == os.getpid() or not executable.is_absolute(): continue
                if executable.resolve().is_relative_to(self.install):
                    # PIDs are used only with the process object/create-time from
                    # this enumeration, never a stale request's arbitrary PID.
                    process.terminate(); owned.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError): continue
        _, live = psutil.wait_procs(owned, timeout=8)
        for process in live:
            try: process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied): pass
        _, live = psutil.wait_procs(live, timeout=4)
        if live: raise RuntimeError("Installed process refused to stop")

    def install_release(self):
        self.state("installing", "Установка проверенного нативного пакета")
        self.stop_install_processes()
        self.applied = True
        self.installer_shutdown_confirmed = False
        try:
            result = self.installer_run([str(self.installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/CLOSEAPPLICATIONS",
                                       "/DIR=" + str(self.install), "/LOG=" + str(self.job / "installer.log")],
                                      cwd=self.installer.parent, timeout=900)
        except InstallerProcessError as error:
            self.installer_shutdown_confirmed = error.shutdown_confirmed is True
            raise
        # Returning from the supervisor establishes that every job process exited,
        # including temporary Setup children outside the installation directory.
        self.installer_shutdown_confirmed = True
        if result.returncode != 0: raise RuntimeError("Installer failed")

    def verify_installed(self, revision: str):
        marker = object_file(self.install / "native-install.json", 16384)
        expected_distribution = self.previous["distribution"] if revision == self.previous["revision"] else self.release_distribution
        if marker.get("app_id") != APP_ID or marker.get("distribution") != expected_distribution or marker.get("revision") != revision:
            raise ValueError("Installed identity/revision mismatch")
        payload = object_file(self.install / "payload-manifest.json", 16 * 1024 * 1024)
        if payload.get("revision") != revision or not isinstance(payload.get("files"), list):
            raise ValueError("Payload manifest revision mismatch")
        expected = {row["path"]: {"sha256": row["sha256"], "size": row["bytes"]} for row in payload["files"]}
        verify_tree(self.install, expected)
        helper = self.install / "runtime" / "XASS.NativeHelper.exe"
        native = self.install / "Xass.Native.exe"
        if not helper.is_file() or not native.is_file(): raise ValueError("Installed executable is missing")
        result = self.run([str(helper), "--health-check"], cwd=self.install, capture_output=True, timeout=90,
                          creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if result.returncode != 0: raise RuntimeError("Companion health check failed")
        output = getattr(result, "stdout", b"")
        if len(output) > 65536 or json.loads(output).get("ok") is not True:
            raise RuntimeError("Companion health response invalid")
        # The UI acknowledgment proves XAML load, revision and backend protocol
        # readiness. Process launch alone does not establish success.
        nonce = uuid.uuid4().hex
        acknowledgment = self.job / ("health-" + nonce + ".json")
        child = self.popen([str(native), "--minimized", "--native-update-health", str(self.job), nonce], cwd=self.install)
        deadline = self.clock() + 60
        while self.clock() < deadline:
            if child.poll() is not None: raise RuntimeError("Native UI exited during health check")
            if acknowledgment.is_file():
                value = object_file(acknowledgment, 4096)
                if value.get("nonce") == nonce and value.get("ready") is True and value.get("revision") == revision and value.get("pid") == child.pid:
                    return
            self.sleep(0.25)
        raise TimeoutError("Native UI did not acknowledge readiness")

    def restore(self):
        if not self.installer_shutdown_confirmed:
            raise InstallerProcessError("Cannot restore while installer shutdown is unconfirmed")
        self.state("rolling-back", "Проверка резервной копии и восстановление предыдущей версии")
        verify_tree(self.backup, self.inventory)
        self.stop_install_processes()
        restored = self.install.with_name(self.install.name + ".restore-" + uuid.uuid4().hex)
        restored_inventory = copy_tree_verified(self.backup, restored)
        if restored_inventory != self.inventory: raise ValueError("Rollback inventory mismatch")
        quarantine = self.install.with_name(self.install.name + ".failed-" + uuid.uuid4().hex)
        self.install.rename(quarantine)
        try: restored.rename(self.install)
        except Exception:
            quarantine.rename(self.install)
            raise
        write_json(self.job / "quarantine.json", {"path":str(quarantine),"reason":"failed-native-update","original_data_retained":True})
        self.restore_uninstall_registration()
        self.state("rollback-health", "Предыдущая версия восстановлена. Проверка запуска")
        self.verify_installed(self.previous["revision"])

    def execute(self):
        try:
            self.prepare(); self.wait_for_parent(); self.install_release()
            self.state("health-check", "Проверка нативного приложения, среды и агента")
            self.verify_installed(self.request["revision"])
            self.state("completed", "Нативное обновление установлено и проверено", ok=True)
            return 0
        except InterruptedError:
            self.state("cancelled", "Обновление отменено до установки. Текущая версия сохранена", ok=False)
            return 2
        except Exception:
            if self.applied:
                if not self.installer_shutdown_confirmed:
                    self.state("rollback-failed", "Остановка установщика не подтверждена. Восстановление и запуск заблокированы; проверенная резервная копия сохранена", ok=False, rollback_ok=False,
                               rollback_blocked=True, installer_shutdown_confirmed=False, rejected_sha256=self.request["sha256"])
                    return 4
                try:
                    self.restore()
                    self.state("rolled-back", "Обновление не прошло проверку. Предыдущая версия восстановлена и запущена", ok=False, rollback_ok=True, rejected_sha256=self.request["sha256"])
                    return 3
                except Exception:
                    self.state("rollback-failed", "Автоматическое восстановление не завершено. Проверенная резервная копия сохранена; требуется ручное восстановление", ok=False, rollback_ok=False, rejected_sha256=self.request["sha256"])
                    return 4
            self.state("failed", "Обновление не началось. Установленная версия и данные сохранены", ok=False)
            return 1


@contextlib.contextmanager
def update_lock(path: Path):
    with path.open("a+b") as handle:
        if handle.tell() == 0: handle.write(b"0"); handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try: yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--request", type=Path, required=True); args = parser.parse_args()
    if os.name != "nt": raise SystemExit("Native installation requires Windows")
    try:
        updater = NativeUpdater(args.request)
        # This role must execute from a copied helper outside its install root.
        if Path(sys.executable).resolve().is_relative_to(updater.install): raise ValueError("Updater still inside installation")
        with update_lock(updater.job.parent / ".native-update.lock"):
            raise SystemExit(updater.execute())
    except SystemExit: raise
    except Exception: raise SystemExit(1) from None

if __name__ == "__main__": main()
