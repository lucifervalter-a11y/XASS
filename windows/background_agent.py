"""Native-owned persistent desktop backend. Parent owns process and closes stdin.

Unlike the one-shot bridge, this host owns its agent child, playback and durable
archive jobs. It never opens Tk and never creates detached unmanaged workers.
"""
from __future__ import annotations
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from desktop_bridge import DesktopService, bind_runtime, config_lock, read_json, redact, text

MAX_LINE = 96 * 1024
MAX_OUTPUT = 512 * 1024
HOST_SCHEMA = {
    "host_status": set(), "host_start": set(), "host_restart": set(), "host_stop": set(), "host_quit": set(),
    "host_archive_move": {"path", "copy", "confirmed"}, "host_archive_cancel": set(),
    "host_archive_cleanup": {"confirmed"},
}


class DesktopHost:
    def __init__(self, source: Path, data: Path):
        self.source, self.data = bind_runtime(source, data)
        self.service = DesktopService(self.source, self.data)
        self.process = None
        self.external = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.recovery_attempts = 0
        self.next_start = 0.0
        self.wanted = True
        self.state = "starting"
        self.music = None
        self.job = {"state": "idle", "progress": 0, "message": ""}
        self.job_cancel = threading.Event()
        self.job_thread = None
        self.supervisor = threading.Thread(target=self._supervise, daemon=True)

    def _agent_identity(self):
        import psutil
        raw = read_json(self.data / ".agent-status.json")
        try:
            p = psutil.Process(int(raw.get("process_id") or 0))
            command = p.cmdline()
            joined = " ".join(command).lower()
            if not p.is_running() or not any(x in joined for x in ("client_agent.py", "--agent-child", "--agent")):
                return None
            if float(raw.get("updated_at") or 0) < p.create_time() - 1:
                return None
            # Existing helper data argument must match; old installed agent uses
            # the single legacy data root and status path checked above.
            if "--data" in command:
                value = command[command.index("--data") + 1]
                if Path(value).resolve() != self.data:
                    return None
            return p
        except (psutil.Error, ValueError, TypeError, IndexError):
            return None

    def start(self):
        with self.lock:
            self.wanted = True
            if self.process is not None and self.process.poll() is None:
                return
            existing = self._agent_identity()
            if existing:
                self.external = (existing.pid, existing.create_time())
                self.state = "running"
                return
            self.external = None
            config = self.service.config()
            if not config.get("api_key") or not config.get("server_url"):
                self.state = "unpaired"; return
            marker = read_json(self.data / ".updates" / ".in-progress")
            if marker and time.time() - float(marker.get("updated_at") or 0) < 60:
                self.state = "updating"; return
            args = [sys.executable]
            if getattr(sys, "frozen", False):
                args += ["--role", "background-agent"]
            else:
                args += ["-I", "-B", str(Path(__file__).resolve())]
            args += ["--agent-child", "--source", str(self.source), "--data", str(self.data)]
            env = dict(os.environ, XASS_DATA_ROOT=str(self.data), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            self.process = subprocess.Popen(args, cwd=self.data, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.started = time.monotonic()
            self.state = "connecting"
            threading.Thread(target=self._logs, args=(self.process,), daemon=True).start()

    def _logs(self, process):
        from runtime_state import append_log
        for line in process.stdout:
            append_log(redact(line))

    def stop(self):
        import psutil
        with self.lock:
            self.wanted = False
            process, self.process = self.process, None
            identity, self.external = self.external, None
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)
            elif identity:
                try:
                    p = psutil.Process(identity[0])
                    if abs(p.create_time() - identity[1]) < 0.01 and self._agent_identity() is not None:
                        p.terminate()
                        try: p.wait(timeout=6)
                        except psutil.TimeoutExpired: p.kill(); p.wait(timeout=3)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            self.state = "stopped"

    def _supervise(self):
        while not self.stop_event.wait(1):
            with self.lock:
                if not self.wanted or self.job.get("state") in {"copying", "committing"}:
                    continue
                if self.process is not None and self.process.poll() is not None:
                    code = self.process.returncode
                    self.process = None
                    if time.monotonic() - getattr(self, "started", 0) > 90:
                        self.recovery_attempts = 0
                    if code not in {75, 76}:
                        self.recovery_attempts += 1
                    if self.recovery_attempts >= 5:
                        self.wanted = False; self.state = "crashed"; continue
                    self.next_start = time.monotonic() + min(30, 2 ** self.recovery_attempts)
                    self.state = "recovering"
                if self.process is None and time.monotonic() >= self.next_start:
                    try: self.start()
                    except Exception:
                        self.recovery_attempts += 1
                        self.next_start = time.monotonic() + min(30, 2 ** self.recovery_attempts)
                        self.state = "error"
                        if self.recovery_attempts >= 5: self.wanted = False

    def status(self):
        with self.lock:
            return {"state": self.state, "pid": self.process.pid if self.process and self.process.poll() is None else self.external[0] if self.external else 0,
                    "recoveries": self.recovery_attempts, "job": dict(self.job)}

    def request(self, action: str, request: dict):
        if action not in HOST_SCHEMA or set(request) - (HOST_SCHEMA[action] | {"id", "action", "version"}):
            raise ValueError("Unsupported host schema")
        if request.get("version", 1) != 1:
            raise ValueError("Unsupported version")
        if action == "host_status": return self.status()
        if action in {"host_start", "host_restart"}:
            if self.job.get("state") in {"copying", "committing", "cleaning"}:
                raise ValueError("Archive operation active")
            if action == "host_restart": self.stop()
            self.recovery_attempts = 0; self.start(); return self.status()
        if action == "host_stop":
            if self.job_thread is not None and self.job_thread.is_alive():
                raise ValueError("Cancel the archive operation before stopping the agent")
            self.stop(); return self.status()
        if action == "host_quit": self.close(); return {"stopped": True}
        if action == "host_archive_cancel":
            self.job_cancel.set(); return {"cancel_requested": True}
        if action == "host_archive_move":
            if request.get("confirmed") is not True or type(request.get("copy")) is not bool:
                raise ValueError("Archive move confirmation required")
            target = Path(text(request.get("path"), 32760, empty=False))
            if not target.is_absolute() or not target.is_dir():
                raise ValueError("Existing target required")
            if self.job_thread is not None and self.job_thread.is_alive():
                raise ValueError("Archive operation active")
            self.job_cancel.clear()
            self.job = {"state": "copying", "progress": 0, "message": "Подготовка архива"}
            self.job_thread = threading.Thread(target=self._move_archive, args=(target.resolve(), request["copy"]), daemon=True)
            self.job_thread.start(); return self.status()
        if action == "host_archive_cleanup":
            if request.get("confirmed") is not True:
                raise ValueError("Irreversible cleanup confirmation required")
            if self.job_thread is not None and self.job_thread.is_alive():
                raise ValueError("Archive operation active")
            from archive_store import cleanup_archive, archive_root, DB_FILE
            self.stop()
            try:
                config = self.service.config(); root = archive_root(config).resolve()
                database = root / DB_FILE
                if database.exists():
                    with sqlite3.connect(database) as conn:
                        for (filename,) in conn.execute("SELECT local_path FROM media WHERE saved=1 AND local_path<>''"):
                            if not Path(filename).resolve().is_relative_to(root):
                                raise ValueError("Archive contains out-of-root media")
                with config_lock(self.data):
                    result = cleanup_archive(config, force=True)
                return result
            finally:
                self.start()
        raise ValueError("Unsupported action")

    def _move_archive(self, target: Path, copy: bool):
        from archive_store import archive_root, DB_FILE
        from runtime_state import atomic_write_json
        stage = target / (".xass-transfer-" + uuid.uuid4().hex)
        try:
            self.stop()
            with config_lock(self.data):
                config = self.service.config(); source = archive_root(config).resolve()
                if source == target or target.is_relative_to(source) or source.is_relative_to(target):
                    raise ValueError("Archive target overlaps source")
                if (source / DB_FILE).is_symlink():
                    raise ValueError("Unsafe archive database")
                if any(target.iterdir()):
                    raise ValueError("Choose an empty archive folder")
                stage.mkdir()
                if copy and source.is_dir():
                    files = [p for p in source.rglob("*") if p.is_file() and not p.is_symlink()
                             and p.name not in {DB_FILE, DB_FILE + "-wal", DB_FILE + "-shm"}]
                    total = sum(p.stat().st_size for p in files)
                    if shutil.disk_usage(target).free < total + 64 * 1024**2:
                        raise OSError("Insufficient free space")
                    done = 0
                    for path in files:
                        if self.job_cancel.is_set(): raise InterruptedError()
                        if not path.resolve().is_relative_to(source): raise ValueError("Unsafe archive entry")
                        dest = stage / path.relative_to(source); dest.parent.mkdir(parents=True, exist_ok=True)
                        with path.open("rb") as src, dest.open("xb") as dst:
                            while chunk := src.read(1024 * 1024):
                                if self.job_cancel.is_set(): raise InterruptedError()
                                dst.write(chunk); done += len(chunk)
                                self.job.update(progress=int(done * 90 / max(1, total)))
                            dst.flush(); os.fsync(dst.fileno())
                    if (source / DB_FILE).exists():
                        with sqlite3.connect(source / DB_FILE) as src, sqlite3.connect(stage / DB_FILE) as dst:
                            src.backup(dst)
                            for row_id, filename in dst.execute("SELECT asset_id,local_path FROM media WHERE local_path<>''").fetchall():
                                old = Path(filename).resolve()
                                if not old.is_relative_to(source): raise ValueError("Unsafe archive media")
                                dst.execute("UPDATE media SET local_path=? WHERE asset_id=?", (str(target / old.relative_to(source)), row_id))
                            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise ValueError("Archive validation failed")
                            dst.commit()
                if self.job_cancel.is_set(): raise InterruptedError()
                self.job.update(state="committing", progress=95, message="Сохранение новой папки")
                for path in list(stage.iterdir()): path.rename(target / path.name)
                stage.rmdir()
                updated = dict(config, archive_folder=str(target)); self.service.save(updated)
                self.job = {"state": "completed", "progress": 100, "message": "Папка изменена. Исходный архив сохранён."}
        except InterruptedError:
            self.job = {"state": "cancelled", "progress": 0, "message": "Копирование отменено. Исходный архив сохранён."}
        except Exception:
            self.job = {"state": "error", "progress": 0, "message": "Не удалось перенести архив. Исходный архив и настройка сохранены; проверьте пустую папку и свободное место."}
        finally:
            if stage.exists(): shutil.rmtree(stage, ignore_errors=True)
            atomic_write_json(self.data / ".native-archive-job.json", self.job)
            if not self.stop_event.is_set():
                try: self.start()
                except Exception: self.state = "error"

    def close(self):
        self.stop_event.set(); self.job_cancel.set()
        if self.job_thread is not None and self.job_thread.is_alive():
            self.job_thread.join(timeout=10)
        if self.music is not None: self.music.close()
        self.stop()


def agent_child(source: Path, data: Path):
    bind_runtime(source, data)
    import client_agent
    from runtime_state import acquire_single_instance
    instance = acquire_single_instance("XASS-background-agent")
    if instance is None: return
    try:
        service = DesktopService(source, data)
        config = client_agent.ensure_minimal_defaults(service.config())
        if not config.get("api_key") or not config.get("server_url"):
            return
        # Preserve the user's stored preference. The stable/Tk feed is not a
        # compatible bundle; all updates go through the native update boundary.
        config = dict(config, auto_update=False, desktop_managed=True)
        def reject_legacy(*args, **kwargs):
            raise RuntimeError("Use the native test updater for this installation")
        client_agent._apply_update = reject_legacy
        client_agent._apply_installer_update = reject_legacy
        reason = client_agent.run_agent(config)
        if reason in {"restart", "update"}: raise SystemExit(75)
        if reason == "installer_update": raise SystemExit(76)
    finally:
        instance.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--agent-child", action="store_true")
    args = parser.parse_args()
    if args.agent_child:
        agent_child(args.source, args.data); return
    source, data = bind_runtime(args.source, args.data)
    from runtime_state import acquire_single_instance
    instance = acquire_single_instance("XASS-native-desktop-host")
    if instance is None: raise SystemExit(2)
    host = DesktopHost(source, data)
    host.supervisor.start()
    protocol = sys.stdout
    try:
        for line in iter(lambda: sys.stdin.readline(MAX_LINE + 1), ""):
            request_id = None
            try:
                if len(line) > MAX_LINE: raise ValueError("Request too large")
                request = json.loads(line)
                if not isinstance(request, dict) or type(request.get("id")) is not int:
                    raise ValueError("Request identifier required")
                request_id = request["id"]
                action = text(request.get("action"), 80, empty=False)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    result = host.request(action, request)
                response = {"id": request_id, "ok": True, "result": result}
            except Exception:
                response = {"id": request_id, "ok": False, "error": "Действие не выполнено. Проверьте состояние агента и повторите."}
            encoded = json.dumps(response, ensure_ascii=True, allow_nan=False)
            if len(encoded) > MAX_OUTPUT: encoded = json.dumps({"id": request_id, "ok": False, "error": "Ответ слишком большой"})
            protocol.write(encoded + "\n"); protocol.flush()
            if host.stop_event.is_set(): break
    finally:
        host.close(); instance.close()


if __name__ == "__main__":
    main()
