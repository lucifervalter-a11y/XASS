from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.services.notifications import (
    emit_notification,
    list_notifications,
    save_preference,
    set_notification_status,
    unread_notification_count,
)


class NotificationCenterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_notification_lifecycle_and_deduplication(self) -> None:
        async with self.sessions() as session:
            item, policy = await emit_notification(
                session,
                event_type="agent_offline",
                title="PC offline",
                message="Heartbeat missing",
                device="PC",
                requires_action=True,
                dedup_key="offline:pc",
            )
            duplicate, _ = await emit_notification(
                session,
                event_type="agent_offline",
                title="PC offline",
                message="Heartbeat missing",
                device="PC",
                requires_action=True,
                dedup_key="offline:pc",
            )
            self.assertIsNotNone(item)
            self.assertIsNone(duplicate)
            self.assertIn("internal", policy["channels"])
            self.assertEqual(await unread_notification_count(session), 1)
            await set_notification_status(session, item.id, "read")
            self.assertEqual(await unread_notification_count(session), 0)
            self.assertEqual((await list_notifications(session))[0].status, "read")

    async def test_disabled_event_does_not_create_internal_notification(self) -> None:
        async with self.sessions() as session:
            await save_preference(
                session,
                event_type="high_load",
                channels=[],
                priority="low",
                quiet_hours=True,
                actor_user_id=1,
            )
            item, policy = await emit_notification(
                session,
                event_type="high_load",
                title="Load",
                message="CPU",
            )
            self.assertIsNone(item)
            self.assertEqual(policy["channels"], [])


if __name__ == "__main__":
    unittest.main()
