"""Owner and PC-worker routes for server-queued song transcription.

Owner: POST/GET /api/mini/music/tracks/{id}/transcription (create/join, status).
Agent: /agent/transcription/* authenticated with the individual agent key,
the same credential used for heartbeats and command delivery.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_session
from app.models import AgentCredential
from app.music_models import MusicTrack
from app.services import transcription_queue as tq
from app.services.agent_lifecycle import ensure_agent_attached
from app.services.agent_pairing import authenticate_agent_api_key
from app.services.music_library import issue_ticket
from app.transcription_models import TranscriptionJob

# Serializes queue mutations inside the single supported backend process.
_queue_lock = asyncio.Lock()
TICKET_TTL_SEC = 900


class TranscriptionRequest(BaseModel):
    language: str | None = Field(default=None, pattern=r"^(auto|[a-z]{2,3})$")
    force: bool = False


class Capabilities(BaseModel):
    gpu: bool = False
    gpu_name: str = Field(default="", max_length=120)
    vram_mb: int = Field(default=0, ge=0, le=10_000_000)
    cpu_cores: int = Field(default=0, ge=0, le=4096)
    cpu_threads: int = Field(default=0, ge=0, le=8192)
    ram_mb: int = Field(default=0, ge=0, le=100_000_000)


class Load(BaseModel):
    cpu_percent: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    gpu_percent: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    busy: bool = False


class PollBody(BaseModel):
    enabled: bool
    state: Literal["ready", "installing", "error"] = "ready"
    detail: str = Field(default="", max_length=300)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    load: Load = Field(default_factory=Load)
    running_job_id: int | None = Field(default=None, gt=0)


class ProgressBody(BaseModel):
    stage: Literal["download", "separate", "transcribe", "upload"] = "transcribe"
    fraction: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    load: Load | None = None


class Line(BaseModel):
    start: float = Field(ge=0, le=86400, allow_inf_nan=False)
    end: float = Field(ge=0, le=86400, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=500)


class CompleteBody(BaseModel):
    lines: list[Line] = Field(min_length=1, max_length=tq.MAX_LINES)
    language: str = Field(default="", max_length=8)
    model: str = Field(default="", max_length=64)
    device: str = Field(default="", max_length=16)
    elapsed_sec: float = Field(default=0, ge=0, le=86400 * 2, allow_inf_nan=False)


class FailBody(BaseModel):
    reason: str = Field(default="worker_error", max_length=200)


def build_router(settings, require_owner, catalog_check: Callable[..., Awaitable[bool]] | None = None):
    router = APIRouter()

    async def find_track(session, track_id):
        track = await session.get(MusicTrack, track_id)
        if track is None or track.deleted:
            raise HTTPException(404, "Трек не найден")
        return track

    async def agent(x_api_key: str | None = Header(default=None), session=Depends(get_session)):
        auth = await authenticate_agent_api_key(session, api_key=x_api_key,
            global_agent_api_key=getattr(settings, "agent_api_key", ""))
        if not auth:
            raise HTTPException(401, "Invalid agent key")
        if not auth.credential_id:
            raise HTTPException(403, "Для расшифровки привяжите ПК отдельным ключом")
        await ensure_agent_attached(session, auth.source_name)
        return auth

    async def worker_for(session, auth):
        from app.transcription_models import TranscriptionWorker
        worker = await session.scalar(select(TranscriptionWorker).where(TranscriptionWorker.credential_id == auth.credential_id))
        if worker is None:
            raise HTTPException(409, "ПК ещё не зарегистрирован как исполнитель расшифровки")
        return worker

    @router.get("/api/mini/music/tracks/{track_id}/transcription")
    async def status(track_id: int, response: Response, user=Depends(require_owner), session=Depends(get_session)):
        await find_track(session, track_id)
        response.headers["Cache-Control"] = "private, no-store"
        async with _queue_lock:
            now = tq.now_utc()
            await tq.expire_leases(session, now)
            await session.commit()
            return await tq.status_payload(session, track_id, now)

    @router.post("/api/mini/music/tracks/{track_id}/transcription")
    async def request(track_id: int, payload: TranscriptionRequest, response: Response,
                      user=Depends(require_owner), session=Depends(get_session)):
        response.headers["Cache-Control"] = "private, no-store"
        track = await find_track(session, track_id)
        existing = await session.scalar(select(TranscriptionJob).where(TranscriptionJob.track_id == track_id))
        if existing is None and not payload.force and catalog_check is not None:
            # LRCLIB first: PC transcription only if the catalog has no timed lyrics.
            if await catalog_check(session, track):
                return {**(await tq.status_payload(session, track_id, tq.now_utc())),
                        "status": "catalog_available", "message": tq.MESSAGES["catalog_available"]}
            track = await find_track(session, track_id)
        async with _queue_lock:
            now = tq.now_utc()
            await tq.request_job(session, track, language=payload.language or "ru",
                                 user_id=getattr(user, "user_id", None), now=now)
            await session.commit()
            return await tq.status_payload(session, track_id, now)

    @router.post("/agent/transcription/poll")
    async def poll(payload: PollBody, auth=Depends(agent), session=Depends(get_session)):
        async with _queue_lock:
            now = tq.now_utc()
            worker = await tq.upsert_worker(session, credential_id=auth.credential_id, source_name=auth.source_name,
                enabled=payload.enabled, state=payload.state, detail=payload.detail,
                capabilities=payload.capabilities.model_dump(), load=payload.load.model_dump(), now=now)
            await tq.release_worker_jobs(session, worker, now, running_job_id=payload.running_job_id)
            job = None
            if tq.worker_online(worker, now) and payload.running_job_id is None:
                await tq.schedule(session, now)
                job = await tq.claim_assigned(session, worker, now)
            body = {"ok": True, "job": None, "poll_after_sec": 20, "worker_state": worker.state}
            if job is not None:
                track = await session.get(MusicTrack, job.track_id)
                if track is None or track.deleted:
                    tq.mark_failed(job, "track_deleted", now)
                else:
                    credential = await session.get(AgentCredential, auth.credential_id)
                    ticket = issue_ticket(settings, track.id, purpose="agent", binding=credential.api_key_hash, ttl=TICKET_TTL_SEC)
                    body["job"] = {"id": job.id, "track_id": track.id, "title": track.title, "artist": track.artist,
                        "duration": track.duration, "mime": track.mime, "filename": track.filename,
                        "language": job.language, "sha256": track.sha256,
                        "media_path": f"/agent/music/tracks/{track.id}/stream?ticket={ticket}",
                        "lease_sec": tq.RUN_LEASE_SEC, "renew_every_sec": 60,
                        "deadline_at": tq.aware(job.deadline_at).isoformat()}
            await session.commit()
            return body

    async def running_job(session, auth, job_id):
        worker = await worker_for(session, auth)
        job = await tq.owned_running_job(session, worker, job_id)
        if job is None:
            # Lease lost (timeout or reassigned): the worker must abandon it.
            raise HTTPException(409, "Задача больше не закреплена за этим ПК")
        return worker, job

    @router.post("/agent/transcription/jobs/{job_id}/progress")
    async def progress(job_id: int, payload: ProgressBody, auth=Depends(agent), session=Depends(get_session)):
        async with _queue_lock:
            now = tq.now_utc()
            worker, job = await running_job(session, auth, job_id)
            await tq.renew(session, worker, job, stage=payload.stage, fraction=payload.fraction, now=now)
            if payload.load is not None:
                worker.load = {**tq.clean_load(payload.load.model_dump()), "busy": True}
            await session.commit()
            return {"ok": True, "lease_sec": tq.RUN_LEASE_SEC}

    @router.post("/agent/transcription/jobs/{job_id}/complete")
    async def complete(job_id: int, payload: CompleteBody, auth=Depends(agent), session=Depends(get_session)):
        async with _queue_lock:
            now = tq.now_utc()
            worker, job = await running_job(session, auth, job_id)
            result = await tq.complete(session, worker, job, lines=[item.model_dump() for item in payload.lines],
                meta=payload.model_dump(exclude={"lines"}), now=now)
            await session.commit()
            return {"ok": True, **result}

    @router.post("/agent/transcription/jobs/{job_id}/fail")
    async def failed(job_id: int, payload: FailBody, auth=Depends(agent), session=Depends(get_session)):
        async with _queue_lock:
            now = tq.now_utc()
            worker, job = await running_job(session, auth, job_id)
            state = await tq.fail(session, worker, job, reason=payload.reason, now=now)
            await session.commit()
            return {"ok": True, "state": state}

    return router
