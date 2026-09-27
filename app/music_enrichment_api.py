"""Owner-only, bounded and reversible catalog enrichment and local transcripts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
from types import SimpleNamespace
from weakref import WeakValueDictionary

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import update

from app.db import get_session
from app.music_models import MusicEnrichment, MusicTrack
from app.services.music_library import track_json
from app.services.music_lyrics import empty_lyrics, parse_lyrics

_locks = WeakValueDictionary()


def identity(track):
    return {key: getattr(track, key) for key in ("title", "artist", "album")}


def fingerprint(track):
    return hashlib.sha256(json.dumps({**identity(track), "sha256": track.sha256,
        "duration": track.duration}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def find_track(session, track_id):
    track = await session.get(MusicTrack, track_id, populate_existing=True)
    if not track or track.deleted:
        raise HTTPException(404, "Трек не найден")
    return track


async def lock_track_row(session, track_id):
    # A no-op write locks the row on PostgreSQL and serializes SQLite writers.
    # All enrichment mutations use the same track->cache lock order.
    changed = await session.execute(update(MusicTrack).where(MusicTrack.id == track_id,
        MusicTrack.deleted.is_(False)).values(id=track_id))
    if changed.rowcount != 1:
        raise HTTPException(404, "Трек не найден")
    return await find_track(session, track_id)


def result_json(record):
    result = dict(record.result or {}) if record else {"status": "not_checked"}
    if record and record.dismissed:
        result = {"status": "disabled", "lyrics": empty_lyrics()}
    result["can_restore"] = bool(record and record.original and not record.dismissed)
    if record and result.get("candidates"):
        result["candidate_token"] = hashlib.sha256(json.dumps([record.revision, record.fingerprint,
            result["candidates"]], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


async def enrich_saved_track(session, track_id, *, refresh=False):
    # Locks are per database and track; never hold a DB transaction over network I/O.
    key = (str(session.bind.url), track_id)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        track = await find_track(session, track_id)
        record = await session.get(MusicEnrichment, track_id, populate_existing=True)
        now = datetime.now(timezone.utc)
        if record:
            age = (now - record.checked_at.replace(tzinfo=timezone.utc)).total_seconds()
            ttl = 30 * 86400 if record.result.get("status") == "matched" else 6 * 3600
            if record.result.get("status") in {"unavailable", "rate_limited"}:
                ttl = max(60, min(86400, record.result.get("retry_after", 60)))
            same = record.fingerprint == fingerprint(track)
            if record.dismissed and not refresh or same and record.result.get("status") and age < (60 if refresh else ttl):
                return {"ok": True, "track": track_json(track), "enrichment": result_json(record)}
        before = identity(track)
        snapshot = SimpleNamespace(**{key: getattr(track, key) for key in
            ("id", "title", "artist", "album", "filename", "duration", "sha256")})
        generation = record.revision if record else None
        original = dict(record.original) if record and record.original and not record.dismissed else dict(before)
        snapshot.is_excerpt = bool((record.original or {}).get("is_excerpt")) if record else False
        snapshot.is_excerpt = snapshot.is_excerpt or bool(re.search(r"\b(?:cut(?:\d+(?:sec|s)?)?|clip|snippet|обрез\w*)\b",
            (original.get("title", "") + " " + snapshot.filename).replace("_", " "), re.I))
        original["is_excerpt"] = snapshot.is_excerpt
        await session.rollback()
        from app.services.music_enrichment import enrich_track, fetch_artwork_thumbnail
        try:
            async with asyncio.timeout(18):
                result = await enrich_track(snapshot)
                artwork = await fetch_artwork_thumbnail(result.get("artwork", {})) if result.get("status") == "matched" else None
        except (TimeoutError, OSError):
            result, artwork = {"status": "unavailable", "lyrics": empty_lyrics()}, None
        if len(json.dumps(result, ensure_ascii=False).encode()) > 192 * 1024:
            result, artwork = {"status": "unavailable", "lyrics": empty_lyrics()}, None
        track = await lock_track_row(session, track_id)
        record = await session.get(MusicEnrichment, track_id, populate_existing=True)
        if identity(track) != before or track.sha256 != snapshot.sha256 or (record.revision if record else None) != generation:
            await session.commit()
            return {"ok": True, "track": track_json(track), "enrichment": {"status": "changed"}}
        candidate = result.get("candidate") or {}
        changes = {key: str(candidate.get(key) or before[key])[:240] for key in before} if result.get("status") == "matched" else before
        if not changes["title"].strip():
            changes = before
        updated = await session.execute(update(MusicTrack).where(MusicTrack.id == track_id,
            MusicTrack.deleted.is_(False), MusicTrack.title == before["title"],
            MusicTrack.artist == before["artist"], MusicTrack.album == before["album"],
            MusicTrack.sha256 == snapshot.sha256).values(**changes))
        if updated.rowcount != 1:
            await session.rollback()
            raise HTTPException(409, "Трек изменён. Повторите поиск.")
        record = await session.get(MusicEnrichment, track_id, populate_existing=True)
        if record is None:
            record = MusicEnrichment(track_id=track_id, original=original, owner_lyrics={})
            session.add(record)
        record.result = result
        record.original = original
        record.revision = (record.revision or 0) + 1
        record.dismissed = False
        record.checked_at = now
        record.artwork_data = artwork if artwork and len(artwork) <= 384 * 1024 else None
        for field, value in changes.items():
            setattr(snapshot, field, value)
        record.fingerprint = fingerprint(snapshot)
        await session.commit()
        track = await find_track(session, track_id)
        return {"ok": True, "track": track_json(track), "enrichment": result_json(record)}


class EnrichBody(BaseModel):
    refresh: bool = False


class TranscriptBody(BaseModel):
    text: str = Field(min_length=1, max_length=64000)
    source: str = Field(pattern=r"^on_device_transcription$")


class CandidateBody(BaseModel):
    index: int = Field(ge=0, le=2, strict=True)
    candidate_token: str = Field(pattern=r"^[a-f0-9]{64}$")


def build_router(require_owner):
    router = APIRouter()

    @router.get("/api/mini/music/tracks/{track_id}/enrichment")
    async def status(track_id: int, response: Response, user=Depends(require_owner), session=Depends(get_session)):
        track = await find_track(session, track_id)
        record = await session.get(MusicEnrichment, track_id)
        response.headers["Cache-Control"] = "private, no-store"
        return {"ok": True, "track": track_json(track), "enrichment": result_json(record)}

    @router.post("/api/mini/music/tracks/{track_id}/enrichment")
    async def enrich(track_id: int, payload: EnrichBody, response: Response, user=Depends(require_owner), session=Depends(get_session)):
        response.headers["Cache-Control"] = "private, no-store"
        return await enrich_saved_track(session, track_id, refresh=payload.refresh)

    @router.post("/api/mini/music/tracks/{track_id}/enrichment/restore")
    async def restore(track_id: int, user=Depends(require_owner), session=Depends(get_session)):
        track = await lock_track_row(session, track_id)
        record = await session.get(MusicEnrichment, track_id)
        if record and record.original:
            # A newer manual edit always wins over a restore of stale metadata.
            if record.fingerprint != fingerprint(track):
                raise HTTPException(409, "Подписи уже изменены вручную; восстановление отменено.")
            for key, value in record.original.items():
                if key in {"title", "artist", "album"}:
                    setattr(track, key, value)
            record.dismissed = True
            record.revision += 1
            record.artwork_data = None
            record.fingerprint = fingerprint(track)
            await session.commit()
        return {"ok": True, "track": track_json(track), "enrichment": result_json(record)}

    @router.post("/api/mini/music/tracks/{track_id}/enrichment/confirm")
    async def confirm(track_id: int, payload: CandidateBody, user=Depends(require_owner), session=Depends(get_session)):
        track = await lock_track_row(session, track_id)
        record = await session.get(MusicEnrichment, track_id)
        candidates = (record.result or {}).get("candidates", []) if record else []
        if not record or record.fingerprint != fingerprint(track) or payload.index >= len(candidates) or result_json(record).get("candidate_token") != payload.candidate_token:
            raise HTTPException(409, "Результаты поиска изменились. Повторите поиск.")
        candidate = candidates[payload.index]
        before = identity(track)
        values = {key: str(candidate.get(key) or before[key])[:240] for key in before}
        if not values["title"].strip():
            raise HTTPException(400, "Не найдено название песни.")
        for key, value in values.items():
            setattr(track, key, value)
        record.result = {"status": "confirmed", "candidate": candidate, "lyrics": empty_lyrics(),
            "provenance": record.result.get("provenance", [])}
        record.dismissed = False
        record.revision += 1
        record.fingerprint = fingerprint(track)
        record.checked_at = datetime.now(timezone.utc)
        await session.commit()
        return {"ok": True, "track": track_json(track), "enrichment": result_json(record)}

    @router.put("/api/mini/music/tracks/{track_id}/lyrics")
    async def transcript(track_id: int, payload: TranscriptBody, user=Depends(require_owner), session=Depends(get_session)):
        track = await lock_track_row(session, track_id)
        value = parse_lyrics(payload.text)
        if not value["text"] or any(row["time"] > track.duration + 1 for row in value["lines"]):
            raise HTTPException(400, "Некорректный текст или время строк.")
        value.update(source="on_device_transcription", status="transcribed")
        record = await session.get(MusicEnrichment, track_id)
        if record is None:
            record = MusicEnrichment(track_id=track_id, fingerprint=fingerprint(track), original=identity(track), result={})
            session.add(record)
        record.owner_lyrics = value
        record.revision = (record.revision or 0) + 1
        await session.commit()
        return {"ok": True, "lyrics": value}

    return router
