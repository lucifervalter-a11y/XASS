"""Signed, read-only Discord interactions. No gateway, tokens, or request-time SQL.

Only the explicit public XASS broadcast is eligible. The cache fails closed on
errors/expiry and is invalidated on local catalog/playback writes. This server's
single-worker deployment is intentional; other workers expire within 1.25 s.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, urlunsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.music_models import MusicEnrichment, MusicSession, MusicTrack
from app.models import HeartbeatSource
from app.services.music_broadcast import current_broadcast

MAX_BODY = 64 * 1024
CACHE_TTL = 1.25
REFRESH_INTERVAL = .5
REFRESH_BUDGET = .75
MAX_ARTWORK = 512 * 1024
MUTABLE_TABLES = {"music_sessions", "music_tracks", "music_enrichment", "heartbeat_sources"}


def public_url(value: str) -> str:
    """Configuration only, never an untrusted request Host or a media ticket."""
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (url.scheme != "https" or not host or url.username or url.password
                or url.query or url.fragment or len(value) > 1800
                or re.search(r"[\x00-\x20\x7f\\]", value)):
            return ""
        if url.port not in (None, 443):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            if "." not in host or host.lower().endswith((".local", ".localhost", ".internal")):
                return ""
        return value.rstrip("/")
    except (ValueError, TypeError):
        return ""


def _text(value, limit=240):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:limit]
    # Metadata is plain display text, never Discord markup or an @mention.
    return re.sub(r"([\\`*_{}\[\]()<>~|])", r"\\\1", text).replace("@", "@\u200b")


def _clock(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _cached_artwork(root, digest):
    if not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
        return None
    path = Path(root).absolute() / ".artwork" / f"{digest}-v1.jpg"
    try:
        for part in (path, *path.parents):
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                return None
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_ARTWORK:
            return None
        with path.open("rb") as source:
            data = source.read(MAX_ARTWORK + 1)
        return data if len(data) <= MAX_ARTWORK and data.startswith(b"\xff\xd8\xff") else None
    except OSError:
        return None


@dataclass(frozen=True)
class BroadcastSnapshot:
    track_id: int
    title: str
    artist: str
    album: str
    duration: float
    position: float
    captured_at: float
    artwork: bytes | None = field(default=None, repr=False)
    source_name: str = ""  # Empty for local playback; never emitted to Discord.


class BroadcastCache:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.snapshot: BroadcastSnapshot | None = None
        self.generation = 0
        self._refresh_sources: dict[object, str | None] = {}

    def invalidate(self):
        self.generation += 1
        self.snapshot = None

    def current(self):
        item = self.snapshot
        if item is None:
            return None
        elapsed = self.clock() - item.captured_at
        if (not 0 <= elapsed <= CACHE_TTL or not math.isfinite(item.duration)
                or not math.isfinite(item.position) or item.duration <= 0
                or item.position < 0 or item.position + elapsed >= item.duration):
            return None
        return item, item.position + elapsed

    async def refresh(self, sessions, music_root):
        generation, captured = self.generation, self.clock()
        refresh_id = object()
        self._refresh_sources[refresh_id] = None
        try:
            async with asyncio.timeout(REFRESH_BUDGET):
                async with sessions() as session:
                    playback = await session.get(MusicSession, 1)
                    source_name = playback.device[6:] if playback and playback.device.startswith("agent:") else ""
                    self._refresh_sources[refresh_id] = source_name
                    playing = await current_broadcast(session)
                    if playing is None:
                        if generation == self.generation:
                            self.snapshot = None
                        return
                    track, position = playing
                    enriched = await session.get(MusicEnrichment, track.id)
                    result = enriched.result if enriched and isinstance(enriched.result, dict) else {}
                    manual = result.get("manual_artwork") or {}
                    owned = isinstance(manual, dict) and manual.get("source") == "owner"
                    stored = enriched.artwork_data if enriched and (not enriched.dismissed or owned) else None
                    if not (stored and len(stored) <= MAX_ARTWORK and stored.startswith(b"\xff\xd8\xff")):
                        stored = None
                    artwork = stored if owned else None
                    if artwork is None:
                        artwork = await asyncio.to_thread(_cached_artwork, music_root, track.sha256) or stored
                    snapshot = BroadcastSnapshot(track.id, track.title, track.artist, track.album,
                        float(track.duration), float(position), captured, artwork, source_name)
                # A sharing/metadata write during refresh cannot republish its old read.
                if generation == self.generation:
                    self.snapshot = snapshot
        except asyncio.CancelledError:
            raise
        except Exception:
            # No request bodies, tokens, song names or SQL exception text in logs.
            if generation == self.generation:
                self.snapshot = None
        finally:
            self._refresh_sources.pop(refresh_id, None)

    @asynccontextmanager
    async def watch_writes(self, sessions):
        engine = sessions.kw["bind"].sync_engine
        flag = f"xass_discord_invalidate_{id(self)}"

        def matches(session):
            return session.get_bind() is engine

        def mark(session):
            if matches(session):
                session.info[flag] = True
                self.invalidate()

        def flushed(session, _):
            def relevant(row):
                if isinstance(row, (MusicSession, MusicTrack, MusicEnrichment)):
                    return True
                if not isinstance(row, HeartbeatSource):
                    return False
                # Ignore another PC's routine ORM heartbeat. Keep active-player
                # changes/deletion conservative, including offline, track and
                # state changes. Unknown in-flight reads still fail closed.
                sources = set(self._refresh_sources.values())
                if None in sources or self.snapshot is None:
                    return True
                sources.add(self.snapshot.source_name)
                names = {row.source_name, *inspect(row).attrs.source_name.history.deleted}
                return bool(sources.intersection(names))

            if any(relevant(row) for row in (*session.new, *session.dirty, *session.deleted)):
                mark(session)

        def executed(state):
            if ((state.is_update or state.is_delete or state.is_insert)
                    and getattr(getattr(state.statement, "table", None), "name", "") in MUTABLE_TABLES):
                mark(state.session)

        def finished(session):
            if session.info.pop(flag, False):
                self.invalidate()

        hooks = [("after_flush", flushed), ("do_orm_execute", executed),
                 ("after_commit", finished), ("after_rollback", finished)]
        for name, callback in hooks:
            event.listen(Session, name, callback)
        try:
            yield
        finally:
            for name, callback in hooks:
                event.remove(Session, name, callback)
            self.invalidate()


def music_response(cache, listen_url):
    current = cache.current()
    data = {"allowed_mentions": {"parse": []}}
    if current is None:
        data["content"] = "Сейчас ничего не играет в XASS"
    else:
        item, position = current
        marker = min(9, int(position / item.duration * 10))
        bar = "─" * marker + "🔘" + "─" * (9 - marker)
        embed = {"title": _text(item.title) or "Без названия", "color": 0x4B6BFB,
                 "description": f"{_text(item.artist) or 'Неизвестный исполнитель'}\n▶ {bar} {_clock(position)} / {_clock(item.duration)}",
                 "footer": {"text": "Публичный эфир XASS"}}
        if item.album:
            embed["fields"] = [{"name": "Альбом", "value": _text(item.album), "inline": False}]
        if listen_url:
            embed["url"] = listen_url
            data["components"] = [{"type": 1, "components": [
                {"type": 2, "style": 5, "label": "Слушать вместе", "url": listen_url}]}]
            if item.artwork:
                parts = urlsplit(listen_url)
                origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
                digest = hashlib.sha256(item.artwork).hexdigest()
                embed["thumbnail"] = {"url": f"{origin}/api/discord/artwork/{item.track_id}/{digest}.jpg"}
        data["embeds"] = [embed]
    return {"type": 4, "data": data}


def build_router(settings, sessions, *, cache=None):
    cache = cache if cache is not None else BroadcastCache()
    configured_key = str(getattr(settings, "discord_public_key", "") or "")
    key = None
    if re.fullmatch(r"[a-fA-F0-9]{64}", configured_key):
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(configured_key))
    listen_url = public_url(getattr(settings, "discord_listen_url", "") or
                            getattr(settings, "profile_public_url", "") or "https://redvps.site")

    async def refresh_loop():
        while True:
            await cache.refresh(sessions, settings.music_root)
            await asyncio.sleep(REFRESH_INTERVAL)

    @asynccontextmanager
    async def lifespan(_):
        if key is None:
            yield
            return
        async with cache.watch_writes(sessions):
            task = asyncio.create_task(refresh_loop())
            try:
                yield
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    router = APIRouter(lifespan=lifespan)

    @router.post("/api/discord/interactions")
    async def interactions(request: Request):
        # Authentication is exclusively Discord's signature over exact bytes.
        signature = request.headers.get("x-signature-ed25519", "")
        timestamp = request.headers.get("x-signature-timestamp", "")
        if (len(request.headers.getlist("x-signature-ed25519")) != 1
                or len(request.headers.getlist("x-signature-timestamp")) != 1
                or not re.fullmatch(r"[a-fA-F0-9]{128}", signature)
                or not re.fullmatch(r"[0-9]{1,12}", timestamp)
                or not -30 <= time.time() - int(timestamp) <= 300):
            raise HTTPException(401, "Invalid Discord signature")
        if key is None:
            raise HTTPException(503, "Discord public key is not configured")
        chunks, size = [], 0
        try:
            async with asyncio.timeout(.75):
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise HTTPException(413, "Interaction is too large")
                    chunks.append(chunk)
        except TimeoutError:
            raise HTTPException(408, "Interaction body timed out") from None
        body = b"".join(chunks)
        try:
            key.verify(bytes.fromhex(signature), timestamp.encode("ascii") + body)
        except (InvalidSignature, ValueError):
            raise HTTPException(401, "Invalid Discord signature") from None
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid Discord interaction") from None
        if not isinstance(payload, dict) or type(payload.get("type")) is not int:
            raise HTTPException(400, "Invalid Discord interaction")
        if payload["type"] == 1:
            result = {"type": 1}
        elif payload["type"] == 2:
            command = payload.get("data")
            if not isinstance(command, dict):
                raise HTTPException(400, "Invalid application command")
            if command.get("name") == "music" and command.get("type", 1) == 1:
                result = music_response(cache, listen_url)
            else:
                result = {"type": 4, "data": {"content": "Поддерживается команда /music", "flags": 64,
                                               "allowed_mentions": {"parse": []}}}
        else:
            raise HTTPException(400, "Unsupported Discord interaction")
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/api/discord/artwork/{track_id}/{digest}.jpg")
    async def artwork(track_id: int, digest: str):
        current = cache.current() if key is not None else None
        if (current is None or current[0].track_id != track_id or not current[0].artwork
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or hashlib.sha256(current[0].artwork).hexdigest() != digest):
            raise HTTPException(404, "Public artwork is unavailable")
        return Response(current[0].artwork, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    return router
