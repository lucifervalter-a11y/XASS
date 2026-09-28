"""Loopback control for the agent-owned player.

The desktop window is a separate process. It reads music-playback.json and
sends pause/seek/volume/stop to 127.0.0.1. Media bytes never travel here.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from client_update import DATA_ROOT
from runtime_state import atomic_write_json

try:
    from music_player import MusicError
except ModuleNotFoundError:
    from pc_client.music_player import MusicError

STATUS_NAME = "music-playback.json"
BRIDGE_NAME = "music-bridge.json"
HEADER = "X-Xass-Bridge"
COMMANDS = frozenset({"music_pause", "music_resume", "music_seek", "music_stop", "music_volume", "music_status"})
_STATUS_KEYS = (
    "state", "track_id", "output_id", "position_sec", "duration_sec", "volume",
    "title", "artist", "error", "downloaded_bytes", "total_bytes",
)
_PAYLOAD_KEYS = frozenset({"volume", "position_sec", "output_id"})

_guard = threading.Lock()
_bridge: "Bridge | None" = None


class Bridge:
    def __init__(self, player, root: Path, server: ThreadingHTTPServer | None, thread: threading.Thread | None, stop: threading.Event, writer: threading.Thread):
        self.player = player
        self.root = root
        self.server = server
        self.thread = thread
        self.stop = stop
        self.writer = writer

    def shutdown(self) -> None:
        self.stop.set()
        server = self.server
        if server is not None:
            server.shutdown()
            server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=1)
        self.writer.join(timeout=1)


def _status_path(root: Path) -> Path:
    return root / STATUS_NAME


def _bridge_path(root: Path) -> Path:
    return root / BRIDGE_NAME


def playback_payload(player) -> dict[str, Any]:
    snap = player.snapshot()
    payload = {key: snap.get(key) for key in _STATUS_KEYS}
    try:
        payload["reveal"] = int(player.reveal_count())
    except (TypeError, ValueError):
        payload["reveal"] = 0
    return payload


def write_playback(player, root: Path | None = None) -> None:
    atomic_write_json(_status_path(root or DATA_ROOT), playback_payload(player))


def read_playback(root: Path | None = None) -> dict[str, Any]:
    path = _status_path(root or DATA_ROOT)
    try:
        if path.stat().st_size > 65536:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_bridge(root: Path | None = None) -> tuple[int, str]:
    path = _bridge_path(root or DATA_ROOT)
    try:
        if path.stat().st_size > 4096:
            raise OSError("bridge file")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, UnicodeError):
        raise MusicError("Плеер компьютера ещё не готов") from None
    if not isinstance(payload, dict):
        raise MusicError("Плеер компьютера ещё не готов")
    try:
        port = int(payload.get("port"))
    except (TypeError, ValueError):
        raise MusicError("Плеер компьютера ещё не готов") from None
    token = payload.get("token")
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not 1 <= port <= 65535:
        raise MusicError("Плеер компьютера ещё не готов")
    return port, token


def send_command(command: str, payload: dict | None = None, *, root: Path | None = None) -> dict[str, Any]:
    if command not in COMMANDS:
        raise MusicError("Эта команда недоступна с экрана компьютера")
    port, token = _load_bridge(root)
    body = {"command": command}
    for key, value in (payload or {}).items():
        if key in _PAYLOAD_KEYS:
            body[key] = value
    encoded = json.dumps(body).encode("utf-8")
    if len(encoded) > 4096:
        raise MusicError("Слишком большая команда")
    url = f"http://127.0.0.1:{port}/command"
    if urlsplit(url).hostname != "127.0.0.1":
        raise MusicError("Управление плеером доступно только на этом компьютере")
    try:
        with httpx.Client(timeout=httpx.Timeout(2.0, connect=1.0), trust_env=False, follow_redirects=False) as client:
            response = client.post(url, headers={HEADER: token, "Content-Type": "application/json"}, content=encoded)
    except httpx.HTTPError:
        raise MusicError("Не удалось связаться с плеером компьютера") from None
    if response.status_code == 409:
        try:
            detail = response.json().get("error") or "Плеер отклонил команду"
        except ValueError:
            detail = "Плеер отклонил команду"
        raise MusicError(str(detail)[:300])
    if response.status_code != 200:
        raise MusicError("Плеер компьютера не принял команду")
    try:
        parsed = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _make_handler(player, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            host = self.client_address[0]
            if host != "127.0.0.1" or self.path.split("?", 1)[0] != "/command":
                self._reply(404, b"")
                return
            supplied = self.headers.get(HEADER, "")
            if not isinstance(supplied, str) or len(supplied) != len(token) or not secrets.compare_digest(supplied, token):
                self._reply(404, b"")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                self._reply(400, b"")
                return
            if length < 0 or length > 4096:
                self._reply(413, b"")
                return
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError, TypeError):
                self._reply(400, b"")
                return
            if not isinstance(body, dict):
                self._reply(400, b"")
                return
            command = body.get("command")
            if not isinstance(command, str) or command not in COMMANDS:
                self._reply(404, b"")
                return
            payload = {key: body[key] for key in _PAYLOAD_KEYS if key in body}
            try:
                result = player.command(command, payload, {})
            except MusicError as exc:
                encoded = json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False).encode("utf-8")
                self._reply(409, encoded)
                return
            except Exception:
                self._reply(409, '{"ok":false,"error":"Не удалось выполнить команду"}'.encode("utf-8"))
                return
            public = {key: result.get(key) for key in _STATUS_KEYS} if isinstance(result, dict) else {}
            self._reply(200, json.dumps({"ok": True, "player": public}, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):
            self._reply(404, b"")

        def log_message(self, fmt: str, *args) -> None:
            return

        def _reply(self, status: int, body: bytes) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    self.wfile.write(body)
            except OSError:
                return

    return Handler


def _publish_loop(player, root: Path, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            write_playback(player, root)
        except Exception:
            pass
        stop.wait(0.5)


def start_music_bridge(player=None, root: Path | None = None) -> bool:
    """Start the status writer and the loopback command socket. Idempotent."""
    global _bridge
    with _guard:
        if _bridge is not None:
            return _bridge.server is not None
        if player is None:
            from music_player import music_player
            player = music_player()
        folder = root or DATA_ROOT
        folder.mkdir(parents=True, exist_ok=True)
        stop = threading.Event()
        writer = threading.Thread(target=_publish_loop, args=(player, folder, stop), name="xass-music-status", daemon=True)
        try:
            player.set_reveal_listener(lambda: write_playback(player, folder))
        except Exception:
            pass
        token = secrets.token_urlsafe(32)
        server = None
        thread = None
        bound = False
        try:
            handler = _make_handler(player, token)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            host, port = server.server_address[:2]
            if host != "127.0.0.1" or not isinstance(port, int):
                raise OSError("loopback")
            atomic_write_json(_bridge_path(folder), {"port": port, "token": token})
            thread = threading.Thread(target=server.serve_forever, name="xass-music-bridge", daemon=True)
            thread.start()
            bound = True
        except Exception as exc:
            print(f"[pc-client] music bridge did not bind: {exc}", flush=True)
            if server is not None:
                server.server_close()
                server = None
        writer.start()
        try:
            write_playback(player, folder)
        except Exception:
            pass
        _bridge = Bridge(player, folder, server, thread, stop, writer)
        return bound


def stop_music_bridge() -> None:
    global _bridge
    with _guard:
        current = _bridge
        _bridge = None
    if current is not None:
        current.shutdown()
