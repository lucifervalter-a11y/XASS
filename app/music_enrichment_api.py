"""Owner-only, bounded and reversible catalog enrichment and local transcripts."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from types import SimpleNamespace
from typing import Literal
from weakref import WeakValueDictionary

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import update

from app.db import get_session
from app.music_models import MusicEnrichment, MusicTrack
from app.services.music_library import track_json
from app.services.music_lyrics import empty_lyrics, parse_lyrics
from app.services.music_query import filename_artist_title
from app.services.agent_workspace import AssetUploadTooLarge, read_bounded_body

MAX_OWNER_ARTWORK_BYTES = 8 * 1024 * 1024

_locks = WeakValueDictionary()
CATALOG_BUDGET_SECONDS = 18


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
    result["artwork_available"] = stored_artwork_available(record)
    result["owner_lyrics_available"] = bool(record and (record.owner_lyrics or {}).get("text"))
    result["owner_lyrics_enabled"] = result["owner_lyrics_available"] and not bool(record.owner_lyrics.get("disabled"))
    companions = lyrics_companions(record)
    result["lyrics_translation_available"] = "translation" in companions
    result["lyrics_transliteration_available"] = "transliteration" in companions
    if record and result.get("candidates"):
        result["candidate_token"] = hashlib.sha256(json.dumps([record.revision, record.fingerprint,
            result["candidates"]], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def lyrics_companions(record) -> dict:
    """Return only bounded owner-authored companion text safe for clients."""
    owner = record.owner_lyrics if record and isinstance(record.owner_lyrics, dict) else {}
    raw = owner.get("companions") if isinstance(owner.get("companions"), dict) else {}
    out = {}
    for key in ("translation", "transliteration"):
        item = raw.get(key)
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:64000]
        if not text:
            continue
        # Companion text is never presented as provider-verified. Automatic
        # translation/transliteration would require a separately configured,
        # attributable provider; this endpoint stores only the owner's input.
        value = {"text": text, "source": "owner", "automatic": False}
        if key == "translation":
            language = str(item.get("language") or "").strip()[:16]
            if language:
                value["language"] = language
        else:
            value["scheme"] = str(item.get("scheme") or "user")[:24]
        out[key] = value
    return out


def with_aligned_lyrics_companions(lyrics: dict, companions: dict) -> dict:
    """Attach owner companion text only when every displayed line aligns.

    Timed companion LRC must match every base timestamp (within a small
    formatting tolerance). Plain companion text must have exactly one non-empty
    row per base row. A mismatch stays available in ``companions`` for editing,
    but is never shown against the wrong sung line.
    """
    result = dict(lyrics or {})
    if companions:
        result["companions"] = companions
    rows = result.get("lines")
    if not isinstance(rows, list) or not rows or len(rows) > 2000:
        return result
    copied = [dict(row) if isinstance(row, dict) else {} for row in rows]
    starts: list[float] = []
    for row in copied:
        raw = row.get("start", row.get("time"))
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            return result
        starts.append(float(raw))

    for companion_key, line_key in (("translation", "translation"),
                                    ("transliteration", "pronunciation")):
        item = companions.get(companion_key) if isinstance(companions, dict) else None
        text = str(item.get("text") or "") if isinstance(item, dict) else ""
        parsed = parse_lyrics(text)
        values: list[str] = []
        if parsed.get("synced"):
            timed = parsed.get("lines") if isinstance(parsed.get("lines"), list) else []
            if len(timed) != len(copied):
                continue
            valid = True
            for expected, row in zip(starts, timed):
                raw_time = row.get("time") if isinstance(row, dict) else None
                value = str(row.get("text") or "").strip() if isinstance(row, dict) else ""
                if (isinstance(raw_time, bool) or not isinstance(raw_time, (int, float))
                        or not math.isfinite(float(raw_time)) or abs(float(raw_time) - expected) > .75
                        or not value):
                    valid = False
                    break
                values.append(value[:500])
            if not valid:
                continue
        else:
            values = [line.strip()[:500] for line in str(parsed.get("text") or "").splitlines() if line.strip()]
            if len(values) != len(copied):
                continue
        for row, value in zip(copied, values):
            row[line_key] = value
    result["lines"] = copied
    return result


def stored_artwork_available(record) -> bool:
    result = record.result if record and isinstance(record.result, dict) else {}
    manual = result.get("manual_artwork") if isinstance(result.get("manual_artwork"), dict) else {}
    return bool(record and record.artwork_data and (not record.dismissed or manual.get("source") == "owner"))


async def catalog_lookup(snapshot, candidate=None, *, refresh=False):
    """One bounded lookup; optional cover I/O never discards verified lyrics."""
    from app.services.music_enrichment import enrich_track, enrich_confirmed_candidate, fetch_artwork_thumbnail
    deadline = asyncio.get_running_loop().time() + CATALOG_BUDGET_SECONDS
    kwargs = {"deadline": deadline - .1, **({"refresh": True} if refresh else {})}
    try:
        async with asyncio.timeout_at(deadline):
            result = (await enrich_confirmed_candidate(snapshot, candidate, **kwargs)
                if candidate is not None else await enrich_track(snapshot, **kwargs))
    except (TimeoutError, OSError):
        result = {"status": "unavailable", "reason": "catalog_unavailable", "lyrics": empty_lyrics()}
    artwork = None
    described = result.get("artwork", {}).get("status") == "candidate"
    if result.get("status") == "matched" or described:
        try:
            async with asyncio.timeout_at(deadline):
                artwork = await fetch_artwork_thumbnail(result.get("artwork", {}))
        except (TimeoutError, OSError):
            pass
        if artwork is None and described:
            result = {**result, "artwork_reason": "artwork_unavailable"}
    return result, artwork


async def adopt_filename_names(session, track) -> None:
    """Store "Artist - Title" from an untagged filename before the catalog is asked.

    A dismissed lookup keeps the owner's original tags. The split is the same
    one covers already use, so the list stops showing the whole file name.
    """
    split = filename_artist_title(track.title, track.artist)
    if split is None:
        return
    artist, title = split
    if artist == track.artist and title == track.title:
        return
    changed = await session.execute(update(MusicTrack).where(MusicTrack.id == track.id,
        MusicTrack.deleted.is_(False), MusicTrack.title == track.title,
        MusicTrack.artist == track.artist).values(title=title[:240], artist=artist[:240]))
    if changed.rowcount != 1:
        return
    track.title, track.artist = title[:240], artist[:240]
    await session.commit()


async def enrich_saved_track(session, track_id, *, refresh=False):
    # Locks are per database and track; never hold a DB transaction over network I/O.
    key = (str(session.bind.url), track_id)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        track = await find_track(session, track_id)
        record = await session.get(MusicEnrichment, track_id, populate_existing=True)
        if record is None or not record.dismissed:
            await adopt_filename_names(session, track)
        now = datetime.now(timezone.utc)
        confirmed_candidate, previous_result, previous_artwork = None, None, None
        if record:
            age = (now - record.checked_at.replace(tzinfo=timezone.utc)).total_seconds()
            ttl = 30 * 86400 if record.result.get("status") == "matched" else 6 * 3600
            retry_status = record.result.get("lookup_status") if record.result.get("using_cached_result") else record.result.get("status")
            if retry_status in {"unavailable", "rate_limited"}:
                ttl = max(60, min(86400, record.result.get("retry_after", 60)))
            elif record.result.get("artwork_reason") in {"catalog_unavailable", "artwork_unavailable"}:
                ttl = min(ttl, 60)
            same = record.fingerprint == fingerprint(track)
            # Older lookups left a duration mismatch with no cover attempt.
            # One new lookup fills the art; lyrics stay a suggestion.
            needs_cover = (same and not record.dismissed and record.result.get("status") == "candidate"
                and record.result.get("reason") == "duration_mismatch" and not record.artwork_data
                and (record.result.get("artwork") or {}).get("status") != "candidate"
                and not record.result.get("artwork_reason"))
            if same and not record.dismissed and record.result.get("status") == "matched":
                previous_result, previous_artwork = deepcopy(record.result), record.artwork_data
            if same and not record.dismissed and record.result.get("status") == "confirmed":
                # Owner selection is durable, not a six-hour search suggestion.
                # Only an explicit refresh may revalidate the chosen provider ID.
                if not refresh:
                    return {"ok": True, "track": track_json(track), "enrichment": result_json(record)}
                confirmed_candidate = deepcopy(record.result.get("candidate") or {})
                previous_result = deepcopy(record.result)
                previous_artwork = record.artwork_data
            if (record.dismissed and not refresh) or (same and record.result.get("status") and age < (60 if refresh else ttl) and not needs_cover):
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
        result, artwork = await catalog_lookup(snapshot, confirmed_candidate, refresh=refresh)
        if len(json.dumps(result, ensure_ascii=False).encode()) > 192 * 1024:
            result, artwork = {"status": "unavailable", "lyrics": empty_lyrics()}, None
        if confirmed_candidate is None and previous_result is not None and result.get("status") in {"unavailable", "rate_limited"}:
            failure = result
            result = {key: value for key, value in previous_result.items()
                if key not in {"lookup_status", "lookup_reason", "retry_after", "using_cached_result"}}
            result.update(using_cached_result=True, lookup_status=failure["status"], lookup_reason=failure.get("reason", "catalog_unavailable"))
            if failure.get("retry_after"):
                result["retry_after"] = failure["retry_after"]
            artwork = previous_artwork
        elif (confirmed_candidate is None and previous_result is not None and result.get("status") == "matched"
                and artwork is None and result.get("artwork_reason") in {"catalog_unavailable", "artwork_unavailable"}):
            # Optional cover outages cannot erase an already verified thumbnail.
            artwork = previous_artwork
            if artwork:
                result = {**result, "artwork": previous_result.get("artwork", {"status": "not_found"})}
        if confirmed_candidate is not None:
            retryable = result.get("status") in {"unavailable", "rate_limited"}
            if retryable:
                # A temporary catalog outage cannot delete an explicit choice
                # or its previously verified lyrics/artwork.
                artwork = previous_artwork
            elif result.get("status") == "matched" and artwork is None:
                artwork = previous_artwork
            result = {"status": "confirmed", "candidate": confirmed_candidate,
                "lyrics": (previous_result.get("lyrics") if retryable else result.get("lyrics")) or empty_lyrics(),
                "provenance": (previous_result.get("provenance", []) if retryable else result.get("provenance", [])),
                "artwork": (previous_result.get("artwork", {"status": "not_found"}) if retryable else result.get("artwork", {"status": "not_found"})),
                "artwork_reason": (previous_result.get("artwork_reason", "") if retryable else result.get("artwork_reason", "")),
                "lookup_status": result.get("status", "not_found"), "lookup_reason": result.get("reason", ""),
                **({"retry_after": result["retry_after"]} if result.get("retry_after") else {}),
                **({"using_cached_result": True} if retryable else {})}
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


class LyricsSourceBody(BaseModel):
    source: Literal["catalog", "owner"]


class LyricsCompanionBody(BaseModel):
    translation: str | None = Field(default=None, max_length=64000)
    translation_language: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,8})?$")
    transliteration: str | None = Field(default=None, max_length=64000)
    transliteration_scheme: Literal["user", "iso9", "custom"] | None = None


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
        snapshot = SimpleNamespace(**{key: getattr(track, key) for key in
            ("id", "title", "artist", "album", "filename", "duration", "sha256")},
            is_excerpt=bool(record.original.get("is_excerpt")))
        expected_revision = record.revision
        await session.rollback()
        selected, artwork = await catalog_lookup(snapshot, candidate)
        track = await lock_track_row(session, track_id)
        record = await session.get(MusicEnrichment, track_id, populate_existing=True)
        if not record or record.revision != expected_revision or record.fingerprint != fingerprint(track) or result_json(record).get("candidate_token") != payload.candidate_token:
            raise HTTPException(409, "Песня или результаты поиска изменились. Повторите выбор.")
        for key, value in values.items():
            setattr(track, key, value)
        record.result = {"status": "confirmed", "candidate": candidate, "lyrics": selected.get("lyrics") or empty_lyrics(),
            "provenance": selected.get("provenance") or record.result.get("provenance", []),
            "lookup_status": selected.get("status", "not_found"), "lookup_reason": selected.get("reason", ""),
            "artwork": selected.get("artwork", {"status": "not_found"}), "artwork_reason": selected.get("artwork_reason", "")}
        record.artwork_data = artwork if artwork and len(artwork) <= 384 * 1024 else None
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
        # Imported files often have duration 0 (unknown). That used to reject
        # every transcript line after 1 s with "Некорректный текст".
        limit = track.duration + 1 if track.duration and track.duration > 0 else 86400
        if not value["text"] or any(row["time"] > limit for row in value["lines"]):
            raise HTTPException(400, "Некорректный текст или время строк.")
        value.update(source="on_device_transcription", status="transcribed")
        record = await session.get(MusicEnrichment, track_id)
        if record is None:
            record = MusicEnrichment(track_id=track_id, fingerprint=fingerprint(track), original=identity(track), result={})
            session.add(record)
        # Re-transcription replaces only the primary words/timing. A manually
        # corrected translation or transliteration remains reversible data.
        companions = lyrics_companions(record)
        record.owner_lyrics = {**value, **({"companions": companions} if companions else {})}
        record.revision = (record.revision or 0) + 1
        await session.commit()
        return {"ok": True, "lyrics": value}

    @router.put("/api/mini/music/tracks/{track_id}/lyrics/companions")
    async def lyrics_companion(track_id: int, payload: LyricsCompanionBody,
                               user=Depends(require_owner), session=Depends(get_session)):
        if not payload.model_fields_set:
            raise HTTPException(400, "Передайте перевод или транслитерацию.")
        track = await lock_track_row(session, track_id)
        record = await session.get(MusicEnrichment, track_id, populate_existing=True)
        if record is None:
            record = MusicEnrichment(track_id=track_id, fingerprint=fingerprint(track),
                                     original=identity(track), result={}, owner_lyrics={})
            session.add(record)
        owner = dict(record.owner_lyrics or {})
        companions = lyrics_companions(record)
        if "translation" in payload.model_fields_set:
            text = str(payload.translation or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if text:
                language = payload.translation_language or (companions.get("translation") or {}).get("language") or ""
                companions["translation"] = {"text": text, "source": "owner", "automatic": False,
                                             **({"language": language} if language else {})}
            else:
                companions.pop("translation", None)
        elif "translation_language" in payload.model_fields_set:
            if "translation" not in companions:
                raise HTTPException(409, "Сначала сохраните перевод.")
            companions["translation"]["language"] = payload.translation_language or ""
        if "transliteration" in payload.model_fields_set:
            text = str(payload.transliteration or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if text:
                scheme = payload.transliteration_scheme or (companions.get("transliteration") or {}).get("scheme") or "user"
                companions["transliteration"] = {"text": text, "scheme": scheme,
                                                  "source": "owner", "automatic": False}
            else:
                companions.pop("transliteration", None)
        elif "transliteration_scheme" in payload.model_fields_set:
            if "transliteration" not in companions:
                raise HTTPException(409, "Сначала сохраните транслитерацию.")
            companions["transliteration"]["scheme"] = payload.transliteration_scheme or "user"
        changed = owner.get("companions") != companions
        if companions:
            owner["companions"] = companions
        else:
            owner.pop("companions", None)
        if changed:
            record.owner_lyrics = owner
            record.revision = (record.revision or 0) + 1
        await session.commit()
        return {"ok": True, "companions": companions, "enrichment": result_json(record)}

    @router.put("/api/mini/music/tracks/{track_id}/artwork")
    async def owner_artwork(track_id: int, request: Request,
                            user=Depends(require_owner), session=Depends(get_session)):
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in {"image/jpeg", "image/png"}:
            raise HTTPException(415, "Поддерживаются JPEG и PNG")
        await find_track(session, track_id)
        await session.rollback()
        try:
            source = await read_bounded_body(request, limit=MAX_OWNER_ARTWORK_BYTES)
        except AssetUploadTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        from app.services.music_artwork import _jpeg_thumbnail
        jpeg = await asyncio.to_thread(_jpeg_thumbnail, source)
        if jpeg is None:
            raise HTTPException(400, "Не удалось безопасно обработать изображение")
        track = await lock_track_row(session, track_id)
        record = await session.get(MusicEnrichment, track_id, populate_existing=True)
        if record is None:
            record = MusicEnrichment(track_id=track_id, fingerprint=fingerprint(track), original=identity(track),
                                     result={}, owner_lyrics={}, checked_at=datetime.now(timezone.utc))
            session.add(record)
        result = dict(record.result or {})
        now = datetime.now(timezone.utc)
        result["manual_artwork"] = {"source": "owner", "automatic": False, "updated_at": now.isoformat()}
        record.result = result
        record.artwork_data = jpeg
        record.revision = (record.revision or 0) + 1
        await session.commit()
        return {"ok": True, "artwork": {"source": "owner", "automatic": False,
                "content_type": "image/jpeg", "bytes": len(jpeg), "revision": record.revision},
                "enrichment": result_json(record)}

    @router.patch("/api/mini/music/tracks/{track_id}/lyrics/source")
    async def lyrics_source(track_id: int, payload: LyricsSourceBody, user=Depends(require_owner), session=Depends(get_session)):
        track = await lock_track_row(session, track_id)
        record = await session.get(MusicEnrichment, track_id, populate_existing=True)
        if record is None or not (record.owner_lyrics or {}).get("text"):
            raise HTTPException(409, "Сохранённой расшифровки пока нет.")
        disabled = payload.source == "catalog"
        if bool(record.owner_lyrics.get("disabled")) != disabled:
            # Keep the text/timing intact so the owner can undo this preference.
            record.owner_lyrics = {**record.owner_lyrics, "disabled": disabled}
            record.revision = (record.revision or 0) + 1
        await session.commit()
        return {"ok": True, "track": track_json(track), "enrichment": result_json(record)}

    return router
