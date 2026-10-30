"""Atomic reservations for legacy/direct PC starts, without optimistic sound."""
from datetime import datetime, timezone
import hashlib

from fastapi import HTTPException
from sqlalchemy import select, update

from app.models import AgentCommand
from app.music_models import MusicSession
from app.music_playback_models import MusicPlaybackState


async def direct_start_command(session, item, meta=None):
    if not item or not item.device.startswith("agent:"):
        return None
    meta = meta or await session.get(MusicPlaybackState, 1)
    command = await session.get(AgentCommand, meta.queue_command_id) if meta and meta.queue_command_id else None
    payload = (command.payload or {}) if command else {}
    return command if (command and payload.get("direct_start") is True
        and command.source_name == item.device[6:] and payload.get("queue_session_key") == item.session_key
        and payload.get("track_id") == item.track_id) else None


def direct_start_confirmed(command, source):
    """ACK plus a newer actual player report, never timeout/acceptance alone."""
    from app.music_playback import aware, music_source_live
    if (command.status not in {"completed", "failed"} or not command.completed_at
            or not music_source_live(source) or aware(source.last_seen_at) <= aware(command.completed_at)):
        return False
    player = (source.last_payload or {}).get("music_player") or {}
    if not isinstance(player, dict):
        return False
    state = player.get("state")
    return (state in {"paused", "stopped", "ended", "idle", "error"}
        or (state == "playing" and player.get("track_id") == command.payload.get("track_id")))


async def reserve_direct_start(session, *, source_name, action, details, actor_user_id, client_id="legacy-controller"):
    """Lease, queue supersession and command become visible in ONE commit.

    All callers use the metadata -> session lock order. Holding this reservation
    across enqueue prevents concurrent phones, desktop starts and heartbeat queue
    advancement from observing a command without its loading lease.
    """
    from app.music_playback import playback_meta
    meta = await playback_meta(session)
    active = await session.scalar(select(MusicSession).where(MusicSession.id == 1)
        .with_for_update().execution_options(populate_existing=True))
    device = "agent:" + source_name
    if meta.transfer_id or (active and active.device != device and active.state in {"playing", "loading"}):
        raise HTTPException(409, "Сначала подтвердите передачу через «Где слушать»")
    if action == "resume" and (not active or active.device != device or not active.track_id):
        raise HTTPException(409, "Сначала выберите трек и подтвердите передачу на этот ПК")
    if active is None:
        active = MusicSession(id=1)
        session.add(active)
    same_player = active.device == device and bool(active.session_key)
    if not same_player:
        active.session_key = "agent-" + hashlib.sha256(source_name.encode()).hexdigest()[:48]
    if action == "resume":
        details = {**details, "track_id": active.track_id, "position_sec": active.position}
    active.track_id, active.device = details["track_id"], device
    active.state, active.position, active.updated_at = "loading", details.get("position_sec", 0), datetime.now(timezone.utc)
    meta.client_id, meta.output_id, meta.volume = client_id, details.get("output_id", "default"), details.get("volume", 70)
    meta.revision += 1
    # A delivered older command is not called cancelled; the reserved PC still
    # owns the lease until actual playback/stop has been observed.
    await session.execute(update(AgentCommand).where(AgentCommand.source_name == source_name,
        AgentCommand.command.in_(("music_play", "music_resume")), AgentCommand.status == "pending")
        .values(status="cancelled"))
    command = AgentCommand(source_name=source_name, command="music_" + action,
        payload={**details, "direct_start": True, "queue_session_key": active.session_key},
        status="pending", requested_by_user_id=actor_user_id)
    session.add(command)
    try:
        await session.flush()
        meta.queue_command_id = command.id
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return command
