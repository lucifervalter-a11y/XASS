"""Safe file operations and on-demand restoration; no provider scraping."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import secrets

import httpx

from fastapi import HTTPException
from sqlalchemy import select, update

from app.models import AgentCredential, HeartbeatSource, utcnow
from app.music_storage_models import MusicStorageCopy, MusicStorageJob, MusicContentLock
from app.services.control_status import source_is_online
from app.services.music_library import track_path

CHUNK_BYTES = 512 * 1024
MAX_BYTES = 256 * 1024 * 1024


def checksum(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(data); size += len(data)
    return digest.hexdigest(), size


def managed_path(settings, track) -> Path:
    root = Path(settings.music_root)
    candidate = root / track.storage_name
    if root.is_symlink() or candidate.is_symlink():
        raise HTTPException(409, "Небезопасный путь музыкального файла")
    return track_path(root, track.storage_name)


def part_path(settings, transfer_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", transfer_id):
        raise HTTPException(404, "Передача не найдена")
    configured = Path(settings.music_root)
    if configured.is_symlink():
        raise HTTPException(409, "Небезопасный корень передачи")
    root = configured.resolve()
    folder = root / ".transfers"
    if folder.is_symlink():
        raise HTTPException(409, "Небезопасный каталог передачи")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (transfer_id + ".part")
    if path.is_symlink():
        raise HTTPException(409, "Небезопасный путь передачи")
    return path


def write_chunk(path: Path, offset: int, data: bytes, total: int) -> int:
    """The durable file length is authoritative after a server/process crash."""
    if not data or len(data) > CHUNK_BYTES or offset < 0 or offset + len(data) > total:
        raise HTTPException(400, "Некорректный блок файла")
    current = path.stat().st_size if path.exists() else 0
    if offset < current:
        with path.open("rb") as stream:
            stream.seek(offset)
            if stream.read(len(data)) == data:
                return current
        raise HTTPException(409, "Повторный блок отличается")
    if offset != current:
        raise HTTPException(409, "Смещение файла изменилось")
    with path.open("ab") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    return current + len(data)


async def lock_track(session, track_id: int):
    from app.music_models import MusicTrack
    await session.execute(update(MusicTrack).where(MusicTrack.id == track_id)
                          .values(size=MusicTrack.size).execution_options(synchronize_session=False))


async def lock_content(session, digest: str):
    """Serialize same-content imports across credentials and server workers."""
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid SHA256")
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    await session.execute(insert(MusicContentLock).values(sha256=digest).on_conflict_do_nothing(index_elements=["sha256"]))
    await session.execute(update(MusicContentLock).where(MusicContentLock.sha256 == digest)
                          .values(sha256=MusicContentLock.sha256))


async def online_credential(session, credential_id: int) -> bool:
    credential = await session.get(AgentCredential, credential_id)
    if not credential or not credential.is_active:
        return False
    source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == credential.source_name))
    return bool(source and source_is_online(source, 2))


async def create_job(session, track, credential_id: int, operation: str):
    await lock_track(session, track.id)
    job = await session.scalar(select(MusicStorageJob).where(
        MusicStorageJob.track_id == track.id, MusicStorageJob.credential_id == credential_id,
        MusicStorageJob.operation == operation, MusicStorageJob.status.in_(["pending", "running"])))
    if job is None:
        job = MusicStorageJob(id=secrets.token_hex(16), track_id=track.id, credential_id=credential_id,
                              operation=operation, sha256=track.sha256, size=track.size)
        session.add(job)
        await session.flush()
    return job


async def ensure_restore_requested(session, settings, track) -> dict:
    """Hook for a missing local track. Returns explicit state, never claims lost metadata."""
    if managed_path(settings, track).is_file():
        return {"status": "available", "server_available": True}
    copies = list(await session.scalars(select(MusicStorageCopy).where(
        MusicStorageCopy.track_id == track.id, MusicStorageCopy.sha256 == track.sha256,
        MusicStorageCopy.size == track.size)))
    for copy in copies:
        if await online_credential(session, copy.credential_id):
            job = await create_job(session, track, copy.credential_id, "restore")
            await session.commit()
            return {"status": "restore_pending", "job_id": job.id, "server_available": False}
    return {"status": "agent_offline" if copies else "file_unavailable", "server_available": False,
            "copies": len(copies), "retryable": bool(copies)}


def job_json(job):
    return {key: getattr(job, key) for key in
            ("id", "track_id", "operation", "sha256", "size", "offset", "status", "error_code")}


def vk_capabilities(configured: bool) -> dict:
    return {"provider": "vk", "configured": configured, "identity_verified": None,
        "token_status": "not_checked" if configured else "not_configured",
        "capabilities": {"now_playing": None, "library_metadata": False, "playlist_metadata": False,
                         "stream": False, "file_export": False},
        "reason_code": "music_access_not_granted",
        "message": "Подключение статуса VK не даёт доступа к импорту всей музыкальной библиотеки. Нужен разрешённый VK API или ваш экспорт и собственные файлы."}


async def check_vk_capabilities(token: str, api_version="5.199", *, client=None) -> dict:
    result = vk_capabilities(bool(token))
    if not token:
        return result
    owned = client is None
    client = client or httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False)
    try:
        response = await client.post("https://api.vk.com/method/users.get", data={"access_token": token, "v": api_version})
        response.raise_for_status(); body = response.json()
        users = body.get("response") if isinstance(body, dict) else None
        if (not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict)
                or type(users[0].get("id")) is not int or users[0]["id"] <= 0):
            result.update(identity_verified=False, token_status="invalid_or_denied")
            return result
        result.update(identity_verified=True, token_status="valid")
        response = await client.post("https://api.vk.com/method/status.get", data={
            "access_token": token, "v": api_version, "user_id": users[0]["id"]})
        response.raise_for_status(); body = response.json()
        result["capabilities"]["now_playing"] = isinstance(body, dict) and isinstance(body.get("response"), dict)
        # No unverified/private audio methods, first-party app IDs, cookies or download promises.
        return result
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        result["token_status"] = "service_unavailable"
        return result
    finally:
        if owned:
            await client.aclose()
