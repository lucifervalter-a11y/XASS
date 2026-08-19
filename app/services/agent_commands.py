from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAction, AgentCommand
from app.services.app_config import prepare_audit_payload
from app.services.notifications import emit_notification

ALLOWED_AGENT_COMMANDS = {
    "update",
    "check_update",
    "restart",
    "reboot",
    "shutdown",
    "sleep",
    "lock",
    "ping",
    "open_archive",
    "cleanup_archive",
    "screenshot",
    "files_list",
    "file_download",
    "file_upload",
    "file_delete",
    "clipboard_get",
    "clipboard_set",
    "migration_download",
}
DANGEROUS_AGENT_COMMANDS = {
    "lock", "sleep", "reboot", "shutdown", "restart", "update",
    "cleanup_archive", "file_delete", "migration_download",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue_agent_command(
    session: AsyncSession,
    *,
    source_name: str,
    command: str,
    payload: dict[str, Any] | None,
    actor_user_id: int | None,
    not_before_at: datetime | None = None,
) -> AgentCommand:
    normalized = (command or "").strip().lower()
    if normalized not in ALLOWED_AGENT_COMMANDS:
        raise ValueError(f"Unsupported agent command: {normalized}")
    item = AgentCommand(
        source_name=source_name,
        command=normalized,
        payload=payload or {},
        status="pending",
        requested_by_user_id=actor_user_id,
        not_before_at=not_before_at,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def acknowledge_agent_commands(
    session: AsyncSession,
    *,
    source_name: str,
    results: list[dict[str, Any]],
) -> None:
    if not results:
        return
    changed = False
    completed: list[tuple[str, str, bool, str, int]] = []
    for result in results[:50]:
        try:
            command_id = int(result.get("id"))
        except (TypeError, ValueError):
            continue
        item = await session.scalar(
            select(AgentCommand).where(
                AgentCommand.id == command_id,
                AgentCommand.source_name == source_name,
            )
        )
        if item is None or item.status in {"completed", "failed"}:
            continue
        ok = bool(result.get("ok"))
        item.status = "completed" if ok else "failed"
        item.result = {
            "ok": ok,
            "message": str(result.get("message") or "")[:1000],
            "details": result.get("details") if isinstance(result.get("details"), dict) else {},
        }
        item.completed_at = _now_utc()
        if item.requested_by_user_id:
            session.add(
                AdminAction(
                    actor_user_id=item.requested_by_user_id,
                    action="agent_command_result",
                    payload=prepare_audit_payload({
                        "command_id": item.id,
                        "source_name": item.source_name,
                        "command": item.command,
                        "channel": "pc",
                        "status": item.status,
                        "message": item.result["message"],
                    }),
                )
            )
        completed.append((item.command, item.source_name, ok, item.result["message"], item.id))
        changed = True
    if changed:
        await session.commit()
    for command, device, ok, message, command_id in completed:
        if command in DANGEROUS_AGENT_COMMANDS:
            await emit_notification(
                session,
                event_type="dangerous_command",
                title="Опасная команда выполнена" if ok else "Опасная команда завершилась ошибкой",
                message=f"{command}: {message or ('готово' if ok else 'ошибка')}",
                device=device,
                priority="high" if not ok else "normal",
                requires_action=not ok,
                details={"command": command, "command_id": command_id, "ok": ok},
                dedup_key=f"command-result:{command_id}",
                cooldown_sec=86400,
            )
        if command == "update" and ok:
            await emit_notification(
                session,
                event_type="update_completed",
                title="Агент обновлён",
                message=message or "Обновление агента завершено",
                device=device,
                details={"command_id": command_id},
                dedup_key=f"update-completed:{command_id}",
                cooldown_sec=86400,
            )


async def deliver_agent_commands(session: AsyncSession, *, source_name: str) -> list[dict[str, Any]]:
    rows = list(
        await session.scalars(
            select(AgentCommand)
            .where(
                AgentCommand.source_name == source_name,
                AgentCommand.status.in_(["pending", "delivered"]),
                or_(AgentCommand.not_before_at.is_(None), AgentCommand.not_before_at <= _now_utc()),
            )
            .order_by(AgentCommand.id.asc())
            .limit(10)
        )
    )
    now = _now_utc()
    changed = False
    result: list[dict[str, Any]] = []
    for item in rows:
        if item.status == "pending":
            item.status = "delivered"
            item.delivered_at = now
            changed = True
        result.append({"id": item.id, "command": item.command, "payload": item.payload or {}})
    if changed:
        await session.commit()
    return result


async def latest_agent_commands(session: AsyncSession, source_names: list[str]) -> dict[str, AgentCommand]:
    if not source_names:
        return {}
    rows = list(
        await session.scalars(
            select(AgentCommand)
            .where(AgentCommand.source_name.in_(source_names))
            .order_by(AgentCommand.id.desc())
        )
    )
    latest: dict[str, AgentCommand] = {}
    for item in rows:
        latest.setdefault(item.source_name, item)
    return latest


async def list_agent_commands(
    session: AsyncSession,
    *,
    source_name: str,
    limit: int = 30,
) -> list[AgentCommand]:
    return list(
        await session.scalars(
            select(AgentCommand)
            .where(AgentCommand.source_name == source_name)
            .order_by(AgentCommand.id.desc())
            .limit(max(1, min(int(limit), 100)))
        )
    )


async def cancel_agent_command(
    session: AsyncSession,
    *,
    source_name: str,
    command_id: int,
    actor_user_id: int,
) -> AgentCommand | None:
    item = await session.scalar(
        select(AgentCommand).where(
            AgentCommand.id == int(command_id),
            AgentCommand.source_name == source_name,
        )
    )
    if item is None or item.status != "pending":
        return None
    item.status = "cancelled"
    item.completed_at = _now_utc()
    item.result = {"ok": False, "message": "Команда отменена до доставки агенту", "details": {}}
    session.add(
        AdminAction(
            actor_user_id=actor_user_id,
            action="agent_command_cancelled",
            payload=prepare_audit_payload({
                "command_id": item.id,
                "source_name": item.source_name,
                "command": item.command,
                "channel": "api",
                "status": item.status,
            }),
        )
    )
    await session.commit()
    await session.refresh(item)
    return item
