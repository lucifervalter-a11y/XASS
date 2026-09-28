"""Owner music library, resumable ingestion, media streaming and playback routing."""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import secrets
import shutil
import time
from typing import Literal
from weakref import WeakValueDictionary

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update, func, case
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import get_session
from app.models import AgentCommand, AgentCredential, AppConfig, HeartbeatSource
from app.music_models import MusicEnrichment, MusicPlaylist, MusicSession, MusicTrack, MusicUpload, MusicUploadReceipt
from app.music_playback import canonical_lan_url, current_session, expire_active_transfer, install_transfer_routes, pending_handoff, playback_meta, stale_local_recovery_available
from app.music_playback_models import MusicRemoteCommand, MusicTransfer
from app.services.agent_commands import enqueue_agent_command
from app.services.agent_lifecycle import ensure_agent_attached
from app.services.agent_workspace import AssetUploadTooLarge, read_bounded_body
from app.services.control_status import canonical_web_app_url, source_is_online
from app.services.music_library import CHUNK_BYTES, MAX_ARCHIVE_UPLOAD_BYTES, archive_upload_limit, content_lock, filename, inspect_audio, issue_ticket, track_json, track_path, verify_ticket
from app.services.music_storage import ensure_restore_requested, lock_content, lock_track


class StartUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, le=MAX_ARCHIVE_UPLOAD_BYTES)


class UploadChunk(BaseModel):
    offset: int = Field(ge=0)
    data: str = Field(min_length=1, max_length=4 * ((CHUNK_BYTES + 2) // 3))


# JSON encoders may escape every '/' in base64 as '\\/'. Accept that legal
# representation without allowing a larger decoded block or unbounded bodies.
CHUNK_JSON_BYTES = 2 * 4 * ((CHUNK_BYTES + 2) // 3) + 1024


class FinishUpload(BaseModel):
    async_mode: bool = Field(default=False, alias="async", strict=True)


class EditTrack(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    artist: str | None = Field(default=None, max_length=240)
    album: str | None = Field(default=None, max_length=240)
    favorite: bool | None = None


class PlaylistBody(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    track_ids: list[int] = Field(default_factory=list, max_length=2000)


class MediaTicketBody(BaseModel):
    purpose: Literal["listen", "download"] = "listen"


class ControlBody(BaseModel):
    source_name: str = Field(min_length=1, max_length=128)
    action: Literal["outputs", "play", "pause", "resume", "stop", "seek", "volume", "status"]
    track_id: int | None = Field(default=None, gt=0)
    output_id: str = Field(default="default", max_length=256)
    position_sec: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)
    volume: int = Field(default=70, ge=0, le=100)
    expires_at: int | None = Field(default=None, gt=0)
    lan_url: str | None = Field(default=None, max_length=300)


class RecoverSessionBody(BaseModel):
    session_key: str = Field(min_length=16, max_length=64)
    client_id: str = Field(min_length=1, max_length=128)
    expected_source_key: str = Field(min_length=16, max_length=64)
    expected_revision: int = Field(ge=0, strict=True)
    confirm_stopped: Literal[True]


class SessionBody(BaseModel):
    session_key: str = Field(min_length=16, max_length=64)
    takeover: bool = False
    # Explicit "play on this device" from the owner. Other local players see the
    # new session key and pause; a PC gets a best-effort stop command.
    force: bool = False
    track_id: int | None = Field(default=None, gt=0)
    device: str = Field(default="local", max_length=134)
    state: Literal["playing", "paused", "stopped", "ended", "loading", "error"] = "paused"
    position: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)
    share_site: bool | None = None
    share_discord: bool | None = None
    client_id: str | None = Field(default=None, max_length=128)
    output_id: str | None = Field(default=None, max_length=256)
    volume: int | None = Field(default=None, ge=0, le=100)
    queue: list[int] | None = Field(default=None, max_length=2000)
    repeat_mode: Literal["off", "one", "all"] | None = None


def build_router(settings, require_owner, public_origin):
    # The supported deployment has one backend worker. At most one ZIP is
    # extracted at once and one may wait; uploaded files/receipts survive restart.
    zip_tasks: dict[str, asyncio.Task] = {}
    zip_failures: dict[str, dict] = {}
    zip_slot = asyncio.Semaphore(1)

    @asynccontextmanager
    async def upload_lifespan(_):
        try:
            yield
        finally:
            running = list(zip_tasks.values())
            for task in running:
                task.cancel()
            if running:
                # music_ingest._blocking waits for its worker before removing only
                # its private staging directory. Never unlink the uploaded ZIP here.
                await asyncio.gather(*running, return_exceptions=True)

    router = APIRouter(lifespan=upload_lifespan)
    root = Path(settings.music_root).resolve()
    project = Path(__file__).resolve().parent.parent
    if root == project or root in project.parents:
        raise ValueError("MUSIC_ROOT must be a dedicated music directory")
    locks = WeakValueDictionary()

    def lock(key):
        return locks.setdefault(key, asyncio.Lock())

    def private_root():
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            root.chmod(0o700)

    def part_path(upload_id):
        if len(upload_id) != 32 or any(c not in "0123456789abcdef" for c in upload_id):
            raise HTTPException(404, "Загрузка не найдена")
        candidate = root / (upload_id + ".part")
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise HTTPException(400, "Некорректный путь загрузки")
        path = candidate.resolve()
        if path.parent != root:
            raise HTTPException(400, "Некорректный путь загрузки")
        return path

    async def find_track(session, track_id):
        item = await session.get(MusicTrack, track_id)
        if item is None or item.deleted:
            raise HTTPException(404, "Трек не найден")
        return item

    def processing(upload_id):
        return {"ok": True, "status": "processing", "upload_id": upload_id, "retry_after": 1}

    async def finish_archive(upload_id, original_name, sessions):
        from app.services.music_ingest import IngestError, ingest_path
        path = part_path(upload_id)
        try:
            async with zip_slot:
                # No request/DB transaction stays open during extraction/parsing.
                async with asyncio.timeout(300):
                    imported = await ingest_path(settings, sessions, path, original_name)
                    async with sessions() as session:
                        ids = list(dict.fromkeys(imported.ordered))
                        rows = {track.id: track for track in await session.scalars(select(MusicTrack).where(MusicTrack.id.in_(ids)))}
                        result = {"ok": True, "tracks": [track_json(rows[value]) for value in ids if value in rows],
                            "added": len(imported.added), "duplicates": len(imported.existing), "restored": imported.restored,
                            "skipped": imported.skipped, "errors": imported.errors, "archive": True}
                        if session.bind.dialect.name == "postgresql":
                            from sqlalchemy.dialects.postgresql import insert
                        else:
                            from sqlalchemy.dialects.sqlite import insert
                        await session.execute(insert(MusicUploadReceipt).values(upload_id=upload_id, result=result)
                            .on_conflict_do_nothing(index_elements=["upload_id"]))
                        await session.commit()
                        result = (await session.get(MusicUploadReceipt, upload_id)).result
                    path.unlink(missing_ok=True)  # Only this completed upload's temporary ZIP.
                    zip_failures.pop(upload_id, None)
                    return result
        except asyncio.CancelledError:
            # Shutdown interrupts the job, not the durable upload. A subsequent
            # finish safely retries content-hash deduplication after restart.
            raise
        except IngestError as exc:
            result = {"ok": True, "status": "failed", "upload_id": upload_id,
                "detail": str(exc)[:300], "error_code": "archive_invalid", "retryable": False}
        except Exception as exc:
            logging.getLogger(__name__).warning("Music ZIP processing failed (%s)", type(exc).__name__)
            result = {"ok": True, "status": "failed", "upload_id": upload_id,
                "detail": "Не удалось завершить импорт ZIP. Уже добавленные треки сохранены; можно повторить импорт.",
                "error_code": "archive_processing_failed", "retryable": True}
        finally:
            if zip_tasks.get(upload_id) is asyncio.current_task():
                zip_tasks.pop(upload_id, None)
        # Pending uploads are already capped at 24. Keep failure state bounded
        # independently as well, without logging filenames or exception text.
        if len(zip_failures) >= 24:
            zip_failures.pop(next(iter(zip_failures)))
        zip_failures[upload_id] = result
        return result

    async def audio_response(item, session, *, download=False):
        try:
            path = track_path(root, item.storage_name)
        except ValueError:
            raise HTTPException(404, "Аудиофайл не найден")
        if not path.is_file():
            storage = await ensure_restore_requested(session, settings, item)
            await session.commit()
            pending = storage.get("status") == "restore_pending"
            raise HTTPException(503, {"code": storage.get("status", "file_unavailable"),
                "message": "Трек возвращается с агента. Повторите через несколько секунд." if pending else "Агент с этим треком сейчас недоступен.",
                "storage": storage}, headers={"Retry-After": "3"} if pending else None)
        return FileResponse(path, media_type=item.mime, filename=item.filename,
                            content_disposition_type="attachment" if download else "inline",
                            headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
                                     "X-Content-Type-Options": "nosniff"})

    @router.get("/api/mini/music/library")
    async def library(q: str = "", favorite: bool = False, playlist: int | None = None,
                      offset: int = Query(default=0, ge=0), limit: int = Query(default=2000, ge=1, le=2000),
                      user=Depends(require_owner), session=Depends(get_session)):
        query = select(MusicTrack).where(MusicTrack.deleted.is_(False))
        if favorite:
            query = query.where(MusicTrack.favorite.is_(True))
        if q.strip():
            escaped = q.strip()[:240].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = "%" + escaped + "%"
            query = query.where(MusicTrack.title.ilike(pattern, escape="\\") | MusicTrack.artist.ilike(pattern, escape="\\") | MusicTrack.album.ilike(pattern, escape="\\"))
        playlist_item = await session.get(MusicPlaylist, playlist) if playlist else None
        if playlist:
            if playlist_item is None:
                raise HTTPException(404, "Плейлист не найден")
            query = query.where(MusicTrack.id.in_(playlist_item.track_ids))
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        if playlist_item and playlist_item.track_ids:
            order = {value: index for index, value in enumerate(playlist_item.track_ids)}
            query = query.order_by(case(order, value=MusicTrack.id, else_=len(order)))
        else:
            query = query.order_by(MusicTrack.created_at.desc(), MusicTrack.id.desc())
        rows = list(await session.scalars(query.offset(offset).limit(limit)))
        playlists = list(await session.scalars(select(MusicPlaylist).order_by(MusicPlaylist.id.desc())))
        return {"ok": True, "tracks": [track_json(item) for item in rows],
                "total": total, "offset": offset, "has_more": offset + len(rows) < total,
                "next_offset": offset + len(rows) if offset + len(rows) < total else None,
                "playlists": [{"id": item.id, "name": item.name, "track_ids": item.track_ids} for item in playlists],
                "max_upload_bytes": settings.music_max_upload_bytes, "chunk_bytes": CHUNK_BYTES,
                "max_archive_upload_bytes": archive_upload_limit(settings),
                "formats": ["mp3", "wav", "flac", "ogg", "m4a"]}

    @router.post("/api/mini/music/uploads")
    async def start_upload(payload: StartUpload, user=Depends(require_owner), session=Depends(get_session)):
        try:
            clean_name = (filename(payload.filename[:-4] + ".wav")[:-4] + ".zip"
                          if payload.filename.lower().endswith(".zip") else filename(payload.filename))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        maximum = archive_upload_limit(settings) if clean_name.lower().endswith(".zip") else settings.music_max_upload_bytes
        if payload.size > maximum:
            raise HTTPException(413, "ZIP-архив слишком большой" if clean_name.lower().endswith(".zip") else "Аудиофайл слишком большой")
        private_root()
        # Abandoned partial files are private temporary uploads, never tracks.
        unfinished = ~select(MusicUploadReceipt.upload_id).where(MusicUploadReceipt.upload_id == MusicUpload.id).exists()
        expired = list(await session.scalars(select(MusicUpload).where(MusicUpload.track_id.is_(None), unfinished,
            MusicUpload.created_at < datetime.now(timezone.utc) - timedelta(days=1)).limit(100)))
        for old in expired:
            async with lock(old.id):
                if old.id in zip_tasks or await session.get(MusicUploadReceipt, old.id):
                    continue
                part_path(old.id).unlink(missing_ok=True)
                zip_failures.pop(old.id, None)
                await session.delete(old)
        if expired:
            await session.commit()
        pending = await session.scalar(select(func.count()).select_from(MusicUpload).where(MusicUpload.track_id.is_(None), unfinished))
        if pending >= 24:
            raise HTTPException(429, "Слишком много незавершённых загрузок. Завершите или отмените их")
        if shutil.disk_usage(root).free - payload.size < settings.music_min_free_bytes:
            raise HTTPException(507, "На сервере недостаточно свободного места")
        item = MusicUpload(id=secrets.token_hex(16), filename=clean_name, size=payload.size, owner_id=user.user_id)
        session.add(item)
        await session.commit()
        return {"ok": True, "upload_id": item.id, "offset": 0, "chunk_bytes": CHUNK_BYTES}

    @router.get("/api/mini/music/uploads/{upload_id}")
    async def upload_status(upload_id: str, response: Response, user=Depends(require_owner), session=Depends(get_session)):
        path = part_path(upload_id)
        item = await session.get(MusicUpload, upload_id)
        if item is None or item.owner_id != user.user_id:
            raise HTTPException(404, "Загрузка не найдена")
        response.headers["Cache-Control"] = "private, no-store"
        receipt = await session.get(MusicUploadReceipt, upload_id)
        if receipt:
            if upload_id in zip_tasks:
                return processing(upload_id)
            # Reconcile a shutdown between receipt commit and temporary ZIP
            # cleanup. The immutable receipt proves import already finished.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning("Completed music ZIP cleanup deferred")
            return {**receipt.result, "status": "completed", "upload_id": upload_id}
        if item.track_id:
            return {"ok": True, "status": "completed", "upload_id": upload_id,
                "track": track_json(await find_track(session, item.track_id))}
        if upload_id in zip_tasks:
            response.headers["Retry-After"] = "1"
            return processing(upload_id)
        if upload_id in zip_failures:
            return zip_failures[upload_id]
        created = item.created_at.replace(tzinfo=timezone.utc) if item.created_at.tzinfo is None else item.created_at
        if (datetime.now(timezone.utc) - created).total_seconds() > 86400:
            raise HTTPException(410, "Загрузка истекла, выберите файл заново")
        ready = item.offset == item.size and path.is_file() and path.stat().st_size == item.size
        return {"ok": True, "status": "ready_to_finish" if ready else "uploading", "upload_id": upload_id,
            "offset": item.offset, "size": item.size, "chunk_bytes": CHUNK_BYTES}

    @router.delete("/api/mini/music/uploads/{upload_id}")
    async def cancel_upload(upload_id: str, user=Depends(require_owner), session=Depends(get_session)):
        path = part_path(upload_id)
        async with lock(upload_id):
            item = await session.scalar(select(MusicUpload).where(MusicUpload.id == upload_id).with_for_update())
            if item is None or item.owner_id != user.user_id:
                raise HTTPException(404, "Загрузка не найдена")
            if item.track_id or await session.get(MusicUploadReceipt, upload_id):
                raise HTTPException(409, "Трек уже сохранён в библиотеке")
            if upload_id in zip_tasks:
                raise HTTPException(409, "ZIP уже обрабатывается. Дождитесь результата; добавленные треки сохранятся.")
            # Only the selected, unfinished operation's managed partial file.
            path.unlink(missing_ok=True)
            zip_failures.pop(upload_id, None)
            await session.delete(item)
            await session.commit()
        return {"ok": True}

    @router.put("/api/mini/music/uploads/{upload_id}")
    async def put_chunk(upload_id: str, request: Request, user=Depends(require_owner), session=Depends(get_session)):
        path = part_path(upload_id)
        try:
            body = await read_bounded_body(request, limit=CHUNK_JSON_BYTES)
        except AssetUploadTooLarge as exc:
            raise HTTPException(413, "Блок загрузки превышает допустимый размер") from exc
        try:
            payload = UploadChunk.model_validate_json(body)
        except ValueError as exc:
            raise HTTPException(422, "Некорректный блок загрузки") from exc
        try:
            data = base64.b64decode(payload.data, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(400, "Некорректный блок файла")
        if not 0 < len(data) <= CHUNK_BYTES:
            raise HTTPException(413, "Некорректный размер блока")
        async with lock(upload_id):
            item = await session.scalar(select(MusicUpload).where(MusicUpload.id == upload_id).with_for_update())
            if item is None or item.owner_id != user.user_id:
                raise HTTPException(404, "Загрузка не найдена")
            receipt = await session.get(MusicUploadReceipt, upload_id)
            if receipt:
                return {"ok": True, "offset": item.size, "complete": True}
            if item.track_id:
                return {"ok": True, "offset": item.size, "track_id": item.track_id}
            created = item.created_at.replace(tzinfo=timezone.utc) if item.created_at.tzinfo is None else item.created_at
            if (datetime.now(timezone.utc) - created).total_seconds() > 86400:
                raise HTTPException(410, "Загрузка истекла, выберите файл заново")
            if payload.offset > item.offset or payload.offset + len(data) > item.size:
                raise HTTPException(409, f"Ожидается блок с позиции {item.offset}")
            def write_chunk():
                if payload.offset < item.offset:
                    with path.open("rb") as handle:
                        handle.seek(payload.offset)
                        if handle.read(len(data)) != data:
                            raise ValueError("Повторный блок отличается от сохранённого")
                    return
                if shutil.disk_usage(root).free - len(data) < settings.music_min_free_bytes:
                    raise OSError("Недостаточно свободного места")
                with path.open("r+b" if path.exists() else "wb") as handle:
                    handle.seek(payload.offset)
                    handle.write(data)
                    handle.flush()
                if os.name != "nt":
                    path.chmod(0o600)
            try:
                await asyncio.to_thread(write_chunk)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            except OSError as exc:
                raise HTTPException(507, "Не удалось записать аудио на сервер") from exc
            item.offset = max(item.offset, payload.offset + len(data))
            await session.commit()
            return {"ok": True, "offset": item.offset}

    @router.post("/api/mini/music/uploads/{upload_id}/finish")
    async def finish(upload_id: str, response: Response, payload: FinishUpload | None = None,
                     user=Depends(require_owner), session=Depends(get_session)):
        path = part_path(upload_id)
        async with lock(upload_id):
            item = await session.scalar(select(MusicUpload).where(MusicUpload.id == upload_id).with_for_update())
            if item is None or item.owner_id != user.user_id:
                raise HTTPException(404, "Загрузка не найдена")
            receipt = await session.get(MusicUploadReceipt, upload_id)
            if receipt:
                return {**receipt.result, "status": "completed", "upload_id": upload_id} if payload and payload.async_mode else receipt.result
            if item.track_id:
                return {"ok": True, "track": track_json(await find_track(session, item.track_id))}
            if item.offset != item.size or not path.is_file() or path.stat().st_size != item.size:
                raise HTTPException(409, "Файл ещё не загружен целиком")
            if item.filename.lower().endswith(".zip"):
                original_name = item.filename
                sessions = async_sessionmaker(session.bind, expire_on_commit=False)
                await session.rollback()
                task = zip_tasks.get(upload_id)
                if task is None:
                    if len(zip_tasks) >= 2:
                        raise HTTPException(429, "Уже обрабатываются другие ZIP. Повторите через несколько секунд.", headers={"Retry-After": "2"})
                    zip_failures.pop(upload_id, None)
                    task = asyncio.create_task(finish_archive(upload_id, original_name, sessions))
                    zip_tasks[upload_id] = task
                if payload and payload.async_mode:
                    response.status_code = 202
                    response.headers.update({"Cache-Control": "private, no-store", "Retry-After": "1"})
                    return processing(upload_id)
                # Existing Mini App/older iOS receive their original final
                # receipt. Disconnecting a waiter cannot cancel the import.
                result = await asyncio.shield(task)
                if result.get("status") == "failed":
                    raise HTTPException(422 if not result["retryable"] else 503, result["detail"])
                return result
            try:
                metadata = await asyncio.to_thread(inspect_audio, path, item.filename)
            except Exception as exc:
                raise HTTPException(422, "Файл не распознан как поддерживаемое аудио. Попробуйте MP3 или WAV") from exc
            async with content_lock(root, metadata["sha256"]):
                await lock_content(session, metadata["sha256"])
                existing = await session.scalar(select(MusicTrack).where(MusicTrack.sha256 == metadata["sha256"], MusicTrack.deleted.is_(False)))
                if existing:
                    await lock_track(session, existing.id)
                    target = track_path(root, existing.storage_name)
                    restored = not target.is_file()
                    if restored:
                        # Keep the original track identity and every playlist reference.
                        await asyncio.to_thread(shutil.copyfile, path, target)
                        if os.name != "nt":
                            target.chmod(0o600)
                    item.track_id = existing.id
                    await session.commit()
                    path.unlink(missing_ok=True)  # Only this operation's managed temporary duplicate.
                    return {"ok": True, "track": track_json(existing), "duplicate": True, "restored": restored}
                storage_name = secrets.token_hex(16) + Path(item.filename).suffix.lower()
                target = track_path(root, storage_name)
                track = MusicTrack(**metadata, filename=item.filename, storage_name=storage_name)
                session.add(track)
                await session.flush()
                path.replace(target)
                item.track_id = track.id
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()
                    # Leave the resumable upload intact if the database rejects it.
                    if target.is_file() and not path.exists():
                        target.replace(path)
                    raise
                return {"ok": True, "track": track_json(track)}

    @router.patch("/api/mini/music/tracks/{track_id}")
    async def edit(track_id: int, payload: EditTrack, user=Depends(require_owner), session=Depends(get_session)):
        if {"title", "artist", "album"}.intersection(payload.model_fields_set):
            from app.music_enrichment_api import lock_track_row
            item = await lock_track_row(session, track_id)
        else:
            item = await find_track(session, track_id)
        for key, value in payload.model_dump(exclude_none=True).items():
            cleaned = value.strip() if isinstance(value, str) else value
            if key == "title" and not cleaned:
                raise HTTPException(400, "Укажите название трека")
            setattr(item, key, cleaned)
        if {"title", "artist", "album"}.intersection(payload.model_fields_set):
            cached = await session.get(MusicEnrichment, track_id)
            if not cached:
                from app.music_enrichment_api import identity, fingerprint
                cached = MusicEnrichment(track_id=track_id, original=identity(item), fingerprint=fingerprint(item), result={})
                session.add(cached)
            cached.dismissed = True
            cached.revision = (cached.revision or 0) + 1
            cached.artwork_data = None
        await session.commit()
        return {"ok": True, "track": track_json(item)}

    @router.delete("/api/mini/music/tracks/{track_id}")
    async def remove(track_id: int, user=Depends(require_owner), session=Depends(get_session)):
        item = await find_track(session, track_id)
        item.deleted = True
        # A soft delete revokes all stream tickets immediately; bytes stay recoverable.
        for playlist in await session.scalars(select(MusicPlaylist)):
            playlist.track_ids = [value for value in playlist.track_ids if value != track_id]
        await session.commit()
        return {"ok": True, "file_preserved": True}

    @router.post("/api/mini/music/playlists")
    async def create_playlist(payload: PlaylistBody, user=Depends(require_owner), session=Depends(get_session)):
        ids = list(dict.fromkeys(payload.track_ids))
        available = set(await session.scalars(select(MusicTrack.id).where(MusicTrack.id.in_(ids), MusicTrack.deleted.is_(False))))
        if len(available) != len(ids) or not payload.name.strip():
            raise HTTPException(400, "Проверьте название и треки плейлиста")
        item = MusicPlaylist(name=payload.name.strip(), track_ids=ids)
        session.add(item)
        await session.commit()
        return {"ok": True, "playlist": {"id": item.id, "name": item.name, "track_ids": ids}}

    @router.put("/api/mini/music/playlists/{playlist_id}")
    async def edit_playlist(playlist_id: int, payload: PlaylistBody, user=Depends(require_owner), session=Depends(get_session)):
        item = await session.get(MusicPlaylist, playlist_id)
        if item is None:
            raise HTTPException(404, "Плейлист не найден")
        ids = list(dict.fromkeys(payload.track_ids))
        available = set(await session.scalars(select(MusicTrack.id).where(MusicTrack.id.in_(ids), MusicTrack.deleted.is_(False))))
        if len(available) != len(ids) or not payload.name.strip():
            raise HTTPException(400, "Проверьте название и треки плейлиста")
        item.name, item.track_ids = payload.name.strip(), ids
        await session.commit()
        return {"ok": True, "playlist": {"id": item.id, "name": item.name, "track_ids": ids}}

    @router.delete("/api/mini/music/playlists/{playlist_id}")
    async def remove_playlist(playlist_id: int, user=Depends(require_owner), session=Depends(get_session)):
        item = await session.get(MusicPlaylist, playlist_id)
        if item:
            await session.delete(item)
            await session.commit()
        return {"ok": True}

    @router.get("/api/mini/music/tracks/{track_id}/lyrics")
    async def lyrics(track_id: int, response: Response, user=Depends(require_owner), session=Depends(get_session)):
        from app.services.music_lyrics import embedded_lyrics
        track = await find_track(session, track_id)
        response.headers["Cache-Control"] = "private, no-store"
        record = await session.get(MusicEnrichment, track_id)
        if record and record.owner_lyrics and not record.owner_lyrics.get("disabled", False):
            return {"ok": True, "lyrics": record.owner_lyrics}
        embedded = await asyncio.to_thread(embedded_lyrics, root, track)
        if embedded["text"]:
            return {"ok": True, "lyrics": embedded}
        from app.music_enrichment_api import enrich_saved_track
        from app.services.transcription_queue import as_owner_lyrics, done_result
        transcription = await done_result(session, track_id)
        result = await enrich_saved_track(session, track_id)
        value = dict(result["enrichment"].get("lyrics") or embedded)
        if transcription and not value.get("synced"):
            # Catalog first; the finished PC transcription only fills a gap.
            value = as_owner_lyrics(transcription)
        value.setdefault("status", result["enrichment"].get("lookup_status") or result["enrichment"].get("status", "not_found"))
        return {"ok": True, "lyrics": value, "track": result["track"]}

    from app.services.synced_lyrics import LrclibClient, LyricsCache, resolve as resolve_synced_lyrics
    lyrics_cache = LyricsCache(root.parent / "music-lyrics-cache")
    lyrics_client = LrclibClient()

    @router.get("/api/mini/music/tracks/{track_id}/timed-lyrics")
    async def timed_lyrics(track_id: int, response: Response, refresh: bool = False,
                           user=Depends(require_owner), session=Depends(get_session)):
        """Apple-Music-style lines [{start, end, text}] for local playback and prefetch."""
        from app.services.music_lyrics import embedded_lyrics
        track = await find_track(session, track_id)
        response.headers["Cache-Control"] = "private, no-store"
        record = await session.get(MusicEnrichment, track_id)
        owner = record.owner_lyrics if record and record.owner_lyrics else None
        enrichment = (record.result or {}).get("lyrics") if record and not record.dismissed and isinstance(record.result, dict) else None
        embedded = await asyncio.to_thread(embedded_lyrics, root, track)
        from types import SimpleNamespace
        snapshot = SimpleNamespace(**{key: getattr(track, key) for key in
            ("id", "title", "artist", "album", "duration", "filename", "sha256")})
        from app.services.transcription_queue import done_result, job_state
        # A finished PC transcription is used only when the catalog has no timed lyrics.
        transcription = await done_result(session, track_id)
        pending = await job_state(session, track_id) in {"queued", "assigned", "running"}
        # Release the DB connection while the provider is contacted.
        await session.rollback()
        value = await resolve_synced_lyrics(snapshot, owner=owner, embedded=embedded, enrichment=enrichment,
                                            cache=lyrics_cache, client=lyrics_client, refresh=refresh,
                                            transcription=transcription)
        if pending and not value.get("synced"):
            # Lets Now Playing re-check soon instead of caching "no lyrics" for hours.
            value["transcription_pending"] = True
        return {"ok": True, "lyrics": value}

    async def catalog_has_timed_lyrics(session, track) -> bool:
        """Embedded LRC or LRCLIB (cached per fingerprint) already has timed lines."""
        from types import SimpleNamespace
        from app.services.music_lyrics import embedded_lyrics
        record = await session.get(MusicEnrichment, track.id)
        enrichment = (record.result or {}).get("lyrics") if record and not record.dismissed and isinstance(record.result, dict) else None
        snapshot = SimpleNamespace(**{key: getattr(track, key) for key in
            ("id", "title", "artist", "album", "duration", "filename", "sha256", "deleted", "storage_name", "size")})
        await session.rollback()
        embedded = await asyncio.to_thread(embedded_lyrics, root, snapshot)
        value = await resolve_synced_lyrics(snapshot, owner=None, embedded=embedded, enrichment=enrichment,
                                            cache=lyrics_cache, client=lyrics_client)
        return bool(value.get("synced") and value.get("lines"))

    from app.music_transcription_api import build_router as build_transcription_router
    router.include_router(build_transcription_router(settings, require_owner, catalog_has_timed_lyrics))

    @router.get("/api/mini/music/tracks/{track_id}/artwork")
    async def artwork(track_id: int, user=Depends(require_owner), session=Depends(get_session)):
        from app.services.music_artwork import artwork_thumbnail
        track = await find_track(session, track_id)
        path = await asyncio.to_thread(artwork_thumbnail, root, track)
        if path is None:
            cached = await session.get(MusicEnrichment, track_id)
            if cached and not cached.dismissed and cached.artwork_data:
                return Response(cached.artwork_data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
            raise HTTPException(404, "Обложка пока не найдена")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    @router.post("/api/mini/music/tracks/{track_id}/ticket")
    async def ticket(track_id: int, payload: MediaTicketBody, user=Depends(require_owner), session=Depends(get_session)):
        track = await find_track(session, track_id)
        ttl = max(3600, int(track.duration) + 300) if payload.purpose == "listen" else 3600
        value = issue_ticket(settings, track_id, purpose=payload.purpose, ttl=ttl)
        return {"ok": True, "path": f"/api/music/tracks/{track_id}/stream?ticket={value}", "expires_in": ttl}

    @router.api_route("/api/music/tracks/{track_id}/stream", methods=["GET", "HEAD"])
    async def stream(track_id: int, ticket: str = "", session=Depends(get_session)):
        auth = verify_ticket(settings, ticket, track_id, purposes=("listen", "download", "public"))
        if auth is None:
            raise HTTPException(401, "Ссылка на аудио истекла. Запустите трек заново")
        if auth["p"] == "public":
            from app.services.music_broadcast import current_broadcast
            playing = await current_broadcast(session)
            if playing is None or playing[0].id != track_id:
                raise HTTPException(403, "Трансляция завершена")
        return await audio_response(await find_track(session, track_id), session, download=auth["p"] == "download")

    @router.get("/api/music/public")
    async def public_music(response: Response, session=Depends(get_session)):
        from app.services.music_broadcast import current_broadcast
        response.headers["Cache-Control"] = "no-store"
        playing = await current_broadcast(session)
        if playing is None:
            return {"ok": True, "playing": False}
        track, position = playing
        token = issue_ticket(settings, track.id, purpose="public", ttl=300)
        return {"ok": True, "playing": True, "track": {key: getattr(track, key) for key in ("id", "title", "artist", "duration", "mime")},
                "position": position, "path": f"/api/music/tracks/{track.id}/stream?ticket={token}"}

    @router.api_route("/agent/music/tracks/{track_id}/stream", methods=["GET", "HEAD"])
    async def agent_stream(track_id: int, ticket: str = "", session=Depends(get_session)):
        auth = verify_ticket(settings, ticket, track_id, purposes=("agent",))
        if auth is None:
            raise HTTPException(401, "Ссылка на аудио истекла")
        credential = await session.scalar(select(AgentCredential).where(AgentCredential.api_key_hash == auth["b"], AgentCredential.is_active.is_(True)))
        if credential is None:
            raise HTTPException(403, "Агент отвязан")
        return await audio_response(await find_track(session, track_id), session)

    @router.get("/api/mini/music/players")
    async def players(user=Depends(require_owner), session=Depends(get_session)):
        credentials = set(await session.scalars(select(AgentCredential.source_name).where(AgentCredential.is_active.is_(True))))
        rows = []
        for source in await session.scalars(select(HeartbeatSource).where(HeartbeatSource.source_type == "PC_AGENT")):
            payload = source.last_payload or {}
            online = source_is_online(source, 2)
            details = payload.get("music_player")
            rows.append({"source_name": source.source_name, "online": online,
                         "available": online and source.source_name in credentials and isinstance(details, dict),
                         "music_player": details if isinstance(details, dict) else {},
                         "agent_version": str(payload.get("agent_version") or "0.0.0")})
        return {"ok": True, "players": rows}

    @router.post("/api/mini/music/control")
    async def control(payload: ControlBody, request: Request, user=Depends(require_owner), session=Depends(get_session)):
        source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == payload.source_name))
        if source is None or source.source_type != "PC_AGENT" or not source_is_online(source, 2):
            raise HTTPException(409, "Компьютер не в сети")
        credential = await session.scalar(select(AgentCredential).where(AgentCredential.source_name == source.source_name, AgentCredential.is_active.is_(True)))
        if credential is None:
            raise HTTPException(409, "Перепривяжите ПК с индивидуальным ключом")
        if payload.action in {"play", "pause", "stop"}:
            from app.services.music_agent_queue import cancel_agent_queue
            await cancel_agent_queue(session, source.source_name)
        await ensure_agent_attached(session, source.source_name)
        if not isinstance((source.last_payload or {}).get("music_player"), dict):
            raise HTTPException(409, "Обновите агент XASS до версии 0.16.0 или новее")
        details = {"output_id": payload.output_id, "volume": payload.volume, "position_sec": payload.position_sec,
                   "expires_at": min(int(time.time()) + 120, payload.expires_at or int(time.time()) + 120)}
        if payload.action == "play":
            if payload.track_id is None:
                raise HTTPException(400, "Выберите трек")
            track = await find_track(session, payload.track_id)
            if track.mime == "audio/mp4":
                raise HTTPException(415, "Для воспроизведения M4A на ПК загрузите версию MP3 или WAV")
            value = issue_ticket(settings, track.id, purpose="agent", binding=credential.api_key_hash, ttl=600)
            media_path = f"/agent/music/tracks/{track.id}/stream?ticket={value}"
            config = await session.get(AppConfig, 1)
            web_url = canonical_web_app_url(config.service_base_url if config else "", settings.profile_public_url, public_origin(request)[1])
            origin = web_url.split("/miniapp.php", 1)[0]
            details.update(track_id=track.id, title=track.title, artist=track.artist, url=origin + media_path, media_path=media_path)
            offered = canonical_lan_url(payload.lan_url, track.id) if payload.lan_url else None
            if offered:
                details["lan_url"] = offered
        # Superseded transient controls must not burst into playback after reconnect.
        if payload.action in {"play", "seek", "volume"}:
            await session.execute(update(AgentCommand).where(AgentCommand.source_name == source.source_name,
                AgentCommand.command == "music_" + payload.action, AgentCommand.status == "pending").values(status="cancelled"))
        item = await enqueue_agent_command(session, source_name=source.source_name, command="music_" + payload.action,
                                           payload=details, actor_user_id=user.user_id)
        return {"ok": True, "command_id": item.id, "status": item.status}

    @router.get("/api/mini/music/control/{command_id}")
    async def command_status(command_id: int, user=Depends(require_owner), session=Depends(get_session)):
        item = await session.get(AgentCommand, command_id)
        if item is None or not item.command.startswith("music_"):
            raise HTTPException(404, "Команда не найдена")
        return {"ok": True, "status": item.status, "result": item.result}

    @router.get("/api/mini/music/session")
    async def session_state(user=Depends(require_owner), session=Depends(get_session)):
        await expire_active_transfer(session)
        return {"ok": True, "session": await current_session(session)}

    @router.post("/api/mini/music/session")
    async def publish(payload: SessionBody, request: Request, user=Depends(require_owner), session=Depends(get_session)):
        await expire_active_transfer(session)
        meta = await playback_meta(session)
        transfer = await pending_handoff(session, payload.session_key)
        if transfer:
            raise HTTPException(409, {"code": "transfer_requested", "transfer_id": transfer.id,
                                      "message": "Приостановите воспроизведение для переключения"})
        active_transfer = await session.get(MusicTransfer, meta.transfer_id) if meta.transfer_id else None
        if active_transfer and active_transfer.status not in {"ready", "failed"}:
            raise HTTPException(409, {"code": "transfer_pending", "message": "Дождитесь подтверждения переключения"})
        if payload.share_discord:
            raise HTTPException(409, "Для звука в Discord выберите виртуальный выход ПК; автоматическое подключение Discord не настроено")
        if payload.track_id:
            await find_track(session, payload.track_id)
        # Idempotent singleton creation also serializes concurrent control tabs.
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        await session.execute(insert(MusicSession).values(id=1).on_conflict_do_nothing(index_elements=["id"]))
        item = await session.scalar(select(MusicSession).where(MusicSession.id == 1).with_for_update())
        changed_player = bool(item.session_key and (item.session_key != payload.session_key or
            ("device" in payload.model_fields_set and item.device != payload.device)))
        stop_pc = ""
        if changed_player and item.state in {"playing", "loading"}:
            from app.music_playback import pc_source_released
            released = item.device.startswith("agent:") and await pc_source_released(session, item)
            if not (payload.takeover and (payload.force or released)):
                raise HTTPException(409, {"code": "transfer_required", "source_device": item.device,
                    "message": "Переключите устройство через «Где слушать», чтобы сохранить позицию и остановить старый плеер"})
            if item.device.startswith("agent:") and item.device != payload.device:
                stop_pc = item.device[6:]
        if item.session_key and item.session_key != payload.session_key and not payload.takeover:
            raise HTTPException(409, "Воспроизведение уже изменено на другом устройстве")
        queue_command = await session.get(AgentCommand, meta.queue_command_id) if meta.queue_command_id else None
        agent_queue_owned = bool(item.device.startswith("agent:") and queue_command
            and queue_command.source_name == item.device[6:]
            and (queue_command.payload or {}).get("queue_managed") is True
            and queue_command.payload.get("queue_session_key") == item.session_key
            and queue_command.status in {"awaiting_media", "pending", "delivered", "completed", "failed"})
        if agent_queue_owned and payload.model_fields_set & {"track_id", "device", "state", "position"}:
            # A controller may be polling a pre-transition snapshot. Only the
            # authenticated PC heartbeat can report queue playback state; even
            # a same-key browser must not overwrite an atomic next-track lease.
            raise HTTPException(409, {"code": "agent_playback_authoritative",
                "message": "Состояние воспроизведения ПК обновляет агент. Обновите плеер; отправляйте отдельно только настройки очереди или публикации."})
        if payload.share_site and not item.share_site:
            from app.services.profile_editor import load_profile
            previous = load_profile(Path(settings.profile_json_path)).get("now_listening_source") or "pc_agent"
            if previous != "xass_music":
                item.previous_source = previous
        meta_fields = {"client_id", "output_id", "volume", "queue", "repeat_mode"}
        for key, value in payload.model_dump(exclude={"takeover", "force"} | meta_fields, exclude_unset=True).items():
            setattr(item, key, value)
        for key in meta_fields:
            if key in payload.model_fields_set and getattr(payload, key) is not None:
                setattr(meta, key, getattr(payload, key))
        meta.revision += 1
        if not agent_queue_owned:
            item.updated_at = datetime.now(timezone.utc)
        await session.commit()
        if stop_pc:
            try:
                await control(ControlBody(source_name=stop_pc, action="stop",
                    expires_at=int(time.time()) + 60), request, user, session)
            except HTTPException:
                pass  # Offline PC: nothing is audible there that the server could stop.
        from app.services.music_broadcast import sync_music_profile
        await sync_music_profile(session, settings)
        return {"ok": True, "session": await current_session(session)}

    @router.post("/api/mini/music/session/recover")
    async def recover_session(payload: RecoverSessionBody, user=Depends(require_owner), session=Depends(get_session)):
        # Use the same lock order as publish/handoff. Staleness is not evidence
        # that a disconnected player is silent: the owner must confirm this.
        # Do not expire/fake-ACK a handoff or enqueue any playback command here.
        meta = await playback_meta(session)
        item = await session.scalar(select(MusicSession).where(MusicSession.id == 1).with_for_update())
        transfer = await session.get(MusicTransfer, meta.transfer_id) if meta.transfer_id else None
        if (not stale_local_recovery_available(item, meta, transfer)
                or item.session_key != payload.expected_source_key
                or meta.revision != payload.expected_revision
                or payload.session_key == payload.expected_source_key):
            raise HTTPException(409, {"code": "stale_session_recovery_unavailable",
                "message": "Сессия изменилась или прежний плеер ещё на связи. Обновите состояние; восстановление доступно только после пяти минут без обновлений и остановки звука на прежнем устройстве."})
        await session.execute(update(MusicRemoteCommand).where(
            MusicRemoteCommand.session_key == item.session_key,
            MusicRemoteCommand.status == "pending").values(status="cancelled", error="Владелец восстановил управление"))
        item.session_key = payload.session_key
        item.state = "paused"
        item.updated_at = datetime.now(timezone.utc)
        meta.client_id = payload.client_id
        meta.transfer_id = ""
        meta.revision += 1
        await session.commit()
        result = {"ok": True, "session": await current_session(session)}
        from app.services.music_broadcast import sync_music_profile
        try:
            await sync_music_profile(session, settings)
        except Exception:
            # The lease is already safely recovered. A public-profile I/O fault
            # must not turn that committed operation into an ambiguous failure.
            logging.getLogger(__name__).warning("Music recovery completed; public profile refresh deferred")
            await session.rollback()
        return result

    install_transfer_routes(router, settings, require_owner, control, ControlBody)
    from app.music_remote_control import install_remote_control_routes
    install_remote_control_routes(router, require_owner)
    from app.music_enrichment_api import build_router as enrichment_router
    router.include_router(enrichment_router(require_owner))
    return router
