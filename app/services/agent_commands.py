from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAction, AgentCommand
from app.services.agent_lifecycle import ensure_agent_attached
from app.services.app_config import prepare_audit_payload
from app.services.notifications import emit_notification

# A delivery the agent has not answered yet. Fresher than this is still in flight.
DELIVERY_RETRY_AFTER = timedelta(seconds=90)

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
    "music_outputs", "music_play", "music_pause", "music_resume", "music_stop",
    "music_seek", "music_volume", "music_status", "music_storage_sync",
}
DANGEROUS_AGENT_COMMANDS = {
    "lock", "sleep", "reboot", "shutdown", "restart", "update",
    "cleanup_archive", "file_delete", "migration_download",
}
# These commands expose private workstation data or write a user-selected file.
# They need the same fresh, payload-bound approval as destructive actions, but
# remain a separate category for notifications and UI wording.
SENSITIVE_AGENT_COMMANDS = {
    "screenshot", "file_download", "clipboard_get", "file_upload",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _idempotency_digest(source_name: str, command: str, idempotency_key: str | None) -> str | None:
    key = (idempotency_key or "").strip()[:256]
    if not key:
        return None
    return hashlib.sha256(f"{source_name}:{command}:{key}".encode()).hexdigest()


async def enqueue_agent_command(
    session: AsyncSession,
    *,
    source_name: str,
    command: str,
    payload: dict[str, Any] | None,
    actor_user_id: int | None,
    not_before_at: datetime | None = None,
    idempotency_key: str | None = None,
) -> AgentCommand:
    normalized = (command or "").strip().lower()
    if normalized not in ALLOWED_AGENT_COMMANDS:
        raise ValueError(f"Unsupported agent command: {normalized}")
    await ensure_agent_attached(session, source_name)
    digest = _idempotency_digest(source_name, normalized, idempotency_key)
    if digest:
        existing = await session.scalar(select(AgentCommand).where(AgentCommand.idempotency_key == digest))
        if existing is not None:
            await session.commit()
            return existing
    if normalized == "update" and not_before_at is None:
        existing = await session.scalar(
            select(AgentCommand)
            .where(
                AgentCommand.source_name == source_name,
                AgentCommand.command == normalized,
                AgentCommand.status.in_(["pending", "delivered"]),
                AgentCommand.not_before_at.is_(None),
            )
            .order_by(AgentCommand.id.asc())
            .limit(1)
        )
        if existing is not None:
            await session.commit()
            return existing
    item = AgentCommand(
        source_name=source_name,
        command=normalized,
        payload=payload or {},
        status="pending",
        requested_by_user_id=actor_user_id,
        not_before_at=not_before_at,
        idempotency_key=digest,
    )
    session.add(item)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if digest is None:
            raise
        existing = await session.scalar(select(AgentCommand).where(AgentCommand.idempotency_key == digest))
        if existing is None:
            raise
        return existing
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
    await ensure_agent_attached(session, source_name)
    parsed: list[tuple[int, dict[str, Any]]] = []
    for result in results[:50]:
        if not isinstance(result, dict):
            continue
        try:
            parsed.append((int(result.get("id")), result))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return
    rows = list(await session.scalars(
        select(AgentCommand).where(
            AgentCommand.id.in_([command_id for command_id, _ in parsed]),
            AgentCommand.source_name == source_name,
            AgentCommand.status.in_(("delivered", "pending")),
        )
    ))
    by_id = {item.id: item for item in rows}
    completed: list[tuple[str, str, bool, str, int]] = []
    for command_id, result in parsed:
        item = by_id.get(command_id)
        if item is None or item.status not in {"delivered", "pending"}:
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


def _lock_rows(session: AsyncSession, stmt):
    # Postgres can hand the same pending row to two workers. SQLite is serialized
    # by ensure_agent_attached's dummy UPDATE; FOR UPDATE is a no-op there.
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        return stmt.with_for_update(skip_locked=True)
    return stmt


async def deliver_agent_commands(session: AsyncSession, *, source_name: str) -> list[dict[str, Any]]:
    await ensure_agent_attached(session, source_name)
    now = _now_utc()
    due = or_(AgentCommand.not_before_at.is_(None), AgentCommand.not_before_at <= now)
    # Fresh delivered stays with the agent that just received it. Retry only
    # after 90s, or when delivered_at was never recorded.
    stale = or_(AgentCommand.delivered_at.is_(None), AgentCommand.delivered_at < now - DELIVERY_RETRY_AFTER)
    pending = list(
        await session.scalars(
            _lock_rows(session, select(AgentCommand)
            .where(
                AgentCommand.source_name == source_name,
                AgentCommand.status == "pending",
                due,
            )
            .order_by(AgentCommand.id.asc())
            .limit(10))
        )
    )
    # Reserve two slots for retries even when new requests keep arriving. Old
    # unacknowledged commands cannot block new controls, and neither queue starves.
    retries = list(
        await session.scalars(
            _lock_rows(session, select(AgentCommand)
            .where(
                AgentCommand.source_name == source_name,
                AgentCommand.status == "delivered",
                due,
                stale,
            )
            .order_by(
                func.coalesce(AgentCommand.delivered_at, AgentCommand.created_at).asc(),
                AgentCommand.id.asc(),
            )
            .limit(max(2, 10 - len(pending))))
        )
    )
    rows = pending[:10 - len(retries)] + retries
    result: list[dict[str, Any]] = []
    for item in rows:
        if item.status == "pending":
            item.status = "delivered"
        item.delivered_at = now
        item.attempt_count = int(item.attempt_count or 0) + 1
        result.append({
            "id": item.id,
            "command": item.command,
            "payload": item.payload or {},
            "attempt": item.attempt_count,
        })
    # Release the lifecycle lock even for an empty queue, before heartbeat
    # performs archive/manifest work outside this transaction.
    await session.commit()
    return result


def latest_agent_commands_stmt(source_names: list[str]):
    latest_id = (
        select(func.max(AgentCommand.id).label("max_id"))
        .where(AgentCommand.source_name.in_(source_names))
        .group_by(AgentCommand.source_name)
        .subquery()
    )
    return select(AgentCommand).join(latest_id, AgentCommand.id == latest_id.c.max_id)


async def latest_agent_commands(session: AsyncSession, source_names: list[str]) -> dict[str, AgentCommand]:
    if not source_names:
        return {}
    rows = list(await session.scalars(latest_agent_commands_stmt(source_names)))
    return {item.source_name: item for item in rows}


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
