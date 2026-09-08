"""One active music session with acknowledged handoff, never optimistic dual play."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import math
import secrets
from typing import Literal
from weakref import WeakValueDictionary

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.db import get_session
from app.models import AgentCommand, AgentCredential, HeartbeatSource
from app.music_models import MusicSession, MusicTrack
from app.music_playback_models import MusicPlaybackState, MusicTransfer, MusicRemoteCommand
from app.services.control_status import source_is_online
from app.services.agent_lifecycle import ensure_agent_attached


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def number(value, default=0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


async def playback_meta(session):
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    await session.execute(insert(MusicPlaybackState).values(id=1).on_conflict_do_nothing(index_elements=["id"]))
    return await session.scalar(select(MusicPlaybackState).where(MusicPlaybackState.id == 1).with_for_update())


async def current_session(session):
    item = await session.get(MusicSession, 1)
    if item is None:
        return {}
    meta = await session.get(MusicPlaybackState, 1)
    result = {key: getattr(item, key) for key in ("track_id", "device", "state", "position", "share_site", "share_discord", "session_key")}
    result.update(client_id=meta.client_id if meta else "", output_id=meta.output_id if meta else "default",
                  volume=meta.volume if meta else 70, queue=meta.queue if meta else [],
                  repeat_mode=meta.repeat_mode if meta else "off", revision=meta.revision if meta else 0)
    timestamp = aware(item.updated_at)
    now = datetime.now(timezone.utc)
    queue_command = await session.get(AgentCommand, meta.queue_command_id) if meta and meta.queue_command_id else None
    queue_pending = bool(queue_command and (queue_command.status in {"awaiting_media", "pending", "delivered", "failed"} or
        (queue_command.status == "completed" and not (queue_command.payload or {}).get("queue_seen_playing") and item.state == "loading")))
    if queue_command and queue_command.status in {"awaiting_media", "failed"}:
        result["detail"] = (queue_command.result or {}).get("message") or "Ожидаем подтверждение ПК"
    if item.device.startswith("agent:"):
        source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == item.device[6:]))
        if source and source_is_online(source, 2):
            player = (source.last_payload or {}).get("music_player") or {}
            if player.get("track_id") == item.track_id and not queue_pending:
                result.update(state=player.get("state", "paused"), position=number(player.get("position_sec")),
                    volume=max(0, min(100, number(player.get("volume"), result["volume"]))),
                    output_id=player.get("output_id") or result["output_id"])
                timestamp = aware(source.last_seen_at)
        else:
            result.update(state="unavailable", detail="Компьютер не в сети")
    if result["state"] == "playing":
        # Bounded projection smooths 5s reports, without inventing hours of progress
        # when a phone loses its connection to the server.
        result["position"] += max(0, min(15, (now-timestamp).total_seconds()))
    track = await session.get(MusicTrack, item.track_id) if item.track_id else None
    if track:
        result["position"] = min(track.duration, max(0, result["position"]))
        result["duration"] = track.duration
    result["updated_at"] = timestamp.isoformat()
    result["server_time"] = now.isoformat()
    return result


async def pending_handoff(session, session_key):
    meta = await session.get(MusicPlaybackState, 1)
    transfer = await session.get(MusicTransfer, meta.transfer_id) if meta and meta.transfer_id else None
    if transfer and transfer.status == "waiting" and transfer.source_device == "local" and transfer.source_key == session_key:
        return transfer
    return None


class TransferBody(BaseModel):
    session_key: str = Field(min_length=16, max_length=64)
    client_id: str = Field(min_length=1, max_length=128)
    device: str = Field(max_length=134)
    output_id: str = Field(default="default", max_length=256)
    track_id: int | None = Field(default=None, gt=0)
    position: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)
    volume: int = Field(default=70, ge=0, le=100)
    autoplay: bool = True
    queue: list[int] | None = Field(default=None, max_length=2000)
    repeat_mode: Literal["off", "one", "all"] = "off"


class TransferAck(BaseModel):
    session_key: str = Field(min_length=16, max_length=64)
    position: float = Field(ge=0, le=86400, allow_inf_nan=False)


def install_transfer_routes(router, settings, require_owner, control, control_body):
    locks = WeakValueDictionary()

    def lock():
        return locks.setdefault("handoff", asyncio.Lock())

    async def fail(session, transfer, detail):
        transfer.status = "failed"; transfer.detail = detail
        for command_id in (transfer.stop_command_id, transfer.start_command_id):
            if command_id:
                await session.execute(update(AgentCommand).where(AgentCommand.id == command_id,
                    AgentCommand.status == "pending").values(status="cancelled"))
        meta = await playback_meta(session)
        if meta.transfer_id == transfer.id:
            meta.transfer_id = ""
        item = await session.get(MusicSession, 1)
        if item and item.session_key == transfer.target.get("session_key") and transfer.status == "failed":
            item.state = "error"; item.updated_at = datetime.now(timezone.utc)
        await session.commit()

    async def advance(transfer, request, user, session):
        if transfer.status in {"ready", "failed"}:
            return
        if (datetime.now(timezone.utc)-aware(transfer.created_at)).total_seconds() > 30:
            await fail(session, transfer, "Предыдущее устройство не подтвердило переключение. Музыка не запущена повторно; попробуйте ещё раз")
            return
        meta = await playback_meta(session)
        if meta.transfer_id != transfer.id:
            await fail(session, transfer, "Переключение заменено новым действием")
            return
        target = transfer.target
        if transfer.status == "waiting" and transfer.stop_command_id:
            command = await session.get(AgentCommand, transfer.stop_command_id)
            if command and command.status == "completed" and (command.result or {}).get("ok") is not False:
                details = (command.result or {}).get("details") or {}
                if details.get("state") not in {"paused", "stopped", "ended", "idle"}:
                    await fail(session, transfer, "ПК не подтвердил остановку звука")
                    return
                if target.get("preserve_position"):
                    transfer.position = number(details.get("position_sec"), transfer.position)
                transfer.status = "stopped"
            elif command and command.status in {"failed", "cancelled"}:
                await fail(session, transfer, (command.result or {}).get("message") or "Не удалось остановить прежний ПК")
                return
        if transfer.status == "stopped":
            item = await session.get(MusicSession, 1)
            if item is None:
                item = MusicSession(id=1); session.add(item)
            item.track_id = target["track_id"]; item.device = target["device"]
            item.session_key = target["session_key"]; item.position = transfer.position
            item.state = "loading" if target["autoplay"] else "paused"
            item.updated_at = datetime.now(timezone.utc)
            meta.client_id = target["client_id"]; meta.output_id = target["output_id"]; meta.volume = target["volume"]
            if target.get("queue") is not None:
                meta.queue = list(dict.fromkeys(target["queue"]))
            meta.repeat_mode = target["repeat_mode"]; meta.revision += 1
            if target["device"] == "local" or not target["autoplay"]:
                transfer.status = "ready"; meta.transfer_id = ""
                await session.commit()
                return
            # Persist the new lease before creating the target command. Playback
            # is started only after the old source supplied a real paused ACK.
            transfer.status = "starting"
            await session.commit()
            try:
                sent = await control(control_body(source_name=target["device"][6:], action="play",
                    track_id=target["track_id"], output_id=target["output_id"], position_sec=transfer.position,
                    volume=target["volume"], expires_at=int(aware(transfer.created_at).timestamp()) + 30), request, user, session)
                transfer.start_command_id = sent["command_id"]
                await session.commit()
            except HTTPException as exc:
                await fail(session, transfer, str(exc.detail))
                return
        if transfer.status == "starting":
            if not transfer.start_command_id:
                await fail(session, transfer, "Запуск был прерван. Повторите переключение")
                return
            command = await session.get(AgentCommand, transfer.start_command_id)
            if command and command.status == "completed" and (command.result or {}).get("ok") is not False:
                transfer.status = "ready"; meta.transfer_id = ""
                details = (command.result or {}).get("details") or {}
                item = await session.get(MusicSession, 1)
                item.state = details.get("state", "loading"); item.updated_at = datetime.now(timezone.utc)
                await session.commit()
            elif command and command.status in {"failed", "cancelled"}:
                await fail(session, transfer, (command.result or {}).get("message") or "ПК не запустил музыку")

    async def result(transfer, session):
        return {"ok": transfer.status != "failed", "transfer_id": transfer.id,
                "status": transfer.status if transfer.status in {"ready", "failed"} else "waiting",
                "detail": transfer.detail, "session": await current_session(session)}

    @router.post("/api/mini/music/transfers")
    async def start(payload: TransferBody, request: Request, user=Depends(require_owner), session=Depends(get_session)):
        if payload.device != "local" and not payload.device.startswith("agent:"):
            raise HTTPException(400, "Выберите телефон или компьютер")
        async with lock():
            meta = await playback_meta(session)
            if meta.transfer_id:
                previous = await session.get(MusicTransfer, meta.transfer_id)
                if previous and previous.status not in {"ready", "failed"}:
                    if (datetime.now(timezone.utc)-aware(previous.created_at)).total_seconds() <= 30:
                        raise HTTPException(409, "Дождитесь завершения текущего переключения")
                    await fail(session, previous, "Время переключения истекло")
            state = await current_session(session)
            item = await session.get(MusicSession, 1)
            if state.get("state") == "unavailable" and item and item.state in {"playing", "loading"}:
                raise HTTPException(409, "Предыдущий ПК не в сети: невозможно подтвердить остановку звука. Подключите или остановите его перед переключением")
            track_id = payload.track_id or state.get("track_id")
            track = await session.get(MusicTrack, track_id) if track_id else None
            if track is None or track.deleted:
                raise HTTPException(404, "Выберите доступный трек")
            if payload.device.startswith("agent:"):
                source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == payload.device[6:]))
                if not source or not source_is_online(source, 2) or not isinstance((source.last_payload or {}).get("music_player"), dict):
                    raise HTTPException(409, "ПК недоступен для музыки. Проверьте подключение и версию агента")
                credential = await session.scalar(select(AgentCredential).where(
                    AgentCredential.source_name == source.source_name, AgentCredential.is_active.is_(True)))
                if credential is None:
                    raise HTTPException(409, "Перепривяжите ПК с индивидуальным ключом")
                await ensure_agent_attached(session, source.source_name)
                if track.mime == "audio/mp4":
                    raise HTTPException(415, "Для этого ПК нужен файл MP3, WAV, FLAC или OGG Vorbis")
            if item and item.device.startswith("agent:"):
                from app.services.music_agent_queue import cancel_agent_queue
                await cancel_agent_queue(session, item.device[6:])
                state = await current_session(session)
            target = payload.model_dump()
            target["track_id"] = track_id
            target["preserve_position"] = payload.position is None and state.get("track_id") == track_id
            position = payload.position if payload.position is not None else state.get("position", 0) if target["preserve_position"] else 0
            transfer = MusicTransfer(id=secrets.token_hex(16), source_device=item.device if item else "local",
                source_key=item.session_key if item else "", target=target, position=min(track.duration, position))
            if item and item.session_key:
                await session.execute(update(MusicRemoteCommand).where(MusicRemoteCommand.session_key == item.session_key,
                    MusicRemoteCommand.status == "pending").values(status="cancelled", error="Устройство переключено"))
            session.add(transfer); meta.transfer_id = transfer.id
            audible = state.get("state") in {"playing", "loading"}
            # A current local player may already have paused itself and reported
            # that pause. Never infer silence merely from a new controller key.
            transfer.status = "waiting" if audible else "stopped"
            await session.commit()
            if audible and transfer.source_device.startswith("agent:"):
                try:
                    sent = await control(control_body(source_name=transfer.source_device[6:], action="pause",
                        expires_at=int(aware(transfer.created_at).timestamp()) + 30), request, user, session)
                    transfer.stop_command_id = sent["command_id"]
                    await session.commit()
                except HTTPException as exc:
                    await fail(session, transfer, str(exc.detail))
            await advance(transfer, request, user, session)
            return await result(transfer, session)

    @router.get("/api/mini/music/transfers/{transfer_id}")
    async def status(transfer_id: str, request: Request, user=Depends(require_owner), session=Depends(get_session)):
        async with lock():
            transfer = await session.get(MusicTransfer, transfer_id)
            if transfer is None:
                raise HTTPException(404, "Переключение не найдено")
            await advance(transfer, request, user, session)
            return await result(transfer, session)

    @router.post("/api/mini/music/transfers/{transfer_id}/ack")
    async def acknowledge(transfer_id: str, payload: TransferAck, user=Depends(require_owner), session=Depends(get_session)):
        async with lock():
            transfer = await session.get(MusicTransfer, transfer_id)
            if transfer is None or transfer.source_device != "local" or not secrets.compare_digest(transfer.source_key, payload.session_key):
                raise HTTPException(403, "Подтверждение другого плеера")
            if transfer.status == "waiting":
                if transfer.target.get("preserve_position"):
                    transfer.position = payload.position
                transfer.status = "stopped"
                item = await session.get(MusicSession, 1)
                if item and item.session_key == payload.session_key:
                    item.position = payload.position; item.state = "paused"; item.updated_at = datetime.now(timezone.utc)
                await session.commit()
            return {"ok": True}
