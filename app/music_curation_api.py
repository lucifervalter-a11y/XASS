"""Owner-only server catalog audit and atomic, reversible batch tag edits.

No arbitrary filesystem paths, external URLs, source-file rename or delete API.
The existing desktop curation tool handles separately confirmed local-file edits.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import get_session
from app.models import utcnow
from app.music_curation_models import MusicCurationBatch
from app.music_enrichment_api import fingerprint, lock_track_row
from app.music_models import MusicEnrichment, MusicTrack
from app.services.music_curation import AuditBusy, audit_tracks, catalog_identity, revision_token
from app.transcription_models import TranscriptionJob

PREFIX = "/api/mini/music/curation"


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditBody(StrictBody):
    track_ids: list[int] = Field(min_length=1, max_length=20)
    acoustic: StrictBool = False

    @field_validator("track_ids", mode="before")
    @classmethod
    def ids(cls, values):
        if not isinstance(values, list) or any(type(value) is not int or value <= 0 or value >= 2**63 for value in values) or len(set(values)) != len(values):
            raise ValueError("Выберите разные треки")
        return values


class TagValues(StrictBody):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    artist: str | None = Field(default=None, max_length=240)
    album: str | None = Field(default=None, max_length=240)

    @field_validator("title", "artist", "album")
    @classmethod
    def clean(cls, value, info):
        if value is not None:
            if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise ValueError("Недопустимый текст")
            value = value.strip()
            if info.field_name == "title" and not value:
                raise ValueError("Укажите название трека")
        return value


class TagEdit(StrictBody):
    track_id: int = Field(gt=0, lt=2**63, strict=True)
    revision_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    tags: TagValues


class BatchBody(StrictBody):
    request_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")
    confirmed: StrictBool
    changes: list[TagEdit] = Field(min_length=1, max_length=50)


class UndoBody(StrictBody):
    confirmed: StrictBool


def batch_json(batch):
    return {"id": batch.id, "state": batch.state, "created_at": batch.created_at.isoformat(),
            "undone_at": batch.undone_at.isoformat() if batch.undone_at else None,
            "entries": batch.entries, "audio_files_modified": False}


def build_router(settings, require_owner):
    router = APIRouter(prefix=PREFIX)
    root = Path(settings.music_root).absolute()
    audit_slots = asyncio.Semaphore(2)

    @router.get("/tracks")
    async def tracks(response: Response, after_id: int = Query(default=0, ge=0),
                     limit: int = Query(default=100, ge=1, le=100),
                     user=Depends(require_owner), session=Depends(get_session)):
        rows = list(await session.scalars(select(MusicTrack).where(MusicTrack.deleted.is_(False),
            MusicTrack.id > after_id).order_by(MusicTrack.id).limit(limit + 1)))
        records = {row.track_id: row for row in await session.execute(select(MusicEnrichment.track_id,
            MusicEnrichment.revision).where(MusicEnrichment.track_id.in_([row.id for row in rows[:limit]])))}
        response.headers["Cache-Control"] = "private, no-store"
        return {"tracks": [{"id": row.id, **catalog_identity(row), "filename": row.filename,
                            "revision_token": revision_token(row, records.get(row.id))} for row in rows[:limit]],
                "next_after_id": rows[limit - 1].id if len(rows) > limit else None}

    @router.post("/audit")
    async def audit(payload: AuditBody, response: Response, user=Depends(require_owner), session=Depends(get_session)):
        if audit_slots.locked():
            raise HTTPException(429, "Аудит уже выполняется. Повторите позже.", headers={"Retry-After": "5"})
        await audit_slots.acquire()
        owns_slot = True
        try:
            tracks = {row.id: row for row in await session.scalars(select(MusicTrack).where(
                MusicTrack.id.in_(payload.track_ids), MusicTrack.deleted.is_(False)))}
            if len(tracks) != len(payload.track_ids):
                raise HTTPException(404, "Один из треков не найден")
            records = {row.track_id: row for row in await session.scalars(select(MusicEnrichment).where(
                MusicEnrichment.track_id.in_(payload.track_ids)))}
            transcripts = await session.scalars(select(TranscriptionJob).where(
                TranscriptionJob.track_id.in_(payload.track_ids), TranscriptionJob.state == "done"))
            known_lyrics = {row.track_id for row in transcripts if row.sha256 == tracks[row.track_id].sha256
                and isinstance(row.result, dict) and any(isinstance(line, dict) and str(line.get("text") or "").strip()
                    for line in (row.result.get("lines") or [])[:2000])}
            pairs = []
            for key in payload.track_ids:
                track, record = tracks[key], records.get(key)
                snapshot = SimpleNamespace(**{field: getattr(track, field) for field in
                    ("id", "title", "artist", "album", "filename", "storage_name", "size", "duration", "sha256", "deleted")})
                cache = SimpleNamespace(**{field: deepcopy(getattr(record, field)) for field in
                    ("result", "owner_lyrics", "artwork_data", "dismissed", "revision")}) if record else None
                pairs.append((snapshot, cache))
            worker = asyncio.create_task(asyncio.to_thread(audit_tracks, root,
                pairs, acoustic=payload.acoustic, known_lyrics_ids=known_lyrics))

            def completed(task):
                audit_slots.release()
                if not task.cancelled():
                    task.exception()  # Consume failures after a disconnected request.

            worker.add_done_callback(completed)
            owns_slot = False
            # Request cancellation does not free the admission slot while its
            # thread still runs. At most two jobs can enter the executor queue.
            result = await asyncio.shield(worker)
        except AuditBusy as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "5"}) from None
        finally:
            if owns_slot:
                audit_slots.release()
        response.headers["Cache-Control"] = "private, no-store"
        return {"ok": True, **result}

    @router.get("/batches")
    async def history(response: Response, limit: int = Query(default=20, ge=1, le=50),
                      user=Depends(require_owner), session=Depends(get_session)):
        rows = await session.scalars(select(MusicCurationBatch).where(MusicCurationBatch.owner_id == user.user_id)
            .order_by(MusicCurationBatch.created_at.desc(), MusicCurationBatch.id.desc()).limit(limit))
        response.headers["Cache-Control"] = "private, no-store"
        return {"batches": [batch_json(row) for row in rows]}

    @router.post("/batches")
    async def apply(payload: BatchBody, response: Response, user=Depends(require_owner), session=Depends(get_session)):
        if not payload.confirmed:
            raise HTTPException(400, "Подтвердите пакет изменений после просмотра")
        ids = [edit.track_id for edit in payload.changes]
        if len(ids) != len(set(ids)):
            raise HTTPException(400, "Трек повторяется в пакете")
        if any(not edit.tags.model_dump(exclude_none=True) for edit in payload.changes):
            raise HTTPException(400, "Нет изменений тегов")
        request_hash = hashlib.sha256(json.dumps(payload.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

        async def existing_request():
            existing = await session.scalar(select(MusicCurationBatch).where(
                MusicCurationBatch.owner_id == user.user_id, MusicCurationBatch.request_key == payload.request_key))
            if existing is not None and existing.request_hash != request_hash:
                raise HTTPException(409, "Ключ запроса уже использован для другого пакета")
            return existing

        previous = await existing_request()
        if previous is not None:
            response.headers["Cache-Control"] = "private, no-store"
            return {"ok": True, "batch": batch_json(previous)}
        try:
            # Fixed lock order shares the enrichment API's row lock, preventing
            # provider responses or same-value manual edits from racing this batch.
            locked = {key: await lock_track_row(session, key) for key in sorted(ids)}
            # A concurrent idempotent request may have committed while we waited.
            previous = await existing_request()
            if previous is not None:
                rendered = batch_json(previous)
                await session.rollback()
                response.headers["Cache-Control"] = "private, no-store"
                return {"ok": True, "batch": rendered}
            entries = []
            for edit in sorted(payload.changes, key=lambda item: item.track_id):
                track = locked[edit.track_id]
                record = await session.get(MusicEnrichment, track.id, populate_existing=True)
                if revision_token(track, record) != edit.revision_token:
                    raise HTTPException(409, "Медиатека изменилась после просмотра. Повторите аудит.")
                before = catalog_identity(track)
                before_cache = {"dismissed": record.dismissed if record else False,
                                "fingerprint": record.fingerprint if record else "",
                                "tag_override": deepcopy((record.result or {}).get("curation_tag_override")) if record else None}
                for key, value in edit.tags.model_dump(exclude_none=True).items():
                    setattr(track, key, value)
                if record is None:
                    record = MusicEnrichment(track_id=track.id, original=deepcopy(before), result={}, owner_lyrics={}, revision=0)
                    session.add(record)
                # Audio did not change: preserve existing visible cover/lyrics,
                # fencing names separately from dismissal of all enrichment.
                record.result = {**(record.result or {}), "curation_tag_override": {
                    "source": "owner_batch", "automatic": False, "tags": catalog_identity(track)}}
                record.revision = (record.revision or 0) + 1
                record.fingerprint = fingerprint(track)
                entries.append({"track_id": track.id, "before": before, "after": catalog_identity(track),
                    "before_cache": before_cache, "after_token": revision_token(track, record)})
            batch = MusicCurationBatch(id=uuid.uuid4().hex, owner_id=user.user_id, request_key=payload.request_key,
                request_hash=request_hash, state="applied", entries=entries)
            session.add(batch)
            # Journal and catalog tags commit in ONE DB transaction. Any error
            # rolls back both; original media bytes are never opened for writing.
            await session.commit()
        except IntegrityError:
            await session.rollback()
            previous = await existing_request()
            if previous is None:
                raise HTTPException(409, "Пакет изменился. Повторите аудит.") from None
            batch = previous
        except BaseException:
            await session.rollback()
            raise
        response.headers["Cache-Control"] = "private, no-store"
        return {"ok": True, "batch": batch_json(batch)}

    @router.post("/batches/{batch_id}/undo")
    async def undo(batch_id: str, payload: UndoBody, response: Response,
                   user=Depends(require_owner), session=Depends(get_session)):
        if not payload.confirmed:
            raise HTTPException(400, "Подтвердите откат пакета")
        try:
            batch = await session.get(MusicCurationBatch, batch_id)
            if batch is None or batch.owner_id != user.user_id:
                raise HTTPException(404, "Пакет не найден")
            if batch.state == "undone":
                response.headers["Cache-Control"] = "private, no-store"
                return {"ok": True, "batch": batch_json(batch)}
            locked = {key: await lock_track_row(session, key) for key in sorted(entry["track_id"] for entry in batch.entries)}
            batch = await session.get(MusicCurationBatch, batch_id, populate_existing=True)
            if batch.state == "undone":
                response.headers["Cache-Control"] = "private, no-store"
                return {"ok": True, "batch": batch_json(batch)}
            for entry in batch.entries:
                track = locked[entry["track_id"]]
                record = await session.get(MusicEnrichment, track.id, populate_existing=True)
                if record is None or revision_token(track, record) != entry["after_token"]:
                    raise HTTPException(409, "Откат заблокирован: один из треков уже изменён. Ничего не перезаписано.")
                for key, value in entry["before"].items():
                    setattr(track, key, value)
                record.dismissed = entry["before_cache"]["dismissed"]
                record.fingerprint = entry["before_cache"]["fingerprint"] or fingerprint(track)
                restored_result = dict(record.result or {})
                restored_result.pop("curation_tag_override", None)
                if entry["before_cache"].get("tag_override") is not None:
                    restored_result["curation_tag_override"] = deepcopy(entry["before_cache"]["tag_override"])
                record.result = restored_result
                record.revision = (record.revision or 0) + 1
            batch.state, batch.undone_at = "undone", utcnow()
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        response.headers["Cache-Control"] = "private, no-store"
        return {"ok": True, "batch": batch_json(batch)}

    return router
