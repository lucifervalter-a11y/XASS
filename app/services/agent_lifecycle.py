"""Unpair a source, preserving user archives and a permanent credential tombstone."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAction, AgentArchiveTarget, AgentCommand, AgentCredential, HeartbeatSource


class AgentDetachedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Агент отвязан. Для подключения нужен новый код привязки.")


async def _lock_source(session: AsyncSession, source_name: str) -> None:
    # A no-op UPDATE serializes with detach on SQLite as well as PostgreSQL.
    # SELECT FOR UPDATE alone does not protect SQLite. Keep timestamps untouched.
    await session.execute(
        update(HeartbeatSource).where(HeartbeatSource.source_name == source_name)
        .values(id=HeartbeatSource.id, updated_at=HeartbeatSource.updated_at)
        .execution_options(synchronize_session=False)
    )


async def ensure_agent_attached(session: AsyncSession, source_name: str, *, lock: bool = True) -> None:
    if lock:
        await _lock_source(session, source_name)
    active = await session.scalar(
        select(AgentCredential.is_active).where(AgentCredential.source_name == source_name)
    )
    if active is False:
        raise AgentDetachedError()


@dataclass(frozen=True)
class DetachResult:
    source: HeartbeatSource | None
    cancelled_commands: int = 0


async def detach_agent(
    session: AsyncSession, *, source_name: str, source_id: int, actor_user_id: int | None = None,
) -> DetachResult:
    await _lock_source(session, source_name)
    source = await session.scalar(
        select(HeartbeatSource).where(HeartbeatSource.source_name == source_name)
        .execution_options(populate_existing=True)
    )
    credential = await session.scalar(
        select(AgentCredential).where(AgentCredential.source_name == source_name)
        .execution_options(populate_existing=True)
    )
    if source is None:
        if credential is not None and not credential.is_active:
            return DetachResult(source=None)
        raise LookupError("Агент не найден. Обновите список устройств.")
    if source.id != source_id:
        raise ValueError("Подключение агента изменилось. Обновите список и подтвердите отвязку заново.")

    now = datetime.now(timezone.utc)
    if credential is None:
        # Legacy shared keys cannot be revoked per device. Reserve this source
        # name with an unusable credential, so its next heartbeat cannot recreate it.
        credential = AgentCredential(
            source_name=source_name, source_type=source.source_type,
            api_key_hash=secrets.token_hex(32), key_hint="revoked", is_active=False,
            created_by_user_id=actor_user_id,
        )
        session.add(credential)
    else:
        credential.is_active = False
        credential.updated_at = now
    cancelled = await session.execute(
        update(AgentCommand).where(
            AgentCommand.source_name == source_name, AgentCommand.status.in_(["pending", "delivered"]),
        ).values(
            status="cancelled", completed_at=now,
            result={"ok": False, "message": "Агент отвязан владельцем", "details": {"reason": "agent_detached"}},
        )
    )
    await session.execute(
        update(AgentArchiveTarget).where(AgentArchiveTarget.source_name == source_name)
        .values(enabled=False, updated_at=now, updated_by_user_id=actor_user_id)
    )
    count = int(cancelled.rowcount or 0)
    if actor_user_id:
        session.add(AdminAction(actor_user_id=actor_user_id, action="detach_agent", payload={
            "source_id": source.id, "source_name": source_name,
            "cancelled_commands": count, "archives_preserved": True,
        }))
    await session.delete(source)
    await session.commit()
    return DetachResult(source=source, cancelled_commands=count)
