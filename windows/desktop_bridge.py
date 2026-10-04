"""Versioned native desktop operations. No Tk, child lifetime or raw secrets in DTOs.

The trusted parent supplies immutable absolute runtime paths. Mutation is limited
by typed schemas; pairing/settings share a cross-process lock and atomic sealing.
"""
from __future__ import annotations

import contextlib
import getpass
import json
import math
import os
from pathlib import Path, PureWindowsPath
import platform
import re
import socket
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from typing import Any

VERSION = 1
MAX_TEXT = 65536
SCHEMAS = {
    "desktop_status": set(), "desktop_metrics": set(), "desktop_processes": set(),
    "desktop_settings": set(), "desktop_save_settings": {"settings"},
    "desktop_profile": {"path", "text"}, "desktop_pair": {"server", "name", "code", "path", "text"},
    "desktop_health": set(), "desktop_miniapp": set(),
    "desktop_files": {"root", "path"}, "desktop_archive": set(),
    "desktop_archive_rows": set(), "desktop_archive_detail": {"id"},
    "desktop_archive_probe": {"path"}, "desktop_diagnostics": set(),
    "desktop_updates": set(), "desktop_check_update": set(), "desktop_transcription": set(),
    "desktop_clipboard": set(), "desktop_screenshot": set(), "desktop_lock": {"confirmed"},
}
SETTINGS = {"owner_name", "interval_sec", "auto_update", "transcription_enabled",
            "archive_folder", "archive_max_gb", "archive_retention_days"}


def text(value: Any, maximum: int = 240, *, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError("Invalid text")
    if not empty and not value.strip():
        raise ValueError("Text required")
    return value.strip()


def number(value: Any, low: float, high: float, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid number")
    if not low <= value <= high or (integer and type(value) is not int):
        raise ValueError("Number out of range")
    return int(value) if integer else float(value)


def read_json(path: Path, limit: int = 1024 * 1024) -> dict:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("File too large")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Invalid object")
    return result


def bind_runtime(source: Path, data: Path) -> tuple[Path, Path]:
    if not source.is_absolute() or not data.is_absolute():
        raise ValueError("Absolute runtime paths required")
    source, data = source.resolve(strict=True), data.resolve(strict=True)
    if not source.is_dir() or not data.is_dir() or not (source / "client_agent.py").is_file():
        raise ValueError("Invalid runtime")
    # Required BEFORE importing any pc_client module that captures DATA_ROOT.
    existing = sys.modules.get("client_update")
    if existing is not None and Path(existing.DATA_ROOT).resolve() != data:
        raise ValueError("Runtime context already bound")
    os.environ["XASS_DATA_ROOT"] = str(data)
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    return source, data


def redact(value: Any, secrets: tuple[str, ...] = ()) -> str:
    result = str(value or "")
    for secret in secrets:
        if len(secret) >= 4:
            result = result.replace(secret, "[redacted]")
    result = re.sub(r"\bag_[A-Za-z0-9_\-]+", "[agent-key]", result)
    result = re.sub(r"(?i)(api[_-]?key|authorization|token|pair[_-]?code|secret|password)(\s*[=:]\s*)[^\s,;]+", r"\1\2[redacted]", result)
    result = re.sub(r"https?://[^\s<>\"']+", lambda m: safe_url(m.group(0)), result)
    return result.replace("\x00", "")[:2000]


def safe_url(value: str) -> str:
    try:
        p = urlsplit(value)
        if p.scheme not in {"https", "http"} or not p.hostname or p.username is not None or p.password is not None:
            return "[invalid-url]"
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except ValueError:
        return "[invalid-url]"


@contextlib.contextmanager
def config_lock(data: Path):
    # OS-owned advisory lock releases on process crash. Never delete a lock
    # file based on a partially written owner record or a guessed stale age.
    lock = data / ".native-config.lock"
    with lock.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Configuration busy") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class DesktopService:
    def __init__(self, source: Path, data: Path):
        self.source, self.data = bind_runtime(source, data)

    def config(self) -> dict:
        from secret_store import unseal_config
        raw = read_json(self.data / "config.json")
        envelope = raw.get("sealed")
        if envelope is not None and (not isinstance(envelope, dict) or envelope.get("cipher") not in {"dpapi", "aes-256-gcm"} or not envelope.get("data")):
            raise ValueError("Invalid existing encrypted configuration")
        if isinstance(envelope, dict) and envelope.get("cipher") == "aes-256-gcm":
            master = self.data / ".xass-master.key"
            if not master.is_file() or master.stat().st_size != 32:
                raise ValueError("Existing encryption key unavailable")
        # Never use client_agent.load_config's fallback-to-sealed-object behavior.
        return unseal_config(raw, data_dir=self.data) if raw else {}

    def save(self, config: dict) -> None:
        from secret_store import seal_config
        from runtime_state import atomic_write_json
        atomic_write_json(self.data / "config.json", seal_config(config, data_dir=self.data), backup=True)

    def settings(self) -> dict:
        raw = read_json(self.data / "config.json")
        return {"owner_name": str(raw.get("owner_name") or "")[:128],
                "interval_sec": raw.get("interval_sec", 30),
                "auto_update": raw.get("auto_update", True) is True,
                "transcription_enabled": raw.get("transcription_enabled", False) is True,
                "archive_folder": str(raw.get("archive_folder") or "")[:32760],
                "archive_max_gb": raw.get("archive_max_gb", 0),
                "archive_retention_days": raw.get("archive_retention_days", 0)}

    def validate_settings(self, values: Any) -> dict:
        if not isinstance(values, dict) or set(values) != SETTINGS:
            raise ValueError("Invalid settings schema")
        result = {"owner_name": text(values["owner_name"], 128),
                  "interval_sec": number(values["interval_sec"], 5, 86400, integer=True),
                  "archive_folder": text(values["archive_folder"], 32760),
                  "archive_max_gb": number(values["archive_max_gb"], 0, 1_000_000),
                  "archive_retention_days": number(values["archive_retention_days"], 0, 365000, integer=True)}
        for field in ("auto_update", "transcription_enabled"):
            if type(values[field]) is not bool:
                raise ValueError("Invalid boolean")
            result[field] = values[field]
        if result["archive_folder"] and not Path(result["archive_folder"]).is_absolute():
            raise ValueError("Absolute archive folder required")
        return result

    def profile(self, request: dict):
        from connection_file import load_connection_file, parse_connection_text
        if bool(request.get("path")) == bool(request.get("text")):
            raise ValueError("Provide one profile source")
        if request.get("path"):
            path = Path(text(request["path"], 32760, empty=False))
            if not path.is_absolute() or path.suffix.lower() not in {".xass", ".xass-connect", ".json"}:
                raise ValueError("Invalid profile file")
            return load_connection_file(path)
        return parse_connection_text(text(request["text"], MAX_TEXT, empty=False))

    def _connection(self):
        from network_client import require_secure_transport, create_http_client
        c = self.config()
        base = require_secure_transport(str(c.get("server_url") or ""),
            allow_insecure_http=c.get("allow_insecure_http") is True).rstrip("/")
        return c, base, create_http_client

    def request(self, action: str, request: dict) -> dict:
        if action not in SCHEMAS or set(request) - (SCHEMAS[action] | {"action", "version"}):
            raise ValueError("Unsupported desktop schema")
        if request.get("version", VERSION) != VERSION:
            raise ValueError("Unsupported protocol version")
        if action == "desktop_settings":
            return self.settings()
        if action == "desktop_save_settings":
            values = self.validate_settings(request.get("settings"))
            with config_lock(self.data):
                config = self.config(); config.update(values); config["desktop_managed"] = True
                self.save(config)
            return {"saved": True, "restart_required": True}
        if action == "desktop_profile":
            p = self.profile(request)
            return {"server": p.server_url, "name": p.source_name,
                    "expires_at": p.expires_at.isoformat(), "auto_update": p.auto_update}
        if action == "desktop_pair":
            profile = self.profile(request) if request.get("path") or request.get("text") else None
            server = profile.server_url if profile else text(request.get("server"), 2048, empty=False)
            code = profile.pair_code if profile else text(request.get("code"), 64, empty=False)
            name = (profile.source_name if profile else text(request.get("name"), 128)) or socket.gethostname()
            if len(code) < 4:
                raise ValueError("Invalid pairing code")
            with config_lock(self.data):
                config = self.config()
                from client_agent import discover_backend_url, normalize_server_url, claim_pair_code
                from e2e_crypto import ensure_agent_keys
                server = discover_backend_url(normalize_server_url(server))
                updated = dict(config); ensure_agent_keys(updated)
                result = claim_pair_code(server_url=server, pair_code=code, source_name=name,
                    source_type="PC_AGENT", e2e_public_jwk=updated.get("e2e_public_jwk"), allow_insecure_http=False)
                key = result.get("agent_api_key")
                if not isinstance(key, str) or not key.startswith("ag_"):
                    raise ValueError("Invalid pairing response")
                updated.update(server_url=server, source_name=str(result.get("source_name") or name),
                               source_type="PC_AGENT", api_key=key, desktop_managed=True)
                if profile:
                    updated["auto_update"] = profile.auto_update
                owner = result.get("owner_e2e_public_jwk") or (profile.e2e_public_jwk if profile else None)
                if owner:
                    updated["owner_e2e_public_jwk"] = owner
                self.save(updated)
            return {"paired": True, "name": name, "server": safe_url(server)}
        if action == "desktop_status":
            return self.status()
        if action in {"desktop_metrics", "desktop_processes"}:
            import psutil
            vm, disk = psutil.virtual_memory(), psutil.disk_usage(str(self.data.anchor))
            result = {"host": socket.gethostname(), "user": getpass.getuser(), "os": platform.platform(),
                      "uptime": max(0, int(time.time() - psutil.boot_time())),
                      "local_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "cpu_percent": psutil.cpu_percent(interval=0.15), "ram_percent": vm.percent,
                      "ram_used": vm.used, "ram_total": vm.total, "disk_percent": disk.percent,
                      "disk_used": disk.used, "disk_total": disk.total}
            if action == "desktop_processes":
                rows = []
                for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
                    try:
                        rows.append({"pid": p.pid, "name": str(p.info["name"] or "")[:160],
                            "cpu": float(p.info["cpu_percent"] or 0), "memory": float(p.info["memory_percent"] or 0)})
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                result["processes"] = sorted(rows, key=lambda p: (p["cpu"], p["memory"]), reverse=True)[:30]
            return result
        if action == "desktop_files":
            from remote_tools import list_files, ROOT_LABELS
            root, path = text(request.get("root"), 40), text(request.get("path", ""), 4096)
            if root not in ROOT_LABELS:
                raise ValueError("Unknown root")
            if Path(path).is_absolute() or PureWindowsPath(path).is_absolute() or ".." in path.replace("\\", "/").split("/"):
                raise ValueError("Unsafe relative path")
            return list_files(self.data, root, path)
        if action == "desktop_archive":
            from archive_store import archive_status
            result = archive_status(self.config()); result["last_error"] = redact(result.get("last_error"))
            return result
        if action in {"desktop_archive_rows", "desktop_archive_detail"}:
            from archive_store import conversation_rows
            rows = conversation_rows(self.config(), limit=500)
            if action == "desktop_archive_detail":
                wanted = number(request.get("id"), 0, 2**63 - 1, integer=True)
                row = next((r for r in rows if r["id"] == wanted), None)
                if not row:
                    raise ValueError("Message not available")
                return {"id": wanted, "text": str(row.get("text_content") or "")[:32000]}
            allowed = {"id", "created_at", "chat_id", "chat_title", "sender_name", "direction",
                       "deleted", "forwarded_from", "reply_to_message_id", "media_count", "message_date", "from_username"}
            return {"rows": [{**{k: (str(v)[:240] if isinstance(v, str) else v) for k, v in row.items() if k in allowed},
                             "text": str(row.get("text_content") or "")[:512]} for row in rows]}
        if action == "desktop_archive_probe":
            path = Path(text(request.get("path"), 32760, empty=False))
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("Existing absolute directory required")
            import tempfile, shutil
            with tempfile.NamedTemporaryFile(prefix=".xass-write-test-", dir=path, delete=True) as f:
                f.write(b"XASS archive write test\n"); f.flush(); os.fsync(f.fileno())
            return {"writable": True, "free_bytes": shutil.disk_usage(path).free}
        if action == "desktop_health":
            config, base, factory = self._connection()
            start = time.monotonic()
            with factory(base, timeout=8, trust_env=config.get("trust_env_proxy") is True, follow_redirects=False) as c:
                response = c.get(base + "/health"); response.raise_for_status()
            return {"reachable": True, "latency_ms": round((time.monotonic() - start) * 1000), "server": safe_url(base)}
        if action == "desktop_miniapp":
            config, base, factory = self._connection()
            with factory(base, timeout=8, trust_env=config.get("trust_env_proxy") is True, follow_redirects=False) as c:
                response = c.get(base + "/api/pwa/config"); response.raise_for_status()
                if len(response.content) > 32768:
                    raise ValueError("Invalid discovery")
                body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Invalid web configuration")
            origin = str(body.get("web_app_url") or "").strip()
            if not origin:
                domain = str(body.get("domain") or "")
                if not domain or any(c in domain for c in "/\\?#@"):
                    raise ValueError("Invalid web origin")
                requirements = body.get("requirements") or {}
                if not isinstance(requirements, dict): raise ValueError("Invalid web requirements")
                scheme = "https" if requirements.get("https") is True else urlsplit(base).scheme
                origin = f"{scheme}://{domain}"
            if any(c.isspace() or ord(c) < 32 or c == "\\" for c in origin):
                raise ValueError("Invalid Mini App origin")
            p = urlsplit(origin)
            if p.scheme not in {"http", "https"} or not p.hostname or p.username is not None or p.password is not None or p.netloc.endswith(":") or p.port == 0:
                raise ValueError("Invalid Mini App origin")
            return {"url": urlunsplit((p.scheme, p.netloc, "/miniapp.php", "standalone=1", ""))}
        if action == "desktop_transcription":
            raw = read_json(self.data / "transcription" / "status.json")
            return {k: redact(v) if isinstance(v, str) else v for k, v in raw.items()
                    if k in {"stage", "state", "phase", "percent", "progress", "error", "message", "detail", "updated_at"}}
        if action == "desktop_updates":
            return self.updates()
        if action == "desktop_check_update":
            return self.check_update()
        if action == "desktop_diagnostics":
            c = self.config()
            secrets = tuple(str(c.get(k) or "") for k in ("api_key", "pair_code")) + tuple(str(v) for v in (c.get("e2e_private_jwk") or {}).values())
            from runtime_state import read_log_tail
            from archive_store import archive_status
            archive = archive_status(c); archive["last_error"] = redact(archive.get("last_error"), secrets)
            logs = [redact(x, secrets) for x in read_log_tail(120)]
            return {"protocol": VERSION, "status": self.status(), "updates": self.updates(),
                    "source": str(self.source), "data": str(self.data), "logs": logs, "archive": archive, "runtime_version": platform.python_version(),
                    "protection": str(read_json(self.data / "config.json").get("sealed", {}).get("cipher") or "legacy"),
                    "e2e": bool(c.get("e2e_private_jwk") and c.get("owner_e2e_public_jwk"))}
        if action == "desktop_clipboard":
            from remote_tools import clipboard_get
            return {"text": clipboard_get()[:1200]}
        if action == "desktop_screenshot":
            from PIL import ImageGrab
            folder = Path.home() / "Pictures" / "XASS"; folder.mkdir(parents=True, exist_ok=True)
            path = folder / (time.strftime("Screenshot-%Y%m%d-%H%M%S-") + str(time.time_ns() % 1000000) + ".jpg")
            image = ImageGrab.grab(all_screens=True)
            image.convert("RGB").save(path, "JPEG", quality=90)
            return {"path": str(path)}
        if action == "desktop_lock":
            if request.get("confirmed") is not True or os.name != "nt":
                raise ValueError("Explicit Windows lock confirmation required")
            import ctypes
            if not ctypes.windll.user32.LockWorkStation():
                raise OSError("Lock failed")
            return {"locked": True}
        raise ValueError("Unsupported action")

    def status(self) -> dict:
        raw = read_json(self.data / "config.json")
        report = read_json(self.data / ".agent-status.json")
        result = {"name": str(raw.get("source_name") or socket.gethostname())[:128],
                  "owner_name": str(raw.get("owner_name") or "")[:128], "paired": bool(raw.get("api_key") or raw.get("sealed")),
                  "server": safe_url(str(raw.get("server_url") or "")), "state": "stopped", "pid": 0,
                  "age": None, "latency_ms": 0, "version": str(report.get("agent_version") or report.get("version") or "")[:64],
                  "server_version": str(report.get("server_version") or "")[:64],
                  "detail": "Агент сообщил об ошибке. Откройте журнал для диагностики." if report.get("state") == "error" else "", "auto_update": raw.get("auto_update", True) is True}
        try:
            import psutil
            pid = number(report.get("process_id", 0), 1, 2**31 - 1, integer=True)
            process = psutil.Process(pid)
            command = " ".join(process.cmdline()).lower()
            if not process.is_running() or not any(x in command for x in ("client_agent.py", "--agent-child", "--agent")):
                return result
            started = process.create_time()
            updated = number(report.get("updated_at", 0), started - 1, time.time() + 5)
            interval = number(raw.get("interval_sec", 30), 5, 86400)
            age = max(0, int(time.time() - updated))
            state = str(report.get("state") or "connecting")
            if state not in {"online", "connecting", "offline", "error", "stale"}:
                state = "connecting"
            result.update(pid=pid, age=age, state="stale" if age > max(45, interval * 3 + 15) else state,
                          latency_ms=number(report.get("latency_ms", 0) or 0, 0, 600000))
        except (ValueError, TypeError, OSError, psutil.Error):
            pass
        return result

    def updates(self) -> dict:
        from client_update import current_version, current_revision
        state = read_json(self.data / ".updates" / ".in-progress")
        result = read_json(self.data / ".updates" / ".last-result.json")
        allowed = {"phase", "version", "revision", "message", "progress", "downloaded", "total", "updated_at", "ok", "finished_at"}
        return {"version": current_version(), "revision": current_revision(), "distribution": "native-test",
                "state": {k: redact(v) if isinstance(v, str) else v for k, v in state.items() if k in allowed},
                "result": {k: redact(v) if isinstance(v, str) else v for k, v in result.items() if k in allowed},
                "auto_update": self.settings()["auto_update"]}

    def check_update(self) -> dict:
        from client_update import current_version, current_revision, verify_manifest
        config, base, factory = self._connection()
        key = str(config.get("api_key") or "")
        with factory(base, timeout=10, trust_env=config.get("trust_env_proxy") is True, follow_redirects=False) as c:
            response = c.get(base + "/agent/update-manifest", headers={"X-Api-Key": key},
                params={"agent_version": current_version(), "agent_revision": current_revision(), "agent_distribution": "native-test"})
            if response.status_code == 204:
                return {"available": False}
            response.raise_for_status()
            if len(response.content) > 65536:
                raise ValueError("Manifest too large")
            body = response.json()
            manifest = body.get("native_update") or body.get("installer_update") or body.get("update") if isinstance(body, dict) else None
            if not manifest or not manifest.get("available"):
                return {"available": False}
        if not isinstance(manifest, dict) or not verify_manifest(manifest, key):
            raise ValueError("Unverified update manifest")
        compatible = manifest.get("distribution") == "native-test" and manifest.get("native_ui") is True
        # No legacy installer/source update may replace this native installation.
        return {"available": True, "compatible": compatible, "version": str(manifest.get("version") or "")[:64],
                "revision": str(manifest.get("revision") or "")[:128],
                "message": "Пакет нативного клиента" if compatible else "Сервер предложил несовместимый пакет. Нативный клиент не будет заменён старым интерфейсом."}
