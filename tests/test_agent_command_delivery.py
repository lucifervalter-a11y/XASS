from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import AgentCommand
from app.services.agent_commands import acknowledge_agent_commands, deliver_agent_commands, enqueue_agent_command


class AgentCommandDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_new_command_bypasses_orphaned_delivery_batch_and_retries_rotate(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        async with self.sessions() as session:
            stale = [AgentCommand(source_name="PC", command="ping", status="delivered", created_at=old, delivered_at=old) for _ in range(20)]
            fresh = AgentCommand(source_name="PC", command="ping", status="pending")
            other_pc = AgentCommand(source_name="Other PC", command="ping", status="pending")
            scheduled = AgentCommand(source_name="PC", command="ping", status="pending", not_before_at=old + timedelta(hours=2))
            session.add_all([*stale, fresh, other_pc, scheduled])
            await session.commit()
            first = await deliver_agent_commands(session, source_name="PC")
            second = await deliver_agent_commands(session, source_name="PC")
            self.assertEqual(first[0]["id"], fresh.id)
            self.assertEqual(len(first), 10)
            self.assertTrue(set(row["id"] for row in first).isdisjoint(row["id"] for row in second))
            self.assertNotIn(other_pc.id, [row["id"] for row in first + second])
            self.assertNotIn(scheduled.id, [row["id"] for row in first + second])

    async def test_repeated_immediate_update_reuses_pending_command_but_not_scheduled_update(self) -> None:
        async with self.sessions() as session:
            first = await enqueue_agent_command(session, source_name="PC", command="update", payload={}, actor_user_id=42)
            second = await enqueue_agent_command(session, source_name="PC", command="update", payload={}, actor_user_id=42)
            self.assertEqual(first.id, second.id)
            await deliver_agent_commands(session, source_name="PC")
            third = await enqueue_agent_command(session, source_name="PC", command="update", payload={}, actor_user_id=42)
            self.assertEqual(first.id, third.id)
            scheduled = await enqueue_agent_command(
                session, source_name="PC", command="update", payload={}, actor_user_id=42,
                not_before_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            self.assertNotEqual(first.id, scheduled.id)

    async def test_busy_new_queue_still_retries_unacknowledged_commands(self) -> None:
        async with self.sessions() as session:
            retries = [AgentCommand(source_name="PC", command="ping", status="delivered") for _ in range(3)]
            pending = [AgentCommand(source_name="PC", command="ping", status="pending") for _ in range(20)]
            session.add_all([*retries, *pending])
            await session.commit()
            batch = await deliver_agent_commands(session, source_name="PC")
            ids = [row["id"] for row in batch]
            self.assertEqual(ids[:8], [row.id for row in pending[:8]])
            self.assertEqual(ids[8:], [row.id for row in retries[:2]])

    async def test_late_result_does_not_revive_cancelled_command(self) -> None:
        async with self.sessions() as session:
            item = AgentCommand(source_name="PC", command="lock", status="cancelled", result={"ok": False, "message": "Отменено"})
            session.add(item)
            await session.commit()
            with patch("app.services.agent_commands.emit_notification", new_callable=AsyncMock) as notify:
                await acknowledge_agent_commands(session, source_name="PC", results=[{"id": item.id, "ok": True}])
            self.assertEqual(item.status, "cancelled")
            self.assertEqual(item.result["message"], "Отменено")
            notify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
