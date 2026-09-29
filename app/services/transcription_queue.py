"""Persistent song-transcription queue executed by the owner's Windows PCs.

The server never transcribes (tiny VPS). It only keeps one job per track,
leases it to the most powerful free PC worker and caches the result forever.

Lifecycle: queued -> assigned (offered, short lease) -> running (renewed lease
+ hard deadline) -> done | failed. A worker that times out, restarts or reports
an error loses the lease; the job goes back to the queue and other workers are
preferred. After MAX_ATTEMPTS failed runs the job is failed; the owner may
explicitly retry a failed job. A done job is never run again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.music_models import MusicEnrichment, MusicTrack
from app.transcription_models import TranscriptionJob, TranscriptionWorker

WORKER_STALE_SEC = 75          # idle workers poll every ~20 s
ASSIGN_TTL_SEC = 90            # an offered job must be picked up by the next poll
RUN_LEASE_SEC = 300            # running workers renew every ~60 s
MIN_DEADLINE_SEC = 30 * 60     # hard cap for one run: max(30 min, 12 x duration)
DEADLINE_FACTOR = 12
MAX_ATTEMPTS = 3
BUSY_PERCENT = 95.0
MAX_LINES = 2000
ACTIVE = ("assigned", "running")

MESSAGES = {
    "none": "",
    "waiting_for_pc": "Расшифруем, когда включится компьютер",
    "preparing_pc": "ПК готовится к расшифровке",
    "queued": "В очереди на расшифровку",
    "running": "Расшифровываем на компьютере",
    "done": "Текст распознан автоматически, может быть с ошибками",
    "failed": "Не удалось распознать текст. Можно попробовать ещё раз.",
    "catalog_available": "Текст уже есть в каталоге",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes even for timezone-aware columns.
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- workers

def worker_score(worker: TranscriptionWorker) -> tuple:
    """Most powerful first: GPU VRAM, then CPU cores, then RAM."""
    caps = worker.capabilities or {}
    vram = _num(caps.get("vram_mb")) if caps.get("gpu") else 0.0
    return (vram, _num(caps.get("cpu_cores")), _num(caps.get("ram_mb")))


def worker_online(worker: TranscriptionWorker, now: datetime) -> bool:
    return bool(worker.enabled and worker.state == "ready"
                and aware(worker.last_seen_at) >= now - timedelta(seconds=WORKER_STALE_SEC))


def worker_overloaded(worker: TranscriptionWorker) -> bool:
    # The installed client sets busy at 70 %. One lease already limits a worker
    # to a single job, so only a measured CPU or GPU above the ceiling waits.
    load = worker.load or {}
    return (_num(load.get("cpu_percent")) > BUSY_PERCENT
            or _num(load.get("gpu_percent")) > BUSY_PERCENT)


async def _active_worker_ids(session) -> set[int]:
    rows = await session.scalars(select(TranscriptionJob.worker_id).where(
        TranscriptionJob.state.in_(ACTIVE), TranscriptionJob.worker_id.is_not(None)))
    return set(rows)


async def online_workers(session, now: datetime) -> list[TranscriptionWorker]:
    rows = await session.scalars(select(TranscriptionWorker).where(TranscriptionWorker.enabled.is_(True)))
    return [item for item in rows if worker_online(item, now)]


def worker_preparing(worker: TranscriptionWorker, now: datetime) -> bool:
    """Enabled and online, but still installing Python/torch/models for the first time."""
    return bool(worker.enabled and worker.state == "installing"
                and aware(worker.last_seen_at) >= now - timedelta(seconds=WORKER_STALE_SEC))


async def preparing_percent(session, now: datetime) -> int | None:
    rows = await session.scalars(select(TranscriptionWorker).where(TranscriptionWorker.enabled.is_(True),
                                                                   TranscriptionWorker.state == "installing"))
    values = [int(_num((item.setup or {}).get("percent"))) for item in rows if worker_preparing(item, now)]
    return max(0, min(100, max(values))) if values else None


async def free_workers(session, now: datetime) -> list[TranscriptionWorker]:
    """Online, at or under the CPU/GPU ceiling, and not already holding a job."""
    busy = await _active_worker_ids(session)
    return sorted((item for item in await online_workers(session, now)
                   if item.id not in busy and not worker_overloaded(item)),
                  key=worker_score, reverse=True)


def clean_capabilities(value: dict) -> dict:
    return {"gpu": bool(value.get("gpu")), "gpu_name": str(value.get("gpu_name") or "")[:120],
            "vram_mb": max(0, int(_num(value.get("vram_mb")))), "cpu_cores": max(0, int(_num(value.get("cpu_cores")))),
            "cpu_threads": max(0, int(_num(value.get("cpu_threads")))), "ram_mb": max(0, int(_num(value.get("ram_mb"))))}


def clean_setup(value: dict | None) -> dict:
    if not isinstance(value, dict):
        return {}
    return {"stage": str(value.get("stage") or "")[:32], "percent": int(min(100, max(0, _num(value.get("percent")))))}


def clean_load(value: dict) -> dict:
    gpu = value.get("gpu_percent")
    return {"cpu_percent": round(min(100.0, max(0.0, _num(value.get("cpu_percent")))), 1),
            "gpu_percent": None if gpu is None else round(min(100.0, max(0.0, _num(gpu))), 1),
            "busy": bool(value.get("busy"))}


async def upsert_worker(session, *, credential_id: int, source_name: str, enabled: bool, state: str,
                        detail: str, capabilities: dict, load: dict, now: datetime,
                        setup: dict | None = None) -> TranscriptionWorker:
    worker = await session.scalar(select(TranscriptionWorker).where(TranscriptionWorker.credential_id == credential_id))
    if worker is None:
        worker = TranscriptionWorker(credential_id=credential_id)
        session.add(worker)
    worker.source_name = str(source_name or "")[:128]
    worker.enabled = bool(enabled)
    worker.state = state if enabled else "disabled"
    worker.detail = str(detail or "")[:300]
    worker.capabilities = clean_capabilities(capabilities or {})
    worker.load = clean_load(load or {})
    worker.setup = clean_setup(setup) if worker.state == "installing" else {}
    worker.last_seen_at = now
    worker.updated_at = now
    await session.flush()
    return worker


# ---------------------------------------------------------------- jobs

def _requeue(job: TranscriptionJob, reason: str, now: datetime, *, count_attempt: bool) -> None:
    if job.worker_id is not None:
        failed = [item for item in (job.failed_workers or []) if item != job.worker_id]
        job.failed_workers = (failed + [job.worker_id])[-20:]
    if count_attempt:
        job.attempts = (job.attempts or 0) + 1
    job.worker_id = None
    job.lease_expires_at = None
    job.deadline_at = None
    job.assigned_at = None
    job.started_at = None
    job.progress = {}
    job.error = str(reason or "")[:200]
    job.updated_at = now
    job.state = "failed" if job.attempts >= MAX_ATTEMPTS else "queued"
    if job.state == "failed":
        job.finished_at = now


def mark_failed(job: TranscriptionJob, reason: str, now: datetime) -> None:
    job.state, job.error = "failed", str(reason or "")[:200]
    job.worker_id = job.lease_expires_at = job.deadline_at = None
    job.progress = {}
    job.finished_at = job.updated_at = now


async def expire_leases(session, now: datetime) -> int:
    expired = 0
    for job in list(await session.scalars(select(TranscriptionJob).where(TranscriptionJob.state.in_(ACTIVE)))):
        lease = aware(job.lease_expires_at)
        deadline = aware(job.deadline_at)
        if job.state == "assigned" and (lease is None or lease <= now):
            # Never picked up (PC slept/closed): prefer someone else, no attempt spent.
            _requeue(job, "not_picked_up", now, count_attempt=False)
            expired += 1
        elif job.state == "running" and ((lease is None or lease <= now) or (deadline and deadline <= now)):
            _requeue(job, "timeout" if deadline and deadline <= now else "lease_expired", now, count_attempt=True)
            expired += 1
    return expired


async def schedule(session, now: datetime) -> list[tuple[int, int]]:
    """Offer queued jobs (FIFO) to the most powerful free workers, one job each."""
    await expire_leases(session, now)
    queued = list(await session.scalars(select(TranscriptionJob).where(TranscriptionJob.state == "queued")
                                        .order_by(TranscriptionJob.created_at, TranscriptionJob.id)))
    if not queued:
        return []
    candidates = await free_workers(session, now)
    assigned: list[tuple[int, int]] = []
    for job in queued:
        if not candidates:
            break
        failed = set(job.failed_workers or [])
        # Stable sort keeps the power order; workers that already failed this job go last.
        worker = sorted(candidates, key=lambda item: item.id in failed)[0]
        candidates.remove(worker)
        job.state = "assigned"
        job.worker_id = worker.id
        job.assigned_at = now
        job.lease_expires_at = now + timedelta(seconds=ASSIGN_TTL_SEC)
        job.updated_at = now
        assigned.append((job.id, worker.id))
    return assigned


async def request_job(session, track, *, language: str, user_id: int | None, now: datetime) -> tuple[TranscriptionJob, bool]:
    """Create or join the single job of a track. Returns (job, created)."""
    job = await session.scalar(select(TranscriptionJob).where(TranscriptionJob.track_id == track.id))
    created = False
    if job is None:
        job = TranscriptionJob(track_id=track.id, sha256=str(track.sha256 or "")[:64], duration=_num(track.duration),
                               language=language, state="queued", failed_workers=[], progress={}, result={},
                               requested_by=user_id, created_at=now, updated_at=now)
        session.add(job)
        try:
            await session.flush()
            created = True
        except IntegrityError:
            # A concurrent request created it first: join that job.
            await session.rollback()
            job = await session.scalar(select(TranscriptionJob).where(TranscriptionJob.track_id == track.id))
    elif job.state == "failed":
        # Explicit owner retry of a failed track (a done track is never re-run).
        job.state, job.attempts, job.failed_workers, job.error = "queued", 0, [], ""
        job.language = language or job.language
        job.finished_at, job.updated_at = None, now
    await schedule(session, now)
    return job, created


def _estimate_seconds(job: TranscriptionJob, worker: TranscriptionWorker | None, now: datetime) -> float:
    duration = max(30.0, _num(job.duration, 240.0) or 240.0)
    gpu = bool(worker and (worker.capabilities or {}).get("gpu"))
    total = 60.0 + duration * (0.4 if gpu else 2.5)   # model load + demucs + whisper
    started = aware(job.started_at)
    elapsed = (now - started).total_seconds() if started else 0.0
    fraction = _num((job.progress or {}).get("fraction"))
    if 0.1 <= fraction < 1 and elapsed > 0:
        return max(0.0, elapsed * (1 - fraction) / fraction)
    return max(0.0, total - elapsed)


async def status_payload(session, track_id: int, now: datetime) -> dict:
    job = await session.scalar(select(TranscriptionJob).where(TranscriptionJob.track_id == track_id))
    online = await online_workers(session, now)
    if job is None:
        return {"ok": True, "status": "none", "message": "", "workers_online": len(online), "estimate_minutes": None}
    worker = await session.get(TranscriptionWorker, job.worker_id) if job.worker_id else None
    status = job.state
    estimate = None
    setup_percent = None
    if status in {"queued", "assigned"}:
        status = "queued" if online else "waiting_for_pc"
        if status == "waiting_for_pc":
            # No ready PC, but one is installing its components: say so with a percentage.
            setup_percent = await preparing_percent(session, now)
            if setup_percent is not None:
                status = "preparing_pc"
        if status == "queued":
            estimate = max(1, math.ceil(_estimate_seconds(job, (sorted(online, key=worker_score, reverse=True) or [None])[0], now) / 60))
    elif status == "running":
        estimate = max(1, math.ceil(_estimate_seconds(job, worker, now) / 60))
    message = MESSAGES.get(status, "")
    if status == "running":
        message = f"{message} · ~{estimate} мин"
    elif status == "preparing_pc":
        message = f"{message}, {setup_percent}%"
    return {"ok": True, "status": status, "job_id": job.id, "language": job.language, "message": message,
            "estimate_minutes": estimate, "stage": (job.progress or {}).get("stage", "") if status == "running" else "",
            "workers_online": len(online), "attempts": job.attempts, "setup_percent": setup_percent,
            "error": job.error if status == "failed" else "",
            "updated_at": aware(job.updated_at).isoformat() if job.updated_at else None,
            "line_count": len((job.result or {}).get("lines") or []) if status == "done" else 0}


async def done_result(session, track_id: int) -> dict | None:
    job = await session.scalar(select(TranscriptionJob).where(TranscriptionJob.track_id == track_id,
                                                              TranscriptionJob.state == "done"))
    return dict(job.result) if job and isinstance(job.result, dict) and job.result.get("lines") else None


async def job_state(session, track_id: int) -> str | None:
    return await session.scalar(select(TranscriptionJob.state).where(TranscriptionJob.track_id == track_id))


_ACTIVE_JOB = frozenset({"queued", "assigned", "running"})
# One library open used to queue only the page the phone asked for (50 tracks).
# The PC poll keeps filling from the rest of the library.
_SWEEP_LIMIT = 40
_SWEEP_LOOK = 400
_cursors: dict[str, int] = {}


def shown_lyrics_missing(value: dict | None) -> bool:
    """True when the payload has no words to show.

    A catalog timeout with an empty body is a song without text: the PC
    transcribes it. Timed catalog lines still win later, because the lyrics
    route keeps a synced catalog hit ahead of a finished transcription.
    """
    if not isinstance(value, dict):
        return False
    if str(value.get("text") or "").strip():
        return False
    if value.get("synced"):
        for row in value.get("lines") or []:
            if isinstance(row, dict) and str(row.get("text") or "").strip():
                return False
    return True


async def queue_pc_if_no_lyrics(session, track_id: int, value, *, user_id: int | None, now: datetime) -> bool:
    """Queue the owner's PC when this response has no words.

    Timed or plain catalog text is left as-is. A finished job is not run again,
    and a failed one waits for an explicit retry. Returns True while a job is
    still queued, assigned or running.
    """
    state = await job_state(session, track_id)
    if state in _ACTIVE_JOB:
        return True
    if state in {"done", "failed"} or not shown_lyrics_missing(value):
        return False
    track = await session.get(MusicTrack, track_id)
    if track is None or track.deleted:
        return False
    # Same default as the phone's language picker.
    await request_job(session, track, language="ru", user_id=user_id, now=now)
    await session.commit()
    return True


def _stored_lyrics(record: MusicEnrichment | None) -> bool:
    if record is None:
        return False
    owner = record.owner_lyrics if isinstance(record.owner_lyrics, dict) else {}
    if not owner.get("disabled") and str(owner.get("text") or "").strip():
        return True
    result = record.result if isinstance(record.result, dict) and not record.dismissed else {}
    lyrics = result.get("lyrics") if isinstance(result.get("lyrics"), dict) else {}
    return bool(str(lyrics.get("text") or "").strip())


def _cursor_key(session) -> str:
    bind = session.bind
    url = getattr(bind, "url", None)
    return str(url) if url is not None else str(id(bind))


def _fresh_job(track, user_id: int | None, now: datetime) -> TranscriptionJob:
    return TranscriptionJob(track_id=track.id, sha256=str(track.sha256 or "")[:64], duration=_num(track.duration),
                            language="ru", state="queued", failed_workers=[], progress={}, result={},
                            requested_by=user_id, created_at=now, updated_at=now)


async def _insert_job(session, track, user_id: int | None, now: datetime) -> bool:
    """Insert one job. A concurrent insert rolls back only this savepoint."""
    try:
        async with session.begin_nested():
            session.add(_fresh_job(track, user_id, now))
            await session.flush()
        return True
    except IntegrityError:
        return False


async def _sweep_lyric_jobs(session, *, user_id: int | None, now: datetime, limit: int = _SWEEP_LIMIT) -> int:
    """Queue songs the phone has not opened yet. One batch per library load or PC poll."""
    created = 0
    silent = list(await session.scalars(select(TranscriptionJob).where(
        TranscriptionJob.state == "failed", TranscriptionJob.error.startswith("no_speech")).limit(20)))
    for job in silent:
        if (job.result or {}).get("retried_empty"):
            continue
        job.result = {**(job.result or {}), "retried_empty": True}
        job.state, job.attempts, job.failed_workers, job.error = "queued", 0, [], ""
        job.finished_at, job.updated_at = None, now
        created += 1
    key = _cursor_key(session)
    cursor = int(_cursors.get(key, 0) or 0)
    looked = 0
    queued = 0
    while queued < limit and looked < _SWEEP_LOOK:
        batch = list(await session.scalars(select(MusicTrack).where(
            MusicTrack.deleted.is_(False), MusicTrack.id > cursor).order_by(MusicTrack.id).limit(50)))
        if not batch:
            cursor = 0
            break
        ids = [track.id for track in batch]
        existing = set(await session.scalars(select(TranscriptionJob.track_id).where(TranscriptionJob.track_id.in_(ids))))
        records = {row.track_id: row for row in await session.scalars(select(MusicEnrichment).where(MusicEnrichment.track_id.in_(ids)))}
        for track in batch:
            cursor = track.id
            looked += 1
            if track.id not in existing and not _stored_lyrics(records.get(track.id)):
                if await _insert_job(session, track, user_id, now):
                    queued += 1
                    created += 1
            if queued >= limit or looked >= _SWEEP_LOOK:
                break
        if queued >= limit or looked >= _SWEEP_LOOK:
            break
    _cursors[key] = cursor
    return created


async def queue_page_without_lyrics(session, tracks, *, user_id: int | None, now: datetime) -> int:
    """Queue this library page, then the next songs in the library that have no text."""
    pending = [track for track in tracks if getattr(track, "id", None) and not getattr(track, "deleted", False)]
    created = 0
    if pending:
        ids = [track.id for track in pending]
        existing = {row.track_id: row for row in await session.scalars(select(TranscriptionJob).where(TranscriptionJob.track_id.in_(ids)))}
        records = {row.track_id: row for row in await session.scalars(select(MusicEnrichment).where(MusicEnrichment.track_id.in_(ids)))}
        for track in pending:
            job = existing.get(track.id)
            if job is not None:
                if (job.state == "failed" and str(job.error or "").startswith("no_speech")
                        and not (job.result or {}).get("retried_empty")):
                    job.result = {**(job.result or {}), "retried_empty": True}
                    job.state, job.attempts, job.failed_workers, job.error = "queued", 0, [], ""
                    job.finished_at, job.updated_at = None, now
                    created += 1
                continue
            if _stored_lyrics(records.get(track.id)):
                continue
            await request_job(session, track, language="ru", user_id=user_id, now=now)
            created += 1
    created += await _sweep_lyric_jobs(session, user_id=user_id, now=now)
    if created:
        await schedule(session, now)
        await session.commit()
    return created


def as_owner_lyrics(result: dict) -> dict:
    """PC result in the /lyrics payload shape ({text, lines:[{time, text}], synced})."""
    lines = [{"time": row["start"], "text": row["text"]} for row in result.get("lines") or []]
    return {"text": "\n".join(row["text"] for row in lines), "lines": lines, "synced": bool(lines),
            "source": "pc_transcription", "status": "transcribed", "automatic": True}


# ---------------------------------------------------------------- worker protocol

async def release_worker_jobs(session, worker: TranscriptionWorker, now: datetime, *, running_job_id: int | None) -> None:
    """Called on each poll: undo offers to a disabled worker, detect lost runs."""
    rows = list(await session.scalars(select(TranscriptionJob).where(
        TranscriptionJob.worker_id == worker.id, TranscriptionJob.state.in_(ACTIVE))))
    for job in rows:
        if job.state == "assigned" and not worker_online(worker, now):
            _requeue(job, "worker_disabled", now, count_attempt=False)
        elif job.state == "running" and job.id != running_job_id:
            # The PC restarted or dropped the job without reporting it.
            _requeue(job, "worker_lost_job", now, count_attempt=True)
        elif job.state == "running":
            job.lease_expires_at = now + timedelta(seconds=RUN_LEASE_SEC)


async def claim_assigned(session, worker: TranscriptionWorker, now: datetime) -> TranscriptionJob | None:
    job = await session.scalar(select(TranscriptionJob).where(
        TranscriptionJob.worker_id == worker.id, TranscriptionJob.state == "assigned").order_by(TranscriptionJob.id))
    if job is None:
        return None
    job.state = "running"
    job.started_at = now
    job.lease_expires_at = now + timedelta(seconds=RUN_LEASE_SEC)
    job.deadline_at = now + timedelta(seconds=max(MIN_DEADLINE_SEC, DEADLINE_FACTOR * _num(job.duration)))
    job.progress = {"stage": "download", "fraction": 0.0}
    job.updated_at = now
    return job


async def owned_running_job(session, worker: TranscriptionWorker, job_id: int) -> TranscriptionJob | None:
    job = await session.get(TranscriptionJob, job_id, populate_existing=True)
    if job is None or job.worker_id != worker.id or job.state != "running":
        return None
    return job


async def renew(session, worker: TranscriptionWorker, job: TranscriptionJob, *, stage: str, fraction: float, now: datetime) -> None:
    job.lease_expires_at = now + timedelta(seconds=RUN_LEASE_SEC)
    job.progress = {"stage": str(stage or "")[:32], "fraction": round(min(1.0, max(0.0, _num(fraction))), 3)}
    job.updated_at = now
    worker.last_seen_at = now


def clean_lines(rows: list, duration: float) -> list[dict]:
    limit = duration + 5 if duration and duration > 0 else 86400
    out = []
    for row in rows[:MAX_LINES]:
        if not isinstance(row, dict):
            continue
        text = " ".join(str(row.get("text") or "").split())[:500]
        start, end = _num(row.get("start"), -1), _num(row.get("end"), -1)
        if not text or start < 0 or start > limit:
            continue
        end = min(max(end, start + 0.2), limit + 1)
        out.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    out.sort(key=lambda item: item["start"])
    return out


async def complete(session, worker: TranscriptionWorker, job: TranscriptionJob, *, lines: list, meta: dict, now: datetime) -> dict:
    cleaned = clean_lines(lines, _num(job.duration))
    if not cleaned:
        _requeue(job, "empty_transcription", now, count_attempt=True)
        await schedule(session, now)
        return {"accepted": False, "state": job.state}
    job.result = {"status": "synced", "synced": True, "source": "pc_transcription", "lines": cleaned,
                  "text": "\n".join(item["text"] for item in cleaned),
                  "language": str(meta.get("language") or job.language)[:8], "model": str(meta.get("model") or "")[:64],
                  "device": str(meta.get("device") or "")[:16], "worker": worker.source_name,
                  "elapsed_sec": round(_num(meta.get("elapsed_sec")), 1), "transcribed_at": now.isoformat()}
    job.state = "done"
    job.error = ""
    job.progress = {"stage": "done", "fraction": 1.0}
    job.lease_expires_at = job.deadline_at = None
    job.finished_at = job.updated_at = now
    worker.last_seen_at = now
    return {"accepted": True, "state": "done", "lines": len(cleaned)}


async def fail(session, worker: TranscriptionWorker, job: TranscriptionJob, *, reason: str, now: datetime) -> str:
    _requeue(job, reason or "worker_error", now, count_attempt=True)
    worker.last_seen_at = now
    await schedule(session, now)
    return job.state
