"""One active music session with acknowledged handoff, never optimistic dual play."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import ipaddress
import math
import re
import secrets
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit
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
from app.services.music_diagnostics import classify_transfer_failure, transfer_failure_code


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


async def fail_transfer(session, transfer, detail):
    """Release a failed handoff without claiming an audible target is silent."""
    previous_status = transfer.status
    transfer.status = "failed"
    transfer.detail = detail
    transfer.target = {**(transfer.target or {}), "failure_code": classify_transfer_failure(detail, phase=previous_status)}
    start_command = await session.get(AgentCommand, transfer.start_command_id) if transfer.start_command_id else None
    target_may_play = bool(start_command and (
        start_command.delivered_at is not None or start_command.status in {"delivered", "completed"}))
    for command_id in (transfer.stop_command_id, transfer.start_command_id):
        if command_id:
            await session.execute(update(AgentCommand).where(AgentCommand.id == command_id,
                AgentCommand.status == "pending").values(status="cancelled"))
    meta = await playback_meta(session)
    target = transfer.target or {}
    owns_lease = meta.transfer_id == transfer.id or (
        previous_status == "ready" and not meta.transfer_id and meta.revision == target.get("lease_revision"))
    if meta.transfer_id == transfer.id:
        meta.transfer_id = ""
    item = await session.get(MusicSession, 1)
    # A controller commonly reuses its session key when moving from iPhone to
    # PC. Failing before the source ACK must not overwrite that source's state.
    if (owns_lease and previous_status in {"starting", "stopped", "ready"} and item
            and item.session_key == target.get("session_key") and item.device == target.get("device")
            and item.track_id == target.get("track_id")):
        item.state = "loading" if target_may_play else "error"
        item.updated_at = datetime.now(timezone.utc)
    if owns_lease:
        meta.revision += 1
    await session.commit()


async def expire_active_transfer(session):
    """Recover abandoned handoffs even when their initiating client disappears."""
    meta = await session.get(MusicPlaybackState, 1)
    transfer = await session.get(MusicTransfer, meta.transfer_id) if meta and meta.transfer_id else None
    if (transfer and transfer.status not in {"ready", "failed"}
            and (datetime.now(timezone.utc) - aware(transfer.created_at)).total_seconds() > 30):
        await fail_transfer(session, transfer, "Время переключения истекло. Повторите действие")


QUIET_STATES = frozenset({"paused", "stopped", "ended", "idle", "error"})
# A heartbeat must postdate the lease by this much before it can prove that a
# just-started PC command is not still downloading/starting.
PC_SILENCE_GRACE_SECONDS = 15


def pc_heartbeat_proves_silence(item, source, *, now=None) -> bool:
    """A live PC heartbeat, newer than the lease, that reports a quiet player."""
    if not item or not item.device.startswith("agent:") or source is None or source.last_seen_at is None:
        return False
    player = (source.last_payload or {}).get("music_player")
    if not isinstance(player, dict) or player.get("state") not in QUIET_STATES:
        return False
    return (aware(source.last_seen_at) - aware(item.updated_at)).total_seconds() >= PC_SILENCE_GRACE_SECONDS


async def pc_source_released(session, item, *, now=None) -> bool:
    """True when a PC lease cannot be audible: offline PC or a quiet fresh heartbeat.

    An offline PC cannot acknowledge anything, so waiting for it only leaves
    the phone unable to play (HTTP 409 forever). The owner explicitly asked to
    play here; the PC receives a stop command when it is reachable.
    """
    if not item or not item.device.startswith("agent:"):
        return False
    source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == item.device[6:]))
    if source is None or not source_is_online(source, 2, now=now):
        return True
    return pc_heartbeat_proves_silence(item, source, now=now)


def stale_local_recovery_available(item, meta, transfer, *, now=None):
    """Staleness permits an explicit owner-confirmed pause, never automatic play."""
    active_handoff = bool(meta and meta.transfer_id and (
        transfer is None or transfer.status not in {"ready", "failed"}))
    return bool(item and item.session_key and item.device == "local"
        and item.state in {"playing", "loading"} and not active_handoff
        and ((now or datetime.now(timezone.utc)) - aware(item.updated_at)).total_seconds() >= 300)


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
            # A paused heartbeat from before a new play command cannot prove
            # silence if the command was delivered and its ACK was lost.
            fresh_for_start = item.state != "loading" or aware(source.last_seen_at) >= timestamp
            if player.get("track_id") == item.track_id and not queue_pending and fresh_for_start:
                result.update(state=player.get("state", "paused"), position=number(player.get("position_sec")),
                    volume=max(0, min(100, number(player.get("volume"), result["volume"]))),
                    output_id=player.get("output_id") or result["output_id"])
                timestamp = aware(source.last_seen_at)
            elif (item.state in {"playing", "loading"} and not queue_pending
                    and pc_heartbeat_proves_silence(item, source, now=now)):
                # The lease still says "loading" but the PC has reported a
                # quiet, different player well after it. Without this the
                # phone kept receiving transfer_required and waited for a PC
                # that had nothing to pause.
                result.update(state=str(player.get("state") or "stopped") if player.get("state") in QUIET_STATES else "stopped")
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
    transfer = await session.get(MusicTransfer, meta.transfer_id) if meta and meta.transfer_id else None
    result["recovery_available"] = stale_local_recovery_available(item, meta, transfer, now=now)
    if meta and meta.transfer_id:
        if transfer and transfer.status not in {"ready", "failed"}:
            result["active_transfer_id"] = transfer.id
            result["active_transfer_status"] = transfer.status
            result["active_transfer_source_key"] = transfer.source_key
            result["active_transfer_source_device"] = transfer.source_device
    return result


_LAN_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,64}\Z")
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _private_ipv4(host: str) -> ipaddress.IPv4Address | None:
    try:
        value = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not isinstance(value, ipaddress.IPv4Address):
        return None
    if not any(value in network for network in _RFC1918):
        return None
    return value


def lan_media_url(host: Any, port: Any, token: Any, track_id: Any) -> str | None:
    """Build one canonical private URL, or drop the offer. Never raises."""
    if isinstance(port, bool) or isinstance(track_id, bool):
        return None
    if not isinstance(host, str) or not isinstance(token, str) or not _LAN_TOKEN.fullmatch(token):
        return None
    address = _private_ipv4(host)
    if address is None or host != str(address):
        return None
    try:
        number = int(port)
        track = int(track_id)
    except (TypeError, ValueError):
        return None
    if not 1024 <= number <= 65535 or not 0 < track < 2**63:
        return None
    return f"http://{address}:{number}/xass-lan/{track}?token={token}"


def canonical_lan_url(value: Any, track_id: Any) -> str | None:
    """Re-check a stored offer. A public, loopback or rewritten URL is omitted."""
    if isinstance(track_id, bool) or not isinstance(value, str) or len(value) > 300:
        return None
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        return None
    try:
        track = int(track_id)
        parts = urlsplit(value)
        if (parts.scheme != "http" or parts.username is not None or parts.password is not None
                or parts.fragment or not parts.hostname or parts.port is None
                or parts.path != f"/xass-lan/{track}"):
            return None
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError, OverflowError):
        return None
    token = query.get("token", [])
    if set(query) != {"token"} or len(token) != 1:
        return None
    return lan_media_url(parts.hostname, parts.port, token[0], track)


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
    # Optional same-LAN file offer. Invalid values are dropped, not a 422:
    # the paired server stream still has to start playback.
    lan_host: str | None = Field(default=None, max_length=256)
    lan_port: int | None = None
    lan_token: str | None = Field(default=None, max_length=256)


class TransferAck(BaseModel):
    session_key: str = Field(min_length=16, max_length=64)
    position: float = Field(ge=0, le=86400, allow_inf_nan=False)


def install_transfer_routes(router, settings, require_owner, control, control_body):
    locks = WeakValueDictionary()

    def lock():
        return locks.setdefault("handoff", asyncio.Lock())

    fail = fail_transfer

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
                details = (command.result or {}).get("details") or {}
                # Older agents raised when asked to pause an idle player. A
                # snapshot that is already quiet is the stop ACK. Playing or
                # loading is not, and a bare cancellation has no snapshot.
                if command.status == "failed" and details.get("state") in {"paused", "stopped", "ended", "idle", "error"}:
                    if target.get("preserve_position"):
                        transfer.position = number(details.get("position_sec"), transfer.position)
                    transfer.status = "stopped"
                else:
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
            transfer.target = {**target, "lease_revision": meta.revision}
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
                    volume=target["volume"], expires_at=int(aware(transfer.created_at).timestamp()) + 30,
                    lan_url=target.get("lan_url") or None), request, user, session)
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
                details = (command.result or {}).get("details") or {}
                if details.get("state") not in {"loading", "playing", "ended"}:
                    await fail(session, transfer, "ПК не подтвердил запуск музыки")
                    return
                transfer.status = "ready"; meta.transfer_id = ""
                item = await session.get(MusicSession, 1)
                item.state = details.get("state", "loading"); item.updated_at = datetime.now(timezone.utc)
                await session.commit()
            elif command and command.status in {"failed", "cancelled"}:
                await fail(session, transfer, (command.result or {}).get("message") or "ПК не запустил музыку")

    async def result(transfer, session):
        # This is a valid resource response, including failed/cancelled handoffs.
        # Playback success is ONLY status == "ready"; clients must retain the
        # transfer ID and failure detail instead of treating them as malformed JSON.
        return {"ok": True, "transfer_id": transfer.id,
                "status": transfer.status if transfer.status in {"ready", "failed"} else "waiting",
                "detail": str(transfer.detail or "")[:500], "session": await current_session(session),
                **({"error_code": transfer_failure_code(transfer)} if transfer.status == "failed" else {})}

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
            # An offline PC reports state "unavailable": it is not audible from
            # the server's point of view and cannot ACK a pause. Moving playback
            # away from it must not be blocked forever (was HTTP 409).
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
            offer = lan_media_url(target.pop("lan_host", None), target.pop("lan_port", None), target.pop("lan_token", None), track_id)
            if offer:
                target["lan_url"] = offer
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

    
    @router.post("/api/mini/music/transfers/{transfer_id}/cancel")
    async def cancel_transfer(transfer_id: str, request: Request, user=Depends(require_owner), session=Depends(get_session)):
        """Owner abort: clear transfer lock and stop a half-started target if needed."""
        async with lock():
            transfer = await session.get(MusicTransfer, transfer_id)
            if transfer is None:
                raise HTTPException(404, "Переключение не найдено")
            if transfer.status == "failed":
                return await result(transfer, session)
            target = transfer.target or {}
            if transfer.status == "ready":
                meta = await playback_meta(session)
                item = await session.get(MusicSession, 1)
                if (not item or item.session_key != target.get("session_key")
                        or item.device != target.get("device") or item.track_id != target.get("track_id")
                        or meta.transfer_id or meta.revision != target.get("lease_revision")):
                    # A late cancel must never stop a later playback selection.
                    return await result(transfer, session)
            stop_target = (
                transfer.status in {"starting", "ready"}
                and isinstance(target.get("device"), str)
                and target["device"].startswith("agent:")
                and target.get("autoplay", True)
            )
            await fail(session, transfer, "Переключение отменено")
            if stop_target:
                try:
                    # Stop also invalidates a pending download; pause cannot
                    # prevent its worker from starting audio after cancellation.
                    await control(control_body(source_name=target["device"][6:], action="stop",
                        expires_at=int(datetime.now(timezone.utc).timestamp()) + 30), request, user, session)
                except HTTPException:
                    pass
            return await result(transfer, session)

    @router.post("/api/mini/music/transfers/{transfer_id}/ack")
    async def acknowledge(transfer_id: str, payload: TransferAck, user=Depends(require_owner), session=Depends(get_session)):
        async with lock():
            transfer = await session.get(MusicTransfer, transfer_id)
            if transfer is None or transfer.source_device != "local" or not secrets.compare_digest(transfer.source_key, payload.session_key):
                raise HTTPException(403, "Подтверждение другого плеера")
            if transfer.status == "waiting" and (datetime.now(timezone.utc) - aware(transfer.created_at)).total_seconds() > 30:
                await fail(session, transfer, "Время переключения истекло. Повторите действие")
                raise HTTPException(409, "Подтверждение переключения истекло")
            if transfer.status == "waiting":
                if transfer.target.get("preserve_position"):
                    track = await session.get(MusicTrack, transfer.target.get("track_id"))
                    transfer.position = min(track.duration, payload.position) if track else payload.position
                transfer.status = "stopped"
                item = await session.get(MusicSession, 1)
                if item and item.session_key == payload.session_key:
                    item.position = payload.position; item.state = "paused"; item.updated_at = datetime.now(timezone.utc)
                await session.commit()
            return {"ok": True}
