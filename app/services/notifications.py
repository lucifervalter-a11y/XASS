from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import InternalNotification, NotificationPreference
from app.services.app_config import get_or_create_app_config, sanitize_audit_payload
from app.services.heartbeat import is_quiet_hours
from app.services.web_push import send_push


EVENT_TYPES = (
    "agent_connected",
    "agent_offline",
    "agent_recovered",
    "high_load",
    "low_disk",
    "archive_error",
    "media_error",
    "update_available",
    "update_completed",
    "update_rollback",
    "dangerous_command",
    "new_login",
    "passkey_registered",
    "pair_code_created",
    "auth_failed",
)
CHANNELS = {"internal", "telegram", "push"}
PRIORITIES = {"low", "normal", "high", "critical"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _default_policy(event_type: str) -> dict[str, Any]:
    critical = event_type in {"agent_offline", "archive_error", "update_rollback", "auth_failed"}
    return {
        "event_type": event_type,
        "channels": ["internal", "telegram"],
        "priority": "high" if critical else "normal",
        "quiet_hours": not critical,
    }


async def notification_policy(session: AsyncSession, event_type: str) -> dict[str, Any]:
    row = await session.scalar(select(NotificationPreference).where(NotificationPreference.event_type == event_type))
    if row is None:
        return _default_policy(event_type)
    return {
        "event_type": row.event_type,
        "channels": [item for item in (row.channels or []) if item in CHANNELS],
        "priority": row.priority if row.priority in PRIORITIES else "normal",
        "quiet_hours": bool(row.quiet_hours),
    }


async def emit_notification(
    session: AsyncSession,
    *,
    event_type: str,
    title: str,
    message: str,
    device: str = "",
    priority: str = "",
    requires_action: bool = False,
    details: dict[str, Any] | None = None,
    dedup_key: str = "",
    cooldown_sec: int = 3600,
) -> tuple[InternalNotification | None, dict[str, Any]]:
    policy = await notification_policy(session, event_type)
    channels = policy["channels"]
    if not channels:
        return None, policy
    key = dedup_key.strip()[:255] or None
    if key and cooldown_sec > 0:
        cutoff = _now_utc() - timedelta(seconds=max(1, cooldown_sec))
        duplicate = await session.scalar(
            select(InternalNotification.id).where(
                InternalNotification.dedup_key == key,
                InternalNotification.created_at >= cutoff,
            )
        )
        if duplicate is not None:
            return None, policy
    if "push" in channels:
        current_settings = get_settings()
        app_config = await get_or_create_app_config(session, current_settings)
        if not policy["quiet_hours"] or not is_quiet_hours(app_config, current_settings):
            await send_push(
                session,
                current_settings,
                title=title,
                message=message,
                event_type=event_type,
                priority=priority if priority in PRIORITIES else policy["priority"],
            )
    internal_enabled = "internal" in channels
    clean_details = sanitize_audit_payload(details or {})
    item = InternalNotification(
        event_type=event_type[:96],
        title=title.strip()[:180] or "XASS",
        message=message.strip()[:4000],
        device=device.strip()[:128] or None,
        priority=priority if priority in PRIORITIES else policy["priority"],
        status=("action" if requires_action else "new") if internal_enabled else "hidden",
        requires_action=requires_action,
        details=clean_details if isinstance(clean_details, dict) else {},
        dedup_key=key,
        hidden_at=None if internal_enabled else _now_utc(),
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item, policy


def notification_json(item: InternalNotification) -> dict[str, Any]:
    return {
        "id": item.id,
        "event_type": item.event_type,
        "title": item.title,
        "message": item.message,
        "priority": item.priority,
        "status": item.status,
        "requires_action": bool(item.requires_action),
        "device": item.device or "",
        "details": item.details or {},
        "read_at": item.read_at.isoformat() if item.read_at else None,
        "created_at": item.created_at.isoformat(),
    }


async def list_notifications(
    session: AsyncSession, *, status: str = "", limit: int = 50
) -> list[InternalNotification]:
    statement = select(InternalNotification).where(InternalNotification.status != "hidden")
    if status in {"new", "read", "action"}:
        statement = statement.where(InternalNotification.status == status)
    rows = await session.scalars(
        statement.order_by(InternalNotification.id.desc()).limit(max(1, min(int(limit), 200)))
    )
    return list(rows)


async def unread_notification_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count(InternalNotification.id)).where(
                InternalNotification.status.in_(["new", "action"])
            )
        )
        or 0
    )


async def set_notification_status(
    session: AsyncSession, notification_id: int, status: str
) -> InternalNotification | None:
    item = await session.get(InternalNotification, int(notification_id))
    if item is None:
        return None
    now = _now_utc()
    item.status = status
    if status == "read":
        item.read_at = now
    if status == "hidden":
        item.hidden_at = now
    await session.commit()
    await session.refresh(item)
    return item


async def mark_all_read(session: AsyncSession) -> None:
    await session.execute(
        update(InternalNotification)
        .where(InternalNotification.status.in_(["new", "action"]))
        .values(status="read", read_at=_now_utc())
    )
    await session.commit()


async def list_preferences(session: AsyncSession) -> list[dict[str, Any]]:
    rows = list(await session.scalars(select(NotificationPreference)))
    by_type = {item.event_type: item for item in rows}
    result: list[dict[str, Any]] = []
    for event_type in EVENT_TYPES:
        item = by_type.get(event_type)
        result.append(
            _default_policy(event_type)
            if item is None
            else {
                "event_type": item.event_type,
                "channels": [value for value in (item.channels or []) if value in CHANNELS],
                "priority": item.priority,
                "quiet_hours": bool(item.quiet_hours),
            }
        )
    return result


async def save_preference(
    session: AsyncSession,
    *,
    event_type: str,
    channels: list[str],
    priority: str,
    quiet_hours: bool,
    actor_user_id: int,
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ValueError("Unsupported notification event")
    clean_channels = list(dict.fromkeys(item for item in channels if item in CHANNELS))
    if priority not in PRIORITIES:
        raise ValueError("Unsupported notification priority")
    row = await session.scalar(select(NotificationPreference).where(NotificationPreference.event_type == event_type))
    if row is None:
        row = NotificationPreference(event_type=event_type)
        session.add(row)
    row.channels = clean_channels
    row.priority = priority
    row.quiet_hours = bool(quiet_hours)
    row.updated_by_user_id = actor_user_id
    await session.commit()
    return {
        "event_type": event_type,
        "channels": clean_channels,
        "priority": priority,
        "quiet_hours": bool(quiet_hours),
    }
