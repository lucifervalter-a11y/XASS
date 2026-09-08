"""Owner commands to the current native player, acknowledged by actual playback.

This inbox is not an iOS wake-up service. A suspended phone must reopen XASS;
commands expire rather than run unexpectedly after the user returns much later.
"""
from datetime import datetime, timedelta, timezone
import secrets
from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update, func

from app.db import get_session
from app.music_models import MusicSession, MusicTrack
from app.music_playback import aware, playback_meta
from app.music_playback_models import MusicRemoteCommand


class RemoteControl(BaseModel):
    target_key: str = Field(min_length=16, max_length=64)
    action: Literal["pause", "resume", "seek", "volume", "next", "previous"]
    position: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)
    volume: int = Field(default=70, ge=0, le=100)


class RemoteAck(BaseModel):
    session_key: str = Field(min_length=16, max_length=64)
    ok: bool
    state: Literal["playing", "paused", "stopped", "ended", "loading", "error"]
    position: float = Field(ge=0, le=86400, allow_inf_nan=False)
    error: str = Field(default="", max_length=240)


def install_remote_control_routes(router, require_owner):
    async def expire(session):
        await session.execute(update(MusicRemoteCommand).where(MusicRemoteCommand.status == "pending",
            MusicRemoteCommand.created_at < datetime.now(timezone.utc) - timedelta(seconds=30))
            .values(status="expired", error="iPhone не подтвердил команду. Откройте XASS на нём и повторите.")
            .execution_options(synchronize_session=False))

    @router.post("/api/mini/music/session/control")
    async def command(payload: RemoteControl, user=Depends(require_owner), session=Depends(get_session)):
        meta = await playback_meta(session)
        item = await session.get(MusicSession, 1)
        if not item or item.device != "local" or not secrets.compare_digest(item.session_key, payload.target_key):
            raise HTTPException(409, "Воспроизведение уже перешло на другое устройство. Обновите плеер.")
        if meta.transfer_id:
            raise HTTPException(409, "Дождитесь завершения переключения")
        await expire(session)
        count = await session.scalar(select(func.count()).select_from(MusicRemoteCommand).where(
            MusicRemoteCommand.session_key == item.session_key, MusicRemoteCommand.status == "pending"))
        if count >= 16:
            raise HTTPException(429, "iPhone ещё не выполнил отправленные команды")
        value = MusicRemoteCommand(id=secrets.token_hex(16), session_key=item.session_key,
            action=payload.action, payload={"position": payload.position, "volume": payload.volume})
        session.add(value); await session.commit()
        return {"ok": True, "command_id": value.id, "status": value.status}

    @router.get("/api/mini/music/session/commands")
    async def inbox(session_key: str = Query(min_length=16, max_length=64),
                    user=Depends(require_owner), session=Depends(get_session)):
        meta = await playback_meta(session)
        await expire(session)
        item = await session.get(MusicSession, 1)
        rows = []
        if item and item.device == "local" and not meta.transfer_id and secrets.compare_digest(item.session_key, session_key):
            rows = list(await session.scalars(select(MusicRemoteCommand).where(
                MusicRemoteCommand.session_key == session_key, MusicRemoteCommand.status == "pending")
                .order_by(MusicRemoteCommand.created_at, MusicRemoteCommand.id).limit(16)))
        await session.commit()
        return {"ok": True, "commands": [{"id": row.id, "action": row.action, **row.payload,
            "expires_at": int(aware(row.created_at).timestamp()) + 30} for row in rows]}

    @router.get("/api/mini/music/session/commands/{command_id}")
    async def status(command_id: str, user=Depends(require_owner), session=Depends(get_session)):
        await expire(session)
        value = await session.get(MusicRemoteCommand, command_id)
        if not value:
            raise HTTPException(404, "Команда не найдена")
        await session.commit()
        return {"ok": True, "command_id": value.id, "status": value.status, "error": value.error}

    @router.post("/api/mini/music/session/commands/{command_id}/ack")
    async def acknowledge(command_id: str, payload: RemoteAck, user=Depends(require_owner), session=Depends(get_session)):
        meta = await playback_meta(session)
        await expire(session)
        value = await session.get(MusicRemoteCommand, command_id)
        if not value or not secrets.compare_digest(value.session_key, payload.session_key):
            raise HTTPException(403, "Команда другого плеера")
        if value.status in {"completed", "failed"}:
            return {"ok": True, "status": value.status}
        item = await session.get(MusicSession, 1)
        if value.status != "pending" or not item or item.device != "local" or item.session_key != payload.session_key or meta.transfer_id:
            raise HTTPException(409, "Команда истекла или управление перешло на другое устройство")
        confirmed = payload.ok
        if value.action == "pause" and payload.state not in {"paused", "stopped", "ended"}:
            confirmed = False
        if value.action == "resume" and payload.state not in {"playing", "loading"}:
            confirmed = False
        if payload.state == "error":
            confirmed = False
        value.status = "completed" if confirmed else "failed"
        value.error = "" if confirmed else payload.error or "Плеер не подтвердил выполнение команды"
        # next/previous report the new track through the ordinary session reporter.
        # Never optimistically advance the server's track or position here.
        if confirmed and value.action in {"pause", "resume", "seek"}:
            track = await session.get(MusicTrack, item.track_id) if item.track_id else None
            item.state = payload.state
            item.position = min(track.duration, payload.position) if track else payload.position
            item.updated_at = datetime.now(timezone.utc)
            meta.revision += 1
        if confirmed and value.action == "volume":
            meta.volume = value.payload["volume"]; meta.revision += 1
        await session.commit()
        return {"ok": True, "status": value.status}
