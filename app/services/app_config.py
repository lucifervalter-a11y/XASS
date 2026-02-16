from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.enums import SaveMode
from app.models import AdminAction, AppConfig


DEFAULT_TIMEOUT_OPTIONS = (5, 10, 30, 60)
DEFAULT_AWAY_MESSAGE = (
    "Я сейчас не в сети.\n"
    "Пожалуйста, напишите позже.\n"
    "Ваше сообщение сохранено."
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _resolve_timezone(tz_name: str) -> timezone | ZoneInfo:
    cleaned = (tz_name or "").strip() or "UTC"
    try:
        return ZoneInfo(cleaned)
    except ZoneInfoNotFoundError:
        return timezone.utc


def minute_to_hhmm(value: int | None) -> str:
    if value is None:
        return "--:--"
    minute = int(value) % (24 * 60)
    return f"{minute // 60:02d}:{minute % 60:02d}"


def parse_hhmm(raw: str) -> int:
    text = (raw or "").strip()
    if ":" not in text:
        raise ValueError("Ожидается формат ЧЧ:ММ")
    left, right = text.split(":", maxsplit=1)
    if not left.isdigit() or not right.isdigit():
        raise ValueError("Часы и минуты должны быть числами")
    hour = int(left)
    minute = int(right)
    if hour < 0 or hour > 23:
        raise ValueError("Часы должны быть в диапазоне 00..23")
    if minute < 0 or minute > 59:
        raise ValueError("Минуты должны быть в диапазоне 00..59")
    return hour * 60 + minute


def parse_time_range(raw: str) -> tuple[int, int]:
    text = (raw or "").strip().replace("—", "-")
    if "-" not in text:
        raise ValueError("Ожидается формат ЧЧ:ММ-ЧЧ:ММ")
    left, right = text.split("-", maxsplit=1)
    start = parse_hhmm(left)
    end = parse_hhmm(right)
    if start == end:
        raise ValueError("Начало и конец диапазона не должны совпадать")
    return start, end


def format_time_range(start_minute: int | None, end_minute: int | None) -> str:
    if start_minute is None or end_minute is None:
        return "не задано"
    return f"{minute_to_hhmm(start_minute)}-{minute_to_hhmm(end_minute)}"


def _parse_user_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    values: set[int] = set()
    for part in str(raw).split(","):
        item = part.strip()
        if not item:
            continue
        if not item.lstrip("-").isdigit():
            continue
        values.add(int(item))
    return values


def _serialize_user_ids(user_ids: set[int]) -> str:
    return ",".join(str(item) for item in sorted(user_ids))


def get_away_bypass_user_ids(config: AppConfig) -> set[int]:
    return _parse_user_ids(config.away_bypass_user_ids)


def is_in_daily_window(local_minute: int, start_minute: int, end_minute: int) -> bool:
    start = int(start_minute) % (24 * 60)
    end = int(end_minute) % (24 * 60)
    current = int(local_minute) % (24 * 60)
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def is_away_mode_active(config: AppConfig, settings: Settings, now_utc: datetime | None = None) -> bool:
    if config.away_mode_enabled:
        return True

    now = _ensure_aware_utc(now_utc) or _now_utc()
    away_until = _ensure_aware_utc(config.away_until_at)
    if away_until and now < away_until:
        return True

    if config.away_schedule_enabled and config.away_schedule_start_minute is not None and config.away_schedule_end_minute is not None:
        local_now = now.astimezone(_resolve_timezone(settings.timezone))
        local_minute = local_now.hour * 60 + local_now.minute
        return is_in_daily_window(local_minute, config.away_schedule_start_minute, config.away_schedule_end_minute)

    return False


async def get_or_create_app_config(session: AsyncSession, settings: Settings) -> AppConfig:
    config = await session.get(AppConfig, 1)
    if config:
        changed = False

        if config.away_mode_message is None:
            config.away_mode_message = DEFAULT_AWAY_MESSAGE
            changed = True

        if config.quiet_hours_start_minute is None:
            fallback = 23 if config.quiet_hours_start is None else int(config.quiet_hours_start)
            config.quiet_hours_start_minute = max(0, min(1439, fallback * 60))
            changed = True
        if config.quiet_hours_end_minute is None:
            fallback = 8 if config.quiet_hours_end is None else int(config.quiet_hours_end)
            config.quiet_hours_end_minute = max(0, min(1439, fallback * 60))
            changed = True

        if config.quiet_hours_start is None:
            config.quiet_hours_start = int(config.quiet_hours_start_minute // 60)
            changed = True
        if config.quiet_hours_end is None:
            config.quiet_hours_end = int(config.quiet_hours_end_minute // 60)
            changed = True

        if config.away_schedule_start_minute is None:
            config.away_schedule_start_minute = 23 * 60
            changed = True
        if config.away_schedule_end_minute is None:
            config.away_schedule_end_minute = 8 * 60
            changed = True

        if config.away_bypass_user_ids is None:
            config.away_bypass_user_ids = ""
            changed = True
        if config.service_base_url is None:
            config.service_base_url = (settings.profile_public_url or "").strip() or None
            changed = True

        if changed:
            await session.commit()
            await session.refresh(config)
        return config

    config = AppConfig(
        id=1,
        save_mode=SaveMode.SAVE_BASIC.value,
        heartbeat_timeout_minutes=10,
        quiet_hours_enabled=False,
        quiet_hours_start=23,
        quiet_hours_end=8,
        quiet_hours_start_minute=23 * 60,
        quiet_hours_end_minute=8 * 60,
        away_mode_enabled=False,
        away_mode_message=DEFAULT_AWAY_MESSAGE,
        away_until_at=None,
        away_schedule_enabled=False,
        away_schedule_start_minute=23 * 60,
        away_schedule_end_minute=8 * 60,
        away_bypass_user_ids="",
        notify_chat_id=settings.notify_chat_id,
        service_base_url=(settings.profile_public_url or "").strip() or None,
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return config


async def set_save_mode(session: AsyncSession, config: AppConfig, mode: SaveMode, actor_user_id: int) -> AppConfig:
    config.save_mode = mode.value
    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(session, actor_user_id, "set_save_mode", {"mode": mode.value})
    return config


async def cycle_save_mode(session: AsyncSession, config: AppConfig, actor_user_id: int) -> AppConfig:
    modes = [
        SaveMode.SAVE_OFF,
        SaveMode.SAVE_BASIC,
        SaveMode.SAVE_FULL,
        SaveMode.SAVE_PRIVATE_ONLY,
        SaveMode.SAVE_GROUPS_ONLY,
    ]
    current = SaveMode(config.save_mode)
    next_mode = modes[(modes.index(current) + 1) % len(modes)]
    return await set_save_mode(session, config, next_mode, actor_user_id)


async def cycle_timeout(session: AsyncSession, config: AppConfig, actor_user_id: int) -> AppConfig:
    current = config.heartbeat_timeout_minutes
    if current not in DEFAULT_TIMEOUT_OPTIONS:
        config.heartbeat_timeout_minutes = DEFAULT_TIMEOUT_OPTIONS[0]
    else:
        index = DEFAULT_TIMEOUT_OPTIONS.index(current)
        config.heartbeat_timeout_minutes = DEFAULT_TIMEOUT_OPTIONS[(index + 1) % len(DEFAULT_TIMEOUT_OPTIONS)]

    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(
        session,
        actor_user_id,
        "set_heartbeat_timeout",
        {"minutes": config.heartbeat_timeout_minutes},
    )
    return config


async def toggle_quiet_hours(session: AsyncSession, config: AppConfig, actor_user_id: int) -> AppConfig:
    config.quiet_hours_enabled = not config.quiet_hours_enabled
    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(
        session,
        actor_user_id,
        "toggle_quiet_hours",
        {"enabled": config.quiet_hours_enabled},
    )
    return config


async def set_quiet_hours_window(
    session: AsyncSession,
    config: AppConfig,
    *,
    start_minute: int,
    end_minute: int,
    actor_user_id: int,
) -> AppConfig:
    config.quiet_hours_start_minute = int(start_minute) % (24 * 60)
    config.quiet_hours_end_minute = int(end_minute) % (24 * 60)
    config.quiet_hours_start = int(config.quiet_hours_start_minute // 60)
    config.quiet_hours_end = int(config.quiet_hours_end_minute // 60)
    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(
        session,
        actor_user_id,
        "set_quiet_hours_window",
        {
            "start_minute": config.quiet_hours_start_minute,
            "end_minute": config.quiet_hours_end_minute,
            "range": format_time_range(config.quiet_hours_start_minute, config.quiet_hours_end_minute),
        },
    )
    return config


async def set_away_mode(session: AsyncSession, config: AppConfig, enabled: bool, actor_user_id: int) -> AppConfig:
    config.away_mode_enabled = enabled
    if enabled:
        config.away_until_at = None
    else:
        config.away_until_at = None
    if not config.away_mode_message:
        config.away_mode_message = DEFAULT_AWAY_MESSAGE
    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(
        session,
        actor_user_id,
        "set_away_mode",
        {"enabled": enabled},
    )
    return config


async def toggle_away_mode(session: AsyncSession, config: AppConfig, actor_user_id: int) -> AppConfig:
    return await set_away_mode(session, config, not config.away_mode_enabled, actor_user_id)


async def set_away_for_minutes(
    session: AsyncSession,
    config: AppConfig,
    *,
    minutes: int,
    actor_user_id: int,
) -> AppConfig:
    bounded = max(1, min(int(minutes), 7 * 24 * 60))
    config.away_mode_enabled = False
    config.away_until_at = _now_utc() + timedelta(minutes=bounded)
    if not config.away_mode_message:
        config.away_mode_message = DEFAULT_AWAY_MESSAGE
    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(
        session,
        actor_user_id,
        "set_away_for_minutes",
        {"minutes": bounded, "away_until_at": config.away_until_at.isoformat() if config.away_until_at else None},
    )
    return config


async def clear_away_until(session: AsyncSession, config: AppConfig, actor_user_id: int) -> AppConfig:
    config.away_until_at = None
    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(session, actor_user_id, "clear_away_until", {})
    return config


async def set_away_schedule(
    session: AsyncSession,
    config: AppConfig,
    *,
    enabled: bool,
    start_minute: int | None,
    end_minute: int | None,
    actor_user_id: int,
) -> AppConfig:
    config.away_schedule_enabled = bool(enabled)
    if start_minute is not None:
        config.away_schedule_start_minute = int(start_minute) % (24 * 60)
    if end_minute is not None:
        config.away_schedule_end_minute = int(end_minute) % (24 * 60)

    if config.away_schedule_start_minute is None:
        config.away_schedule_start_minute = 23 * 60
    if config.away_schedule_end_minute is None:
        config.away_schedule_end_minute = 8 * 60

    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(
        session,
        actor_user_id,
        "set_away_schedule",
        {
            "enabled": config.away_schedule_enabled,
            "start_minute": config.away_schedule_start_minute,
            "end_minute": config.away_schedule_end_minute,
            "range": format_time_range(config.away_schedule_start_minute, config.away_schedule_end_minute),
        },
    )
    return config


async def set_away_message(session: AsyncSession, config: AppConfig, text: str, actor_user_id: int) -> AppConfig:
    config.away_mode_message = text.strip() if text.strip() else DEFAULT_AWAY_MESSAGE
    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(
        session,
        actor_user_id,
        "set_away_message",
        {"text": config.away_mode_message[:200]},
    )
    return config


async def set_away_bypass_user_ids(
    session: AsyncSession,
    config: AppConfig,
    user_ids: set[int],
    actor_user_id: int,
) -> AppConfig:
    config.away_bypass_user_ids = _serialize_user_ids(user_ids)
    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(
        session,
        actor_user_id,
        "set_away_bypass_user_ids",
        {"count": len(user_ids), "user_ids": sorted(user_ids)},
    )
    return config


async def add_away_bypass_user_id(session: AsyncSession, config: AppConfig, bypass_user_id: int, actor_user_id: int) -> AppConfig:
    user_ids = get_away_bypass_user_ids(config)
    user_ids.add(int(bypass_user_id))
    return await set_away_bypass_user_ids(session, config, user_ids, actor_user_id)


async def remove_away_bypass_user_id(session: AsyncSession, config: AppConfig, bypass_user_id: int, actor_user_id: int) -> AppConfig:
    user_ids = get_away_bypass_user_ids(config)
    user_ids.discard(int(bypass_user_id))
    return await set_away_bypass_user_ids(session, config, user_ids, actor_user_id)


async def set_notify_chat(session: AsyncSession, config: AppConfig, chat_id: int, actor_user_id: int) -> AppConfig:
    config.notify_chat_id = chat_id
    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(session, actor_user_id, "set_notify_chat", {"chat_id": chat_id})
    return config


async def set_service_base_url(
    session: AsyncSession,
    config: AppConfig,
    service_base_url: str | None,
    actor_user_id: int,
) -> AppConfig:
    clean = (service_base_url or "").strip() or None
    config.service_base_url = clean
    config.updated_by_user_id = actor_user_id
    config.updated_at = _now_utc()
    await session.commit()
    await session.refresh(config)
    await log_admin_action(session, actor_user_id, "set_service_base_url", {"service_base_url": clean})
    return config


async def list_recent_admin_actions(session: AsyncSession, limit: int = 20) -> list[AdminAction]:
    result = await session.scalars(select(AdminAction).order_by(AdminAction.id.desc()).limit(limit))
    return list(result)


async def log_admin_action(
    session: AsyncSession,
    actor_user_id: int,
    action: str,
    payload: dict[str, Any] | None = None,
) -> None:
    entry = AdminAction(
        actor_user_id=actor_user_id,
        action=action,
        payload=payload or {},
    )
    session.add(entry)
    await session.commit()
