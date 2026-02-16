import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AgentCredential, AppConfig, HeartbeatSource
from app.schemas import HeartbeatPayload

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _resolve_timezone(tz_name: str) -> timezone | ZoneInfo:
    cleaned = (tz_name or "").strip() or "UTC"
    try:
        return ZoneInfo(cleaned)
    except ZoneInfoNotFoundError:
        logger.warning("Timezone '%s' not found, using UTC fallback", cleaned)
        return timezone.utc


def is_quiet_hours(config: AppConfig, settings: Settings) -> bool:
    if not config.quiet_hours_enabled:
        return False
    start_minute = config.quiet_hours_start_minute
    end_minute = config.quiet_hours_end_minute
    if start_minute is None or end_minute is None:
        if config.quiet_hours_start is None or config.quiet_hours_end is None:
            return False
        start_minute = int(config.quiet_hours_start) * 60
        end_minute = int(config.quiet_hours_end) * 60

    now = _now_utc().astimezone(_resolve_timezone(settings.timezone))
    current_minute = now.hour * 60 + now.minute
    start = int(start_minute) % (24 * 60)
    end = int(end_minute) % (24 * 60)
    if start == end:
        return False
    if start < end:
        return start <= current_minute < end
    return current_minute >= start or current_minute < end


async def process_heartbeat(
    session: AsyncSession,
    payload: HeartbeatPayload,
) -> tuple[HeartbeatSource, bool, bool]:
    source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == payload.source_name))
    recovered = False
    is_new = source is None
    now = _now_utc()
    raw_payload = payload.model_dump(mode="json")

    if is_new:
        source = HeartbeatSource(
            source_name=payload.source_name,
            source_type=payload.source_type.value,
            is_online=True,
            last_seen_at=now,
            last_payload=raw_payload,
        )
        session.add(source)
    else:
        if not source.is_online:
            recovered = True
        source.source_type = payload.source_type.value
        source.is_online = True
        source.last_seen_at = now
        source.went_offline_at = None
        source.last_payload = raw_payload

    await session.commit()
    await session.refresh(source)
    return source, recovered, is_new


async def mark_offline_sources(
    session: AsyncSession,
    timeout_minutes: int,
) -> list[HeartbeatSource]:
    threshold = _now_utc() - timedelta(minutes=timeout_minutes)
    stale_sources = await session.scalars(
        select(HeartbeatSource).where(
            HeartbeatSource.is_online.is_(True),
            HeartbeatSource.last_seen_at < threshold,
        )
    )
    stale_list = list(stale_sources)
    if not stale_list:
        return []

    now = _now_utc()
    for source in stale_list:
        source.is_online = False
        source.went_offline_at = now

    await session.commit()
    return stale_list


async def list_sources(session: AsyncSession) -> list[HeartbeatSource]:
    sources = await session.scalars(select(HeartbeatSource).order_by(HeartbeatSource.source_name.asc()))
    return list(sources)


async def rename_source(session: AsyncSession, current_name: str, new_name: str) -> HeartbeatSource | None:
    source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == current_name))
    if source is None:
        return None
    if current_name == new_name:
        return source
    existing = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == new_name))
    if existing is not None and existing.id != source.id:
        return None
    existing_credential = await session.scalar(select(AgentCredential).where(AgentCredential.source_name == new_name))
    if existing_credential is not None and existing_credential.source_name != current_name:
        return None
    credential = await session.scalar(select(AgentCredential).where(AgentCredential.source_name == current_name))
    source.source_name = new_name
    if credential is not None:
        credential.source_name = new_name
    await session.commit()
    await session.refresh(source)
    return source


async def delete_source_by_id(session: AsyncSession, source_id: int) -> HeartbeatSource | None:
    source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.id == source_id))
    if source is None:
        return None
    credential = await session.scalar(select(AgentCredential).where(AgentCredential.source_name == source.source_name))
    if credential is not None:
        await session.delete(credential)
    await session.delete(source)
    await session.commit()
    return source


def format_source_line(source: HeartbeatSource, timeout_minutes: int) -> str:
    age = int((_now_utc() - _ensure_utc(source.last_seen_at)).total_seconds())
    status = "ONLINE" if source.is_online else "OFFLINE"
    return (
        f"- {source.source_name} ({source.source_type}) -> {status}, "
        f"last_seen={age}s ago, timeout={timeout_minutes}m"
    )
