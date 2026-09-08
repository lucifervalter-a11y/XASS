"""An explicit, revocable now-playing broadcast, never the private library."""
from datetime import datetime, timezone
import math
from pathlib import Path

from sqlalchemy import select

from app.models import HeartbeatSource
from app.music_models import MusicSession, MusicTrack
from app.services.profile_editor import load_profile, save_profile


async def current_broadcast(session):
    item = await session.get(MusicSession, 1)
    if item is None or not item.share_site or not item.track_id:
        return None
    track = await session.get(MusicTrack, item.track_id)
    if track is None or track.deleted:
        return None
    position, state = item.position, item.state
    timestamp = item.updated_at
    if item.device.startswith("agent:"):
        source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == item.device[6:]))
        if source is None or not source.is_online:
            return None
        details = (source.last_payload or {}).get("music_player") or {}
        if not isinstance(details, dict):
            return None
        if details.get("track_id") != item.track_id:
            return None
        state = str(details.get("state") or "idle")
        try:
            position = float(details.get("position_sec") or 0)
        except (ValueError, TypeError):
            return None
        timestamp = source.last_seen_at
    if timestamp is None or not math.isfinite(position) or position < 0:
        return None
    timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
    age = max(0, (datetime.now(timezone.utc) - timestamp).total_seconds())
    if item.device.startswith("agent:") and age > 120:
        return None
    if state != "playing" or age > max(90, track.duration - position + 30):
        return None
    position = min(track.duration, position + age)
    if position >= track.duration:
        return None
    return track, position


async def sync_music_profile(session, settings) -> bool:
    """Return True when this source owns the public now-playing field."""
    item = await session.get(MusicSession, 1)
    path = Path(settings.profile_json_path)
    profile = load_profile(path)
    if item is None or not item.share_site:
        if profile.get("now_listening_source") == "xass_music":
            profile["now_listening_source"] = item.previous_source if item else "pc_agent"
            profile["now_listening_text"] = "Сейчас ничего не играет"
            save_profile(path, profile)
        return False
    playing = await current_broadcast(session)
    text = " — ".join(value for value in (playing[0].artist, playing[0].title) if value) if playing else "Сейчас ничего не играет"
    if profile.get("now_listening_source") != "xass_music" or profile.get("now_listening_text") != text:
        profile.update(now_listening_source="xass_music", now_listening_text=text,
                       now_listening_updated_at=datetime.now(timezone.utc).isoformat())
        save_profile(path, profile)
    return True
