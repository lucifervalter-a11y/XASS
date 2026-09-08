"""Advance an explicitly chosen PC queue from authenticated, fresh player reports.

There are no timers, local playback, arbitrary URLs or best-guess starts here.
Reserve the target session and its command in one transaction before delivery.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select

from app.models import AgentCommand, AgentCredential
from app.music_models import MusicSession, MusicTrack
from app.music_playback import aware, playback_meta
from app.services.agent_lifecycle import AgentDetachedError, ensure_agent_attached
from app.services.control_status import source_is_online
from app.services.music_library import issue_ticket
from app.services.music_storage import ensure_restore_requested, managed_path


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def _origin(value: str) -> str:
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Invalid music origin")
    parts = urlsplit(value)
    if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("Invalid music origin")
    _ = parts.port
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


async def _next_track(session, current_id: int, queue, repeat: str):
    ids = list(dict.fromkeys(item for item in (queue if isinstance(queue, list) else [])[:2000]
                             if type(item) is int and item > 0))
    if repeat == "one":
        ids = [current_id]
    elif current_id not in ids:
        return None
    else:
        index = ids.index(current_id)
        ids = ids[index + 1:] + (ids[:index + 1] if repeat == "all" else [])
    if not ids:
        return None
    available = {item.id: item for item in await session.scalars(select(MusicTrack).where(
        MusicTrack.id.in_(ids), MusicTrack.deleted.is_(False), MusicTrack.mime != "audio/mp4"))}
    return next((available[item] for item in ids if item in available), None)


async def _failed(session, meta, item, command, reason: str, message: str):
    command.status = "failed"
    command.result = {"ok": False, "message": message, "details": {"reason": reason}}
    command.completed_at = datetime.now(timezone.utc)
    item.state = "error"; item.updated_at = command.completed_at
    meta.revision += 1
    await session.commit()
    return {"status": "failed", "command_id": command.id, "reason": reason, "message": message}


async def cancel_agent_queue(session, source_name: str) -> dict:
    """Cancel only this canonical source's queue reservation, without committing.

    The owner control/transfer caller validates its target first and commits this
    with the new action. Never claim a delivered command was cancelled remotely.
    """
    meta = await playback_meta(session)
    await session.refresh(meta)
    item = await session.scalar(select(MusicSession).where(MusicSession.id == 1)
        .execution_options(populate_existing=True))
    if not item or item.device != "agent:" + source_name or not meta.queue_command_id:
        return {"cancelled": False, "restored_from_heartbeat": False}
    command = await session.get(AgentCommand, meta.queue_command_id)
    if (not command or command.source_name != source_name or (command.payload or {}).get("queue_managed") is not True
            or command.payload.get("queue_session_key") != item.session_key):
        return {"cancelled": False, "restored_from_heartbeat": False}
    cancelled = command.status in {"pending", "awaiting_media"}
    restore_actual = cancelled and command.delivered_at is None
    if cancelled:
        command.status = "cancelled"
        command.result = {"ok": False, "message": "Автопереход отменён ручным управлением",
                          "details": {"reason": "manual_control"}}
        command.completed_at = datetime.now(timezone.utc)
    from app.models import HeartbeatSource
    source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == source_name)
        .execution_options(populate_existing=True))
    player = (source.last_payload or {}).get("music_player") if source else None
    restored = False
    if source and source_is_online(source, 2) and isinstance(player, dict):
        actual_id = player.get("track_id")
        if type(actual_id) is int and (restore_actual or actual_id == item.track_id):
            actual = await session.get(MusicTrack, actual_id)
            actual_state = player.get("state")
            if actual and actual_state in {"playing", "paused", "stopped", "ended", "loading", "error"}:
                item.track_id = actual.id; item.state = actual_state
                item.position = min(actual.duration, max(0, _number(player.get("position_sec"))))
                item.updated_at = aware(source.last_seen_at); restored = True
    if restore_actual and not restored:
        # An offline or malformed snapshot cannot be called a paused old track.
        item.state = "error"; item.updated_at = datetime.now(timezone.utc)
    meta.queue_command_id = None; meta.revision += 1
    return {"cancelled": cancelled, "restored_from_heartbeat": restored, "command_id": command.id}


async def advance_agent_queue(session, settings, source, public_origin: str) -> dict:
    """Call after authenticating an individual agent, ACKing commands and saving HB.

    The caller must pass its canonical configured public origin, never an origin
    from the agent payload. This helper commits its own queue/command mutations.
    ``awaiting_media`` is intentionally invisible to the usual command delivery.
    """
    now = datetime.now(timezone.utc)
    if source.source_type != "PC_AGENT" or not source_is_online(source, 2):
        return {"status": "ignored"}
    player = (source.last_payload or {}).get("music_player")
    if not isinstance(player, dict) or type(player.get("track_id")) is not int:
        return {"status": "ignored"}
    seen = aware(source.last_seen_at)
    if seen > now.replace(microsecond=999999) and (seen - now).total_seconds() > 5:
        return {"status": "ignored"}
    # Match the metadata -> source lock order used by explicit transfer routes.
    meta = await playback_meta(session)
    await session.refresh(meta)
    item = await session.scalar(select(MusicSession).where(MusicSession.id == 1)
        .execution_options(populate_existing=True))
    if item is None or item.device != "agent:" + source.source_name or not item.session_key or meta.transfer_id:
        return {"status": "ignored"}
    try:
        await ensure_agent_attached(session, source.source_name)
    except AgentDetachedError:
        return {"status": "ignored"}
    credential = await session.scalar(select(AgentCredential).where(
        AgentCredential.source_name == source.source_name, AgentCredential.is_active.is_(True)))
    if credential is None:
        return {"status": "ignored"}
    command = await session.get(AgentCommand, meta.queue_command_id) if meta.queue_command_id else None
    if command is not None:
        payload = command.payload or {}
        if (payload.get("queue_managed") is not True or command.source_name != source.source_name
                or payload.get("queue_session_key") != item.session_key or payload.get("track_id") != item.track_id):
            if payload.get("queue_managed") is True and command.status in {"awaiting_media", "pending"}:
                command.status = "cancelled"
            meta.queue_command_id = None
            await session.commit()
            return {"status": "ignored"}
        if command.status in {"failed", "cancelled"}:
            if command.status == "failed" and item.state != "error":
                item.state = "error"; item.updated_at = now; meta.revision += 1
                await session.commit()
            return {"status": command.status, "command_id": command.id}
        if command.status == "awaiting_media":
            return await _prepare(session, settings, source, credential, meta, item, command, public_origin)
        expires = _number(payload.get("expires_at"))
        if command.status in {"pending", "delivered"}:
            if expires <= now.timestamp():
                return await _failed(session, meta, item, command, "queue_start_timeout",
                    "ПК не подтвердил запуск следующего трека. Повторите воспроизведение.")
            return {"status": "waiting", "command_id": command.id}
        if command.status != "completed" or (command.result or {}).get("ok") is not True:
            return await _failed(session, meta, item, command, "queue_start_failed", "ПК не подтвердил следующий трек.")
        if payload.get("queue_finished"):
            return {"status": "ended", "command_id": command.id}
        # Ignore a repeated/old snapshot even if its command ACK arrived later.
        if not command.completed_at or seen <= aware(command.completed_at):
            return {"status": "waiting", "command_id": command.id}
        try:
            previous_seen = datetime.fromisoformat(payload.get("queue_last_report_at") or "")
        except (TypeError, ValueError):
            previous_seen = aware(command.created_at)
        if seen <= aware(previous_seen):
            return {"status": "waiting", "command_id": command.id}
        if player["track_id"] != item.track_id:
            if now.timestamp() > expires:
                return await _failed(session, meta, item, command, "queue_track_mismatch", "ПК не сообщил воспроизведение следующего трека.")
            return {"status": "waiting", "command_id": command.id}
        if player.get("state") == "error":
            return await _failed(session, meta, item, command, "queue_player_error", "Агент не смог воспроизвести следующий трек.")
        payload = {**payload, "queue_last_report_at": seen.isoformat()}
        if player.get("state") == "playing":
            payload["queue_seen_playing"] = True
        command.payload = payload
        if player.get("state") in {"playing", "paused", "stopped", "loading"}:
            item.state = player["state"]
            item.position = max(0, _number(player.get("position_sec")))
            item.updated_at = seen
            if player["state"] == "loading" and now.timestamp() > expires:
                return await _failed(session, meta, item, command, "queue_start_timeout", "Следующий трек не начал воспроизводиться. Проверьте агент.")
            await session.commit()
            return {"status": item.state, "command_id": command.id}
        if payload.get("queue_from_track_id") == item.track_id and not payload.get("queue_seen_playing"):
            # Repeat-one cannot distinguish a stale ended snapshot by track ID.
            # Require a genuine playing report in this acknowledged cycle first.
            if now.timestamp() > expires:
                return await _failed(session, meta, item, command, "queue_playback_unconfirmed",
                    "ПК не подтвердил новый цикл повтора. Запустите трек ещё раз.")
            await session.commit()
            return {"status": "waiting", "command_id": command.id}
    elif player["track_id"] == item.track_id and player.get("state") == "playing" and seen > aware(item.updated_at):
        # A manual resume is acknowledged by the actual player, not by HTTP
        # acceptance. This also re-arms an explicitly paused queue safely.
        item.state = "playing"; item.position = max(0, _number(player.get("position_sec"))); item.updated_at = seen
        await session.commit()
        return {"status": "playing"}
    elif item.state not in {"playing", "loading"} or seen <= aware(item.updated_at):
        return {"status": "ignored"}
    if player.get("state") != "ended" or player["track_id"] != item.track_id:
        return {"status": "ignored"}
    next_track = await _next_track(session, item.track_id, meta.queue, meta.repeat_mode)
    if next_track is None:
        item.state = "ended"; item.position = max(0, _number(player.get("position_sec"))); item.updated_at = seen
        if command:
            command.payload = {**command.payload, "queue_finished": True}
        meta.revision += 1
        await session.commit()
        return {"status": "ended"}
    volume = max(0, min(100, _number(player.get("volume"), meta.volume)))
    output = player.get("output_id")
    output = output if isinstance(output, str) and 0 < len(output) <= 256 else meta.output_id or "default"
    command = AgentCommand(source_name=source.source_name, command="music_play", status="awaiting_media",
        requested_by_user_id=getattr(settings, "owner_user_id", None), payload={
            "queue_managed": True, "queue_session_key": item.session_key, "queue_from_track_id": item.track_id,
            "queue_seen_playing": False, "queue_last_report_at": seen.isoformat(),
            "track_id": next_track.id, "title": next_track.title, "artist": next_track.artist,
            "output_id": output, "volume": volume, "position_sec": 0})
    session.add(command); await session.flush()
    meta.queue_command_id = command.id; meta.output_id = output; meta.volume = volume; meta.revision += 1
    item.track_id = next_track.id; item.position = 0; item.state = "loading"; item.updated_at = now
    # Reservation and command publication are atomic. There is no intermediate
    # commit from enqueue_agent_command that could expose an unreserved command.
    return await _prepare(session, settings, source, credential, meta, item, command, public_origin)


async def _prepare(session, settings, source, credential, meta, item, command, public_origin):
    now = datetime.now(timezone.utc)
    target = await session.get(MusicTrack, item.track_id)
    if target is None or target.deleted or target.mime == "audio/mp4":
        return await _failed(session, meta, item, command, "queue_track_unavailable", "Следующий трек удалён или недоступен для ПК.")
    if (now - aware(command.created_at)).total_seconds() > 900:
        return await _failed(session, meta, item, command, "queue_restore_timeout", "Восстановление следующего трека не завершено. Проверьте хранилище.")
    if not managed_path(settings, target).is_file():
        # Restore scheduling may commit. Reacquire and validate the reservation
        # before publishing status, so concurrent owner cancellation wins.
        command.result = {"ok": False, "message": "Ожидаем восстановление трека с агента", "details": {"reason": "awaiting_media"}}
        state = await ensure_restore_requested(session, settings, target)
        meta = await playback_meta(session); await session.refresh(meta)
        item = await session.scalar(select(MusicSession).where(MusicSession.id == 1)
            .execution_options(populate_existing=True))
        await session.refresh(command)
        if (meta.queue_command_id != command.id or command.status != "awaiting_media"
                or not item or item.session_key != command.payload.get("queue_session_key")
                or item.track_id != command.payload.get("track_id")):
            return {"status": "ignored"}
        if state.get("status") == "file_unavailable":
            return await _failed(session, meta, item, command, "queue_file_unavailable",
                "Файл следующего трека недоступен. Загрузите его повторно или выберите другой трек.")
        if state.get("status") == "agent_offline":
            command.result = {"ok": False, "message": "Агент с файлом не в сети. Очередь ждёт подключения хранилища.",
                              "details": {"reason": "agent_offline"}}
        await session.commit()
        return {"status": "awaiting_media", "command_id": command.id, "storage": state}
    try:
        origin = _origin(public_origin)
    except ValueError:
        return await _failed(session, meta, item, command, "queue_origin_invalid", "Проверьте публичный адрес сервера для воспроизведения на ПК.")
    ticket = issue_ticket(settings, target.id, purpose="agent", binding=credential.api_key_hash, ttl=600)
    media_path = f"/agent/music/tracks/{target.id}/stream?ticket={ticket}"
    command.payload = {**command.payload, "url": origin + media_path, "media_path": media_path,
                       "expires_at": int(now.timestamp()) + 120}
    command.status = "pending"; command.result = {}
    await session.commit()
    return {"status": "waiting", "command_id": command.id}
