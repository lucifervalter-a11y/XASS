from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import AgentStateSnapshot, AppConfig, HeartbeatSource
from app.schemas import HeartbeatPayload
from app.services.heartbeat import is_quiet_hours, process_heartbeat


class HeartbeatServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine("sqlite+aiosqlite:///" + (Path(self.temp.name) / "hb.db").as_posix())
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_recovers_offline_agent_and_writes_snapshot(self):
        async with self.sessions() as session:
            session.add(
                HeartbeatSource(
                    source_name="pc-1",
                    source_type="PC_AGENT",
                    is_online=False,
                    last_payload={},
                )
            )
            await session.commit()

        payload = HeartbeatPayload(source_name="pc-1", metrics={"cpu_percent": 3})
        with patch("app.services.heartbeat.ensure_agent_attached", new=AsyncMock()):
            async with self.sessions() as session:
                source, recovered, is_new = await process_heartbeat(session, payload)

        self.assertFalse(is_new)
        self.assertTrue(recovered)
        self.assertTrue(source.is_online)
        async with self.sessions() as session:
            snapshots = list(await session.scalars(select(AgentStateSnapshot)))
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0].is_online)

    def test_quiet_hours_wraps_midnight(self):
        config = AppConfig(
            quiet_hours_enabled=True,
            quiet_hours_start_minute=22 * 60,
            quiet_hours_end_minute=7 * 60,
        )
        settings = SimpleNamespace(timezone="UTC")
        frozen = datetime(2026, 9, 14, 23, 0, tzinfo=timezone.utc)
        with patch("app.services.heartbeat._now_utc", return_value=frozen):
            self.assertTrue(is_quiet_hours(config, settings))
        frozen = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        with patch("app.services.heartbeat._now_utc", return_value=frozen):
            self.assertFalse(is_quiet_hours(config, settings))


if __name__ == "__main__":
    unittest.main()
