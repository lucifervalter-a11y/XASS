"""Owner-directed cold replicas. Agent credentials cannot choose another agent's job."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import secrets
import shutil
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.db import get_session
from app.models import AdminAction, AgentCredential, HeartbeatSource, utcnow
from app.music_models import MusicTrack, MusicPlaylist
from app.music_storage_models import MusicStorageCopy, MusicStorageJob, MusicImportRun, MusicImportFile, MusicProviderCheck
from app.services.agent_lifecycle import ensure_agent_attached
from app.services.agent_pairing import authenticate_agent_api_key
from app.services.music_library import filename, inspect_audio
from app.services.music_storage import (CHUNK_BYTES, MAX_BYTES, checksum, managed_path, part_path,
    write_chunk, lock_track, lock_content, online_credential, create_job, ensure_restore_requested, job_json,
    vk_capabilities, check_vk_capabilities)


class Target(BaseModel):
    source_name: str = Field(min_length=1, max_length=128)


class Evict(Target):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm: str
    action_proof: str = Field(default="", max_length=4096)


class Ack(BaseModel):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(gt=0, le=MAX_BYTES)


class ImportRequest(Target):
    root: str = Field(pattern=r"^(desktop|downloads|documents|xass_files)$")
    path: str = Field(default="", max_length=1024)
    confirm: str


class ImportItem(Ack):
    path_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    filename: str = Field(min_length=1, max_length=255)
    ordinal: int = Field(ge=0, le=100000)


class ImportFinish(BaseModel):
    file_ids: list[str] = Field(max_length=100000)
    skipped: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)


async def bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > CHUNK_BYTES:
            raise HTTPException(413, "Блок больше 512 КБ")
    return bytes(body)


def build_router(settings, require_owner, require_action_proof):
    router = APIRouter()

    def vk_token():
        from app.services.profile_editor import load_profile
        profile = load_profile(Path(settings.profile_json_path))
        return str(profile.get("vk_access_token") or getattr(settings, "vk_access_token", "") or "").strip()

    @router.get("/api/mini/music/providers/vk")
    async def vk_status(user=Depends(require_owner), session=Depends(get_session)):
        token = vk_token()
        saved = await session.get(MusicProviderCheck, "vk")
        if saved and saved.token_fingerprint == hashlib.sha256(token.encode()).hexdigest():
            checked = saved.checked_at.replace(tzinfo=timezone.utc) if saved.checked_at.tzinfo is None else saved.checked_at
            if (datetime.now(timezone.utc) - checked).total_seconds() <= 300:
                return {"ok": True, **saved.result, "checked_at": saved.checked_at.isoformat()}
            return {"ok": True, **vk_capabilities(bool(token)), "token_status": "recheck_required",
                    "checked_at": saved.checked_at.isoformat()}
        return {"ok": True, **vk_capabilities(bool(token)), "checked_at": None}

    @router.post("/api/mini/music/providers/vk/check")
    async def vk_check(user=Depends(require_owner), session=Depends(get_session)):
        token = vk_token()
        result = await check_vk_capabilities(token, getattr(settings, "vk_api_version", "5.199"))
        value = await session.get(MusicProviderCheck, "vk")
        if value is None:
            value = MusicProviderCheck(provider="vk", token_fingerprint="")
            session.add(value)
        value.token_fingerprint, value.result, value.checked_at = hashlib.sha256(token.encode()).hexdigest(), result, utcnow()
        await session.commit()
        return {"ok": True, **result, "checked_at": value.checked_at.isoformat()}

    async def agent(x_api_key: str | None = Header(default=None), session=Depends(get_session)):
        auth = await authenticate_agent_api_key(session, api_key=x_api_key,
            global_agent_api_key=getattr(settings, "agent_api_key", ""))
        if not auth:
            raise HTTPException(401, "Invalid agent key")
        if not auth.credential_id:
            raise HTTPException(403, "Для музыкального хранилища привяжите агент отдельным ключом")
        await ensure_agent_attached(session, auth.source_name)
        return auth

    async def track(session, track_id):
        value = await session.get(MusicTrack, track_id)
        if value is None or value.deleted:
            raise HTTPException(404, "Трек не найден")
        return value

    async def target(session, name):
        value = await session.scalar(select(AgentCredential).where(
            AgentCredential.source_name == name, AgentCredential.is_active.is_(True)))
        if not value:
            raise HTTPException(409, "Агент не привязан отдельным действующим ключом")
        await ensure_agent_attached(session, name)
        source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == name))
        capability = (source.last_payload or {}).get("music_storage") if source else None
        if not isinstance(capability, dict) or capability.get("version") != 1:
            raise HTTPException(409, "Обновите агент до версии с музыкальным хранилищем")
        return value

    async def job(session, job_id, auth):
        value = await session.get(MusicStorageJob, job_id)
        if not value or value.credential_id != auth.credential_id:
            raise HTTPException(404, "Передача не найдена")
        item = await track(session, value.track_id)
        if item.sha256 != value.sha256 or item.size != value.size:
            raise HTTPException(409, "Версия трека изменилась")
        await lock_track(session, item.id)
        return value, item

    @router.get("/api/mini/music/storage")
    async def storage(track_id: int | None = None, user=Depends(require_owner), session=Depends(get_session)):
        credentials = list(await session.scalars(select(AgentCredential).where(AgentCredential.is_active.is_(True))))
        targets = []
        for credential in credentials:
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == credential.source_name))
            capability = (source.last_payload or {}).get("music_storage") if source else None
            targets.append({"source_name": credential.source_name,
                "online": await online_credential(session, credential.id),
                "supported": isinstance(capability, dict) and capability.get("version") == 1})
        query = select(MusicStorageJob).order_by(MusicStorageJob.created_at.desc()).limit(100)
        copies_query = select(MusicStorageCopy)
        if track_id is not None:
            query = query.where(MusicStorageJob.track_id == track_id)
            copies_query = copies_query.where(MusicStorageCopy.track_id == track_id)
        copies = []
        for copy in await session.scalars(copies_query):
            credential = await session.get(AgentCredential, copy.credential_id)
            copies.append({"track_id": copy.track_id, "source_name": credential.source_name if credential else "",
                "sha256": copy.sha256, "size": copy.size, "verified_at": copy.verified_at.isoformat(),
                "online": await online_credential(session, copy.credential_id),
                "attached": bool(credential and credential.is_active)})
        result = {"ok": True, "targets": targets, "copies": copies,
                  "jobs": [job_json(value) for value in await session.scalars(query)]}
        if track_id is not None:
            result["server_available"] = managed_path(settings, await track(session, track_id)).is_file()
        return result

    @router.post("/api/mini/music/storage/{track_id}/replicate")
    async def replicate(track_id: int, payload: Target, user=Depends(require_owner), session=Depends(get_session)):
        item, credential = await track(session, track_id), await target(session, payload.source_name)
        if not managed_path(settings, item).is_file():
            raise HTTPException(409, "Сначала восстановите серверную копию")
        value = await create_job(session, item, credential.id, "replicate")
        await session.commit()
        return {"ok": True, "job": job_json(value), "server_copy_preserved": True}

    @router.post("/api/mini/music/storage/{track_id}/restore")
    async def restore(track_id: int, user=Depends(require_owner), session=Depends(get_session)):
        return {"ok": True, **await ensure_restore_requested(session, settings, await track(session, track_id))}

    @router.post("/api/mini/music/storage/{track_id}/free-server-copy")
    async def evict(track_id: int, payload: Evict, x_telegram_init_data: str | None = Header(default=None),
                    user=Depends(require_owner), session=Depends(get_session)):
        if payload.confirm != "FREE SERVER COPY":
            raise HTTPException(409, "Нужно отдельное подтверждение удаления серверной копии")
        await require_action_proof(session=session, user=user, telegram_init_data=x_telegram_init_data or "",
            action_proof=payload.action_proof, purpose=f"music:evict:{track_id}:{payload.sha256}",
            binding={"source_name": payload.source_name, "sha256": payload.sha256})
        credential = await target(session, payload.source_name)
        await lock_track(session, track_id)
        item = await track(session, track_id)
        copy = await session.scalar(select(MusicStorageCopy).where(MusicStorageCopy.track_id == track_id,
            MusicStorageCopy.credential_id == credential.id, MusicStorageCopy.sha256 == payload.sha256,
            MusicStorageCopy.size == item.size))
        if not copy or item.sha256 != payload.sha256 or not await online_credential(session, credential.id):
            raise HTTPException(409, "Нужна проверенная копия на подключённом агенте")
        checked = copy.verified_at.replace(tzinfo=timezone.utc) if copy.verified_at.tzinfo is None else copy.verified_at
        if (datetime.now(timezone.utc) - checked).total_seconds() > 600:
            raise HTTPException(409, "Сначала повторно проверьте копию на агенте")
        path = managed_path(settings, item)
        if path.exists():
            if await asyncio.to_thread(checksum, path) != (item.sha256, item.size):
                raise HTTPException(409, "Серверный файл изменился: удаление отменено")
            path.unlink()  # Explicit owner-confirmed copy, exact validated managed path only.
            session.add(AdminAction(actor_user_id=user.user_id, action="music_free_server_copy",
                payload={"track_id": track_id, "source_name": payload.source_name, "sha256": payload.sha256}))
        # The native approval is single-use even when the requested copy was
        # already absent. A successful idempotent response must consume it too.
        await session.commit()
        return {"ok": True, "server_available": False, "agent_copy_preserved": True}

    @router.get("/agent/music-storage/jobs")
    async def jobs(auth=Depends(agent), session=Depends(get_session)):
        rows = list(await session.scalars(select(MusicStorageJob).where(
            MusicStorageJob.credential_id == auth.credential_id,
            MusicStorageJob.status.in_(["pending", "running"])).order_by(MusicStorageJob.created_at).limit(10)))
        result = []
        for value in rows:
            item = await session.get(MusicTrack, value.track_id)
            if not item or item.deleted:
                value.status, value.error_code = "cancelled", "track_deleted"
                continue
            if value.operation == "restore":
                part = part_path(settings, value.id)
                value.offset = part.stat().st_size if part.exists() else 0
            result.append(job_json(value))
        imports = list(await session.scalars(select(MusicImportRun).where(
            MusicImportRun.credential_id == auth.credential_id, MusicImportRun.status == "pending").limit(1)))
        await session.commit()
        return {"ok": True, "chunk_bytes": CHUNK_BYTES, "jobs": result,
                "imports": [{"id": run.id, "root": run.root, "path": run.relative_path} for run in imports]}

    @router.get("/agent/music-storage/jobs/{job_id}/chunk")
    async def read_chunk(job_id: str, offset: int = 0, auth=Depends(agent), session=Depends(get_session)):
        value, item = await job(session, job_id, auth)
        if value.operation != "replicate" or value.status not in {"pending", "running"}:
            raise HTTPException(409, "Передача не принимает чтение")
        if offset < 0 or offset >= value.size:
            raise HTTPException(416, "Смещение вне файла")
        path = managed_path(settings, item)
        if not path.is_file():
            raise HTTPException(409, "Серверная копия недоступна")
        with path.open("rb") as stream:
            stream.seek(offset); data = stream.read(min(CHUNK_BYTES, value.size - offset))
        value.status = "running"
        await session.commit()
        return Response(data, media_type="application/octet-stream", headers={"Cache-Control": "private, no-store"})

    @router.put("/agent/music-storage/jobs/{job_id}/chunk")
    async def upload_chunk(job_id: str, request: Request, offset: int = 0, auth=Depends(agent), session=Depends(get_session)):
        data = await bounded_body(request)
        value, item = await job(session, job_id, auth)
        if value.operation != "restore" or value.status not in {"pending", "running"}:
            raise HTTPException(409, "Передача не принимает запись")
        path = part_path(settings, value.id)
        if shutil.disk_usage(path.parent).free - len(data) < settings.music_min_free_bytes:
            raise HTTPException(507, "Недостаточно места на сервере")
        value.offset = await asyncio.to_thread(write_chunk, path, offset, data, value.size)
        value.status, value.updated_at = "running", utcnow()
        await session.commit()
        return {"ok": True, "offset": value.offset}

    @router.post("/agent/music-storage/jobs/{job_id}/finish")
    async def finish(job_id: str, payload: Ack, auth=Depends(agent), session=Depends(get_session)):
        value, item = await job(session, job_id, auth)
        if (payload.sha256, payload.size) != (value.sha256, value.size):
            raise HTTPException(409, "Контрольная сумма или размер не совпали")
        if value.status == "complete":
            return {"ok": True, "job": job_json(value)}
        if value.status not in {"pending", "running"}:
            raise HTTPException(409, "Передача закрыта")
        if value.operation == "restore":
            part, destination = part_path(settings, value.id), managed_path(settings, item)
            # A crash after atomic rename but before SQL commit is safely recoverable.
            if destination.is_file() and await asyncio.to_thread(checksum, destination) == (value.sha256, value.size):
                pass
            elif not part.is_file() or await asyncio.to_thread(checksum, part) != (value.sha256, value.size):
                raise HTTPException(409, "Восстановленная копия не прошла проверку SHA256/размера")
            else:
                part.replace(destination)
        else:
            copy = await session.scalar(select(MusicStorageCopy).where(
                MusicStorageCopy.track_id == item.id, MusicStorageCopy.credential_id == auth.credential_id))
            if not copy:
                copy = MusicStorageCopy(track_id=item.id, credential_id=auth.credential_id, sha256=value.sha256, size=value.size)
                session.add(copy)
            copy.sha256, copy.size, copy.verified_at = value.sha256, value.size, utcnow()
        value.offset, value.status, value.updated_at = value.size, "complete", utcnow()
        await session.commit()
        return {"ok": True, "job": job_json(value)}

    @router.post("/agent/music-storage/jobs/{job_id}/unavailable")
    async def unavailable(job_id: str, auth=Depends(agent), session=Depends(get_session)):
        value, _ = await job(session, job_id, auth)
        if value.status != "complete":
            value.status, value.error_code = "failed", "agent_copy_missing_or_corrupt"
            if value.operation == "restore":
                copy = await session.scalar(select(MusicStorageCopy).where(
                    MusicStorageCopy.track_id == value.track_id, MusicStorageCopy.credential_id == auth.credential_id))
                if copy:
                    await session.delete(copy)  # Invalidate metadata only, never an agent/user file.
            await session.commit()
        return {"ok": True}

    @router.post("/api/mini/music/storage/import-directory")
    async def import_request(payload: ImportRequest, user=Depends(require_owner), session=Depends(get_session)):
        if payload.confirm != "IMPORT DIRECTORY":
            raise HTTPException(409, "Подтвердите чтение выбранной папки; оригиналы сохраняются")
        # Only the existing file-manager root namespace, never an absolute disk path.
        parts = payload.path.replace("\\", "/").split("/")
        if payload.path.startswith(("/", "\\")) or ":" in payload.path or ".." in parts or "\x00" in payload.path:
            raise HTTPException(400, "Выберите папку внутри разрешённого корня")
        credential = await target(session, payload.source_name)
        pending = await session.scalar(select(MusicImportRun).where(
            MusicImportRun.credential_id == credential.id, MusicImportRun.status == "pending"))
        if pending:
            if pending.root != payload.root or pending.relative_path != payload.path:
                raise HTTPException(409, "На этом агенте уже выполняется импорт другой папки")
            run = pending
        else:
            run = MusicImportRun(id=secrets.token_hex(16), credential_id=credential.id,
                                 root=payload.root, relative_path=payload.path)
            session.add(run)
        await session.commit()
        return {"ok": True, "import_id": run.id, "status": run.status, "originals_preserved": True}

    @router.get("/api/mini/music/storage/imports/{run_id}")
    async def import_status(run_id: str, user=Depends(require_owner), session=Depends(get_session)):
        run = await session.get(MusicImportRun, run_id)
        if not run:
            raise HTTPException(404, "Импорт не найден")
        return {"ok": True, "id": run.id, "status": run.status, "result": run.result,
                "online": await online_credential(session, run.credential_id)}

    @router.delete("/api/mini/music/storage/imports/{run_id}")
    async def cancel_import(run_id: str, user=Depends(require_owner), session=Depends(get_session)):
        run = await session.get(MusicImportRun, run_id)
        if not run:
            raise HTTPException(404, "Импорт не найден")
        credential = await session.get(AgentCredential, run.credential_id)
        if credential:
            await ensure_agent_attached(session, credential.source_name)
        if run.status == "pending":
            run.status = "cancelled"; await session.commit()
        return {"ok": True, "status": run.status, "originals_preserved": True}

    async def import_run(session, run_id, auth):
        run = await session.get(MusicImportRun, run_id)
        if not run or run.credential_id != auth.credential_id:
            raise HTTPException(404, "Импорт не найден")
        return run

    async def import_file(session, run_id, file_id, auth):
        run = await import_run(session, run_id, auth)
        value = await session.get(MusicImportFile, file_id)
        if not value or value.run_id != run.id or value.credential_id != auth.credential_id:
            raise HTTPException(404, "Файл импорта не найден")
        return run, value

    @router.post("/agent/music-storage/imports/{run_id}/files")
    async def register_import(run_id: str, payload: ImportItem, auth=Depends(agent), session=Depends(get_session)):
        run = await import_run(session, run_id, auth)
        if run.status != "pending":
            raise HTTPException(409, "Импорт завершён")
        try:
            clean = filename(payload.filename)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        value = await session.scalar(select(MusicImportFile).where(
            MusicImportFile.credential_id == auth.credential_id, MusicImportFile.path_key == payload.path_key,
            MusicImportFile.sha256 == payload.sha256))
        if value and value.size != payload.size:
            raise HTTPException(409, "Размер не соответствует зарегистрированному хешу")
        if not value:
            value = MusicImportFile(id=secrets.token_hex(16), run_id=run.id, credential_id=auth.credential_id,
                path_key=payload.path_key, sha256=payload.sha256, size=payload.size,
                filename=clean, ordinal=payload.ordinal)
            session.add(value)
        value.run_id, value.ordinal = run.id, payload.ordinal
        await lock_content(session, payload.sha256)
        existing = await session.scalar(select(MusicTrack).where(
            MusicTrack.sha256 == payload.sha256, MusicTrack.size == payload.size, MusicTrack.deleted.is_(False)))
        available = bool(existing and managed_path(settings, existing).is_file())
        value.track_id = existing.id if available else None
        part = part_path(settings, value.id)
        recovered = managed_path(settings, SimpleNamespace(storage_name=value.id + Path(value.filename).suffix.lower()))
        recoverable = not available and recovered.is_file() and await asyncio.to_thread(checksum, recovered) == (payload.sha256, payload.size)
        value.offset = payload.size if available or recoverable else (part.stat().st_size if part.exists() else 0)
        await session.commit()
        return {"ok": True, "file_id": value.id, "offset": value.offset, "track_id": value.track_id,
                "duplicate": available}

    @router.put("/agent/music-storage/imports/{run_id}/files/{file_id}/chunk")
    async def import_chunk(run_id: str, file_id: str, request: Request, offset: int = 0,
                           auth=Depends(agent), session=Depends(get_session)):
        data = await bounded_body(request)
        run, value = await import_file(session, run_id, file_id, auth)
        if run.status != "pending":
            raise HTTPException(409, "Импорт завершён")
        if value.track_id:
            return {"ok": True, "offset": value.size}
        part = part_path(settings, value.id)
        if shutil.disk_usage(part.parent).free - len(data) < settings.music_min_free_bytes:
            raise HTTPException(507, "Недостаточно места на сервере")
        value.offset = await asyncio.to_thread(write_chunk, part, offset, data, value.size)
        await session.commit()
        return {"ok": True, "offset": value.offset}

    @router.post("/agent/music-storage/imports/{run_id}/files/{file_id}/finish")
    async def import_file_finish(run_id: str, file_id: str, auth=Depends(agent), session=Depends(get_session)):
        run, value = await import_file(session, run_id, file_id, auth)
        if value.track_id:
            return {"ok": True, "track_id": value.track_id}
        if run.status != "pending":
            raise HTTPException(409, "Импорт завершён")
        await lock_content(session, value.sha256)
        part = part_path(settings, value.id)
        # Deterministic opaque destination recovers rename-before-commit crashes.
        storage_name = value.id + Path(value.filename).suffix.lower()
        destination = managed_path(settings, SimpleNamespace(storage_name=storage_name))
        source = part if part.is_file() else destination
        if not source.is_file() or await asyncio.to_thread(checksum, source) != (value.sha256, value.size):
            raise HTTPException(409, "Файл не прошёл проверку SHA256/размера")
        try:
            metadata = await asyncio.to_thread(inspect_audio, source, value.filename)
        except Exception as exc:
            raise HTTPException(422, "Файл не является поддерживаемым аудио") from exc
        existing = await session.scalar(select(MusicTrack).where(
            MusicTrack.sha256 == value.sha256, MusicTrack.size == value.size, MusicTrack.deleted.is_(False)))
        if existing:
            await lock_track(session, existing.id)
            canonical = managed_path(settings, existing)
            moved = not canonical.is_file()
            if moved:
                source.replace(canonical)
            value.track_id = existing.id
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                if moved and canonical.is_file() and not source.exists():
                    canonical.replace(source)
                raise
            if part.is_file():
                part.unlink()
        else:
            item = MusicTrack(**metadata, filename=value.filename, storage_name=storage_name)
            session.add(item)
            await session.flush()
            if source == part:
                if destination.exists() and await asyncio.to_thread(checksum, destination) != (value.sha256, value.size):
                    raise HTTPException(409, "Путь назначения уже занят другим файлом")
                part.replace(destination)
            value.track_id, value.offset = item.id, value.size
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                if destination.is_file() and not part.exists():
                    destination.replace(part)
                raise
        return {"ok": True, "track_id": value.track_id}

    @router.post("/agent/music-storage/imports/{run_id}/finish")
    async def finish_import(run_id: str, payload: ImportFinish, auth=Depends(agent), session=Depends(get_session)):
        run = await import_run(session, run_id, auth)
        if run.status != "pending":
            return {"ok": True, "status": run.status, "result": run.result}
        # Membership list is explicit and ordered; no implicit deletion or stale-partial replacement.
        ids = list(dict.fromkeys(payload.file_ids))
        files = list(await session.scalars(select(MusicImportFile).where(
            MusicImportFile.run_id == run.id, MusicImportFile.credential_id == auth.credential_id)))
        by_id = {value.id: value for value in files}
        if any(key not in by_id or not by_id[key].track_id for key in ids):
            raise HTTPException(409, "Не все файлы импорта подтверждены")
        track_ids = list(dict.fromkeys(by_id[key].track_id for key in ids))
        previous = await session.scalar(select(MusicImportRun).where(
            MusicImportRun.credential_id == auth.credential_id, MusicImportRun.root == run.root,
            MusicImportRun.relative_path == run.relative_path, MusicImportRun.status == "complete")
            .order_by(MusicImportRun.created_at.desc()).limit(1))
        playlist_id = (previous.result or {}).get("playlist_id") if previous else None
        playlist = await session.get(MusicPlaylist, playlist_id) if playlist_id else None
        if not payload.errors:
            if not playlist:
                playlist = MusicPlaylist(name=("Импорт: " + (run.relative_path or run.root))[:160], track_ids=track_ids)
                session.add(playlist); await session.flush()
            else:
                playlist.track_ids = track_ids
        run.status = "partial" if payload.errors else "complete"
        run.result = {"tracks": len(track_ids), "skipped": payload.skipped, "errors": payload.errors,
                      "playlist_id": playlist.id if playlist else None, "originals_preserved": True}
        await session.commit()
        return {"ok": True, "status": run.status, "result": run.result}

    return router
