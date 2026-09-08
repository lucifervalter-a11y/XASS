"""Bounded audio ingestion and purpose-bound media tickets. No remote downloader."""
from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import time
from weakref import WeakValueDictionary

import mutagen

from app.services.pwa_auth import _session_generation, _session_secret

FORMATS = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg", ".m4a": "audio/mp4"}
CHUNK_BYTES = 512 * 1024
_content_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()


def content_lock(root: Path, sha256: str) -> asyncio.Lock:
    """Serialize SHA check + commit across imports in this single-worker server."""
    key = (str(root.resolve()), sha256)
    lock = _content_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _content_locks[key] = lock
    return lock


def display_title(original_name: str) -> str:
    """Humanize only the fallback label; never rename the source file or its tags."""
    return re.sub(r"\s+", " ", Path(original_name).stem.replace("_", " ")).strip()[:240]


def filename(value: str) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:240]
    if not name or Path(name).suffix.lower() not in FORMATS:
        raise ValueError("Поддерживаются MP3, WAV, FLAC, OGG и M4A")
    return name


def track_path(root: Path, storage_name: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}\.(mp3|wav|flac|ogg|m4a)", storage_name):
        raise ValueError("Invalid music storage name")
    path = (root / storage_name).resolve()
    if path.parent != root.resolve():
        raise ValueError("Invalid music storage path")
    return path


def inspect_audio(path: Path, original_name: str) -> dict:
    # Parse the actual container, not just its extension or browser MIME claim.
    audio = mutagen.File(path, easy=True)
    if audio is None or not getattr(audio, "info", None):
        raise ValueError("Файл не распознан как аудио")
    duration = float(getattr(audio.info, "length", 0))
    if not math.isfinite(duration) or duration <= 0 or duration > 24 * 3600:
        raise ValueError("Некорректная длительность аудио")
    extension = Path(original_name).suffix.lower()
    module = type(audio).__module__
    expected = {".mp3": ("mp3", "easyid3"), ".wav": ("wave",), ".flac": ("flac",), ".ogg": ("oggvorbis",), ".m4a": ("mp4", "easymp4")}
    if not any(name in module for name in expected[extension]):
        raise ValueError("Расширение файла не соответствует аудиоформату")
    def tag(name):
        values = (audio.tags or {}).get(name, [])
        return str(values[0] if isinstance(values, list) and values else "")[:240]
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"title": tag("title") or display_title(original_name), "artist": tag("artist"),
            "album": tag("album"), "duration": round(duration, 3), "mime": FORMATS[extension],
            "size": path.stat().st_size, "sha256": digest.hexdigest()}


def track_json(track) -> dict:
    payload = {key: getattr(track, key) for key in ("id", "title", "artist", "album", "duration", "favorite", "size", "mime", "filename")}
    if payload["title"] == Path(payload["filename"]).stem:
        payload["title"] = display_title(payload["filename"])
    payload["artwork_path"] = f"/api/mini/music/tracks/{track.id}/artwork"
    return payload


def issue_ticket(settings, track_id: int, *, purpose="listen", binding="", ttl=3600) -> str:
    payload = {"id": track_id, "p": purpose, "b": binding, "exp": int(time.time()) + ttl,
               "gen": _session_generation(settings), "nonce": secrets.token_hex(8)}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(_session_secret(settings), b"music-v1:" + encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + mac


def verify_ticket(settings, ticket: str, track_id: int, *, purposes=("listen", "download")) -> dict | None:
    try:
        if len(ticket) > 1024:
            return None
        encoded, signature = ticket.split(".")
        expected = hmac.new(_session_secret(settings), b"music-v1:" + encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if payload["id"] != track_id or payload["p"] not in purposes or payload["exp"] <= time.time():
            return None
        if payload["gen"] != _session_generation(settings):
            return None
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None
