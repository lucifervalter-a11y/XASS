from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAction, AgentCommand, AgentStateSnapshot, InternalNotification


def _utc_iso(value: datetime | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _level(priority: str) -> str:
    return {
        "low": "info", "normal": "info", "high": "warning",
        "info": "info", "success": "success", "warning": "warning", "critical": "critical",
    }.get(priority, "info")


async def timeline_items(
    session: AsyncSession,
    *,
    limit: int = 100,
    event_type: str = "",
    device: str = "",
    level: str = "",
) -> list[dict[str, Any]]:
    fetch_limit = max(30, min(int(limit) * 2, 300))
    notifications = list(await session.scalars(select(InternalNotification).order_by(InternalNotification.id.desc()).limit(fetch_limit)))
    commands = list(await session.scalars(select(AgentCommand).order_by(AgentCommand.id.desc()).limit(fetch_limit)))
    actions = list(await session.scalars(select(AdminAction).order_by(AdminAction.id.desc()).limit(fetch_limit)))
    snapshots = list(await session.scalars(select(AgentStateSnapshot).order_by(AgentStateSnapshot.id.desc()).limit(fetch_limit)))
    rows: list[dict[str, Any]] = []
    for item in notifications:
        rows.append({
            "id": f"notification:{item.id}", "type": "notification", "event_type": item.event_type,
            "title": item.title, "message": item.message, "device": item.device or "",
            "level": _level(item.priority), "status": item.status, "created_at": _utc_iso(item.created_at),
        })
    for item in commands:
        ok = bool((item.result or {}).get("ok"))
        rows.append({
            "id": f"command:{item.id}", "type": "command", "event_type": f"command_{item.command}",
            "title": f"{item.command} · {item.source_name}",
            "message": str((item.result or {}).get("message") or "Команда поставлена в очередь"),
            "device": item.source_name, "level": "critical" if item.status == "failed" else "success" if item.status == "completed" and ok else "info",
            "status": item.status, "created_at": _utc_iso(item.completed_at or item.created_at),
        })
    for item in actions:
        payload = item.payload if isinstance(item.payload, dict) else {}
        rows.append({
            "id": f"action:{item.id}", "type": "action", "event_type": item.action,
            "title": item.action.replace("_", " "),
            "message": " · ".join(str(payload.get(key)) for key in ("command", "status", "result", "message") if payload.get(key)) or "Действие пользователя",
            "device": str(payload.get("source_name") or payload.get("device") or ""),
            "level": "critical" if payload.get("status") == "failed" or payload.get("result") == "failure" else "info",
            "status": str(payload.get("status") or payload.get("result") or "completed"), "created_at": _utc_iso(item.created_at),
        })
    for item in snapshots:
        rows.append({
            "id": f"state:{item.id}", "type": "agent", "event_type": "agent_online" if item.is_online else "agent_offline",
            "title": f"{item.source_name} {'подключён' if item.is_online else 'потерял связь'}",
            "message": item.last_error or f"Агент v{item.agent_version}", "device": item.source_name,
            "level": "success" if item.is_online else "warning", "status": "online" if item.is_online else "offline",
            "created_at": _utc_iso(item.created_at),
        })
    wanted_type, wanted_device, wanted_level = event_type.strip().lower(), device.strip().lower(), level.strip().lower()
    if wanted_type:
        rows = [item for item in rows if item["type"].lower() == wanted_type or item["event_type"].lower() == wanted_type]
    if wanted_device:
        rows = [item for item in rows if wanted_device in item["device"].lower()]
    if wanted_level:
        rows = [item for item in rows if item["level"].lower() == wanted_level]
    rows.sort(key=lambda item: item["created_at"], reverse=True)
    return rows[: max(1, min(int(limit), 200))]
