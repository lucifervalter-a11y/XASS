"""Loopback control for the agent-owned player.

The desktop window is a separate process. It reads music-playback.json and
sends pause/seek/volume/stop to 127.0.0.1. Media bytes never travel here.
"""
from __future__ import annotations

import json
import hashlib
import os
import secrets
import tempfile
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
COVER_NAME = "music-cover.jpg"
_MAX_COVER = 512 * 1024
_MAX_LYRICS = 8000
HEADER = "X-Xass-Bridge"
COMMANDS = frozenset({"music_pause", "music_resume", "music_seek", "music_stop", "music_volume", "music_status"})
_STATUS_KEYS = (
    "state", "track_id", "output_id", "position_sec", "duration_sec", "volume",
    "title", "artist", "error", "downloaded_bytes", "total_bytes", "media_transport",
)
_PAYLOAD_KEYS = frozenset({"volume", "position_sec", "output_id"})

_guard = threading.Lock()
_publish_guard = threading.Lock()
_cover_cache_guard = threading.Lock()
_bridge: "Bridge | None" = None
_cover_read_cache: dict[str, tuple[int, int, str, bytes]] = {}
_cover_write_cache: dict[str, str] = {}


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


def _presentation(player) -> tuple[str, bytes]:
    method = getattr(player, "presentation", None)
    if not callable(method):
        return "", b""
    try:
        notes = method()
    except Exception:
        return "", b""
    if not isinstance(notes, dict):
        return "", b""
    lyrics = notes.get("lyrics")
    artwork = notes.get("artwork")
    text = lyrics.replace("\x00", "")[:_MAX_LYRICS] if isinstance(lyrics, str) else ""
    if (not isinstance(artwork, (bytes, bytearray)) or len(artwork) > _MAX_COVER
            or not bytes(artwork).startswith(b"\xff\xd8\xff")):
        return text, b""
    return text, bytes(artwork)


def _reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _cover_digest(artwork: bytes) -> str:
    return hashlib.sha256(artwork).hexdigest() if artwork else ""


def _valid_digest(value: Any) -> str:
    digest = str(value or "").strip().lower()
    return digest if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest) else ""


def _snapshot_and_presentation(player) -> tuple[dict[str, Any], tuple[str, bytes]]:
    """Read the agent player view under its lock when one is available."""
    lock = getattr(player, "_lock", None)
    if lock is not None and callable(getattr(lock, "__enter__", None)):
        with lock:
            snap = player.snapshot()
            notes = _presentation(player)
    else:
        snap = player.snapshot()
        notes = _presentation(player)
    return (snap if isinstance(snap, dict) else {}), notes


def playback_payload(
    player,
    *,
    snapshot: dict[str, Any] | None = None,
    presentation: tuple[str, bytes] | None = None,
) -> dict[str, Any]:
    snap = snapshot if isinstance(snapshot, dict) else player.snapshot()
    payload = {key: snap.get(key) for key in _STATUS_KEYS}
    try:
        payload["reveal"] = int(player.reveal_count())
    except (TypeError, ValueError):
        payload["reveal"] = 0
    lyrics, artwork = presentation if presentation is not None else _presentation(player)
    if lyrics:
        payload["lyrics"] = lyrics
    # The desktop must never pair a previous track's cover file with the new
    # status. An empty value explicitly means that this track has no cover.
    payload["cover_sha256"] = _cover_digest(artwork)
    return payload


def _cover_path(root: Path) -> Path:
    return root / COVER_NAME


def read_cover(root: Path | None = None, *, expected_sha256: str | None = None) -> bytes:
    path = _cover_path(root or DATA_ROOT)
    expected = _valid_digest(expected_sha256) if expected_sha256 is not None else None
    if expected_sha256 is not None and not expected:
        return b""
    try:
        if _reparse(path) or not path.is_file():
            return b""
        stat = path.stat()
        if not 0 < stat.st_size <= _MAX_COVER:
            return b""
    except OSError:
        return b""
    cache_key = str(path)
    with _cover_cache_guard:
        cached = _cover_read_cache.get(cache_key)
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            _mtime, _size, digest, data = cached
            return data if (expected is None or digest == expected) else b""
    try:
        data = path.read_bytes()
    except OSError:
        return b""
    if not data.startswith(b"\xff\xd8\xff"):
        return b""
    digest = _cover_digest(data)
    with _cover_cache_guard:
        _cover_read_cache[cache_key] = (stat.st_mtime_ns, stat.st_size, digest, data)
    return data if (expected is None or digest == expected) else b""


def _write_cover(root: Path, artwork: bytes, digest: str) -> None:
    path = _cover_path(root)
    cache_key = str(path)
    temporary = None
    descriptor = -1
    try:
        if _reparse(root) or _reparse(path):
            return
        if not artwork:
            path.unlink(missing_ok=True)
            with _cover_cache_guard:
                _cover_read_cache.pop(cache_key, None)
                _cover_write_cache[cache_key] = ""
            return
        with _cover_cache_guard:
            known = _cover_write_cache.get(cache_key)
        if known == digest and path.is_file() and path.stat().st_size == len(artwork):
            return
        # On the first publish after a restart, validate the existing file once.
        # Later 500 ms status ticks use the digest cache and only perform a stat.
        if read_cover(root, expected_sha256=digest):
            with _cover_cache_guard:
                _cover_write_cache[cache_key] = digest
            return
        descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temporary = Path(raw_temporary)
        remaining = memoryview(artwork)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("Could not write cover")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        stat = path.stat()
        with _cover_cache_guard:
            _cover_write_cache[cache_key] = digest
            _cover_read_cache[cache_key] = (stat.st_mtime_ns, stat.st_size, digest, artwork)
    except OSError:
        return
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_playback(player, root: Path | None = None) -> None:
    folder = root or DATA_ROOT
    with _publish_guard:
        snapshot, presentation = _snapshot_and_presentation(player)
        _lyrics, artwork = presentation
        digest = _cover_digest(artwork)
        # Publish bytes first. Until the matching atomic status appears, an old
        # reader sees a digest mismatch and renders no cover rather than a cover
        # belonging to another track.
        _write_cover(folder, artwork, digest)
        atomic_write_json(
            _status_path(folder),
            playback_payload(player, snapshot=snapshot, presentation=presentation),
        )


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
        # The gate is inert without another audio owner; no network surface.
        if callable(getattr(player, "set_ownership", None)):
            from native_music_ownership import AudioOwnership
            player.set_ownership(AudioOwnership(folder, local=False))
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
