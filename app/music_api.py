"""Owner music library, resumable ingestion, media streaming and playback routing."""
from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import datetime, timedelta, timezone
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
from app.music_models import MusicPlaylist, MusicSession, MusicTrack, MusicUpload, MusicUploadReceipt
from app.music_playback import current_session, install_transfer_routes, pending_handoff, playback_meta
from app.music_playback_models import MusicTransfer
from app.services.agent_commands import enqueue_agent_command
from app.services.agent_lifecycle import ensure_agent_attached
from app.services.control_status import canonical_web_app_url, source_is_online
from app.services.music_library import CHUNK_BYTES, content_lock, filename, inspect_audio, issue_ticket, track_json, track_path, verify_ticket
from app.services.music_storage import ensure_restore_requested, lock_content, lock_track


class StartUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, le=256 * 1024 * 1024)


class UploadChunk(BaseModel):
    offset: int = Field(ge=0)
    data: str = Field(min_length=1, max_length=4 * ((CHUNK_BYTES + 2) // 3))


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


class SessionBody(BaseModel):
    session_key: str = Field(min_length=16, max_length=64)
    takeover: bool = False
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
    router = APIRouter()
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
        path = (root / (upload_id + ".part")).resolve()
        if path.parent != root:
            raise HTTPException(400, "Некорректный путь загрузки")
        return path

    async def find_track(session, track_id):
        item = await session.get(MusicTrack, track_id)
        if item is None or item.deleted:
            raise HTTPException(404, "Трек не найден")
        return item

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
                "formats": ["mp3", "wav", "flac", "ogg", "m4a"]}

    @router.post("/api/mini/music/uploads")
    async def start_upload(payload: StartUpload, user=Depends(require_owner), session=Depends(get_session)):
        try:
            clean_name = (filename(payload.filename[:-4] + ".wav")[:-4] + ".zip"
                          if payload.filename.lower().endswith(".zip") else filename(payload.filename))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if payload.size > settings.music_max_upload_bytes:
            raise HTTPException(413, "Аудиофайл слишком большой")
        private_root()
        # Abandoned partial files are private temporary uploads, never tracks.
        unfinished = ~select(MusicUploadReceipt.upload_id).where(MusicUploadReceipt.upload_id == MusicUpload.id).exists()
        expired = list(await session.scalars(select(MusicUpload).where(MusicUpload.track_id.is_(None), unfinished,
            MusicUpload.created_at < datetime.now(timezone.utc) - timedelta(days=1)).limit(100)))
        for old in expired:
            async with lock(old.id):
                part_path(old.id).unlink(missing_ok=True)
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

    @router.delete("/api/mini/music/uploads/{upload_id}")
    async def cancel_upload(upload_id: str, user=Depends(require_owner), session=Depends(get_session)):
        path = part_path(upload_id)
        async with lock(upload_id):
            item = await session.scalar(select(MusicUpload).where(MusicUpload.id == upload_id).with_for_update())
            if item is None or item.owner_id != user.user_id:
                raise HTTPException(404, "Загрузка не найдена")
            if item.track_id or await session.get(MusicUploadReceipt, upload_id):
                raise HTTPException(409, "Трек уже сохранён в библиотеке")
            # Only the selected, unfinished operation's managed partial file.
            path.unlink(missing_ok=True)
            await session.delete(item)
            await session.commit()
        return {"ok": True}

    @router.put("/api/mini/music/uploads/{upload_id}")
    async def put_chunk(upload_id: str, payload: UploadChunk, user=Depends(require_owner), session=Depends(get_session)):
        path = part_path(upload_id)
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
    async def finish(upload_id: str, user=Depends(require_owner), session=Depends(get_session)):
        path = part_path(upload_id)
        async with lock(upload_id):
            item = await session.scalar(select(MusicUpload).where(MusicUpload.id == upload_id).with_for_update())
            if item is None or item.owner_id != user.user_id:
                raise HTTPException(404, "Загрузка не найдена")
            receipt = await session.get(MusicUploadReceipt, upload_id)
            if receipt:
                return receipt.result
            if item.track_id:
                return {"ok": True, "track": track_json(await find_track(session, item.track_id))}
            if item.offset != item.size or not path.is_file() or path.stat().st_size != item.size:
                raise HTTPException(409, "Файл ещё не загружен целиком")
            if item.filename.lower().endswith(".zip"):
                from app.services.music_ingest import IngestError, ingest_path
                original_name = item.filename
                sessions = async_sessionmaker(session.bind, expire_on_commit=False)
                # The shared importer commits one deduplicated track at a time.
                # Release the request transaction before opening its sessions.
                await session.rollback()
                try:
                    imported = await ingest_path(settings, sessions, path, original_name)
                except IngestError as exc:
                    raise HTTPException(422, str(exc)) from exc
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
                path.unlink(missing_ok=True)  # Only this completed upload's managed temporary ZIP.
                return (await session.get(MusicUploadReceipt, upload_id)).result
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
        item = await find_track(session, track_id)
        for key, value in payload.model_dump(exclude_none=True).items():
            cleaned = value.strip() if isinstance(value, str) else value
            if key == "title" and not cleaned:
                raise HTTPException(400, "Укажите название трека")
            setattr(item, key, cleaned)
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

    @router.get("/api/mini/music/tracks/{track_id}/artwork")
    async def artwork(track_id: int, user=Depends(require_owner), session=Depends(get_session)):
        from app.services.music_artwork import artwork_thumbnail
        track = await find_track(session, track_id)
        path = await asyncio.to_thread(artwork_thumbnail, root, track)
        if path is None:
            raise HTTPException(404, "В файле нет обложки")
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
        return {"ok": True, "session": await current_session(session)}

    @router.post("/api/mini/music/session")
    async def publish(payload: SessionBody, user=Depends(require_owner), session=Depends(get_session)):
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
        if changed_player and item.state in {"playing", "loading"}:
            raise HTTPException(409, {"code": "transfer_required", "message": "Переключите устройство через «Где слушать», чтобы сохранить позицию и остановить старый плеер"})
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
        for key, value in payload.model_dump(exclude={"takeover"} | meta_fields, exclude_unset=True).items():
            setattr(item, key, value)
        for key in meta_fields:
            if key in payload.model_fields_set and getattr(payload, key) is not None:
                setattr(meta, key, getattr(payload, key))
        meta.revision += 1
        if not agent_queue_owned:
            item.updated_at = datetime.now(timezone.utc)
        await session.commit()
        from app.services.music_broadcast import sync_music_profile
        await sync_music_profile(session, settings)
        return {"ok": True, "session": await current_session(session)}

    install_transfer_routes(router, settings, require_owner, control, ControlBody)
    from app.music_remote_control import install_remote_control_routes
    install_remote_control_routes(router, require_owner)
    return router
