from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentCommand

ALLOWED_AGENT_COMMANDS = {"update", "restart", "lock"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue_agent_command(
    session: AsyncSession,
    *,
    source_name: str,
    command: str,
    payload: dict[str, Any] | None,
    actor_user_id: int | None,
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
        changed = True
    if changed:
        await session.commit()


async def deliver_agent_commands(session: AsyncSession, *, source_name: str) -> list[dict[str, Any]]:
    rows = list(
        await session.scalars(
            select(AgentCommand)
            .where(
                AgentCommand.source_name == source_name,
                AgentCommand.status.in_(["pending", "delivered"]),
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
