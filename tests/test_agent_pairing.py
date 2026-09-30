from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.enums import SourceType
from app.models import AgentPairCode
from app.services.agent_pairing import PairingError, claim_pair_code_and_issue_key, issue_pair_code


class AgentPairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine("sqlite+aiosqlite:///" + (Path(self.temp.name) / "pair.db").as_posix())
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_one_time_code_cannot_be_reused(self):
        async with self.sessions() as session:
            issued = await issue_pair_code(session, actor_user_id=1, ttl_minutes=15, code_length=8)
        async with self.sessions() as session:
            first = await claim_pair_code_and_issue_key(
                session, pair_code=issued.code, source_name="desk", source_type=SourceType.PC_AGENT
            )
        self.assertTrue(first.agent_api_key.startswith("ag_"))
        async with self.sessions() as session:
            with self.assertRaises(PairingError):
                await claim_pair_code_and_issue_key(
                    session, pair_code=issued.code, source_name="desk-2", source_type=SourceType.PC_AGENT
                )

    async def test_expired_code_is_rejected(self):
        from sqlalchemy import select

        async with self.sessions() as session:
            issued = await issue_pair_code(session, actor_user_id=1, ttl_minutes=15, code_length=8)
        async with self.sessions() as session:
            pair = await session.scalar(select(AgentPairCode))
            pair.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await session.commit()
        async with self.sessions() as session:
            with self.assertRaises(PairingError):
                await claim_pair_code_and_issue_key(
                    session, pair_code=issued.code, source_name="desk", source_type=SourceType.PC_AGENT
                )


if __name__ == "__main__":
    unittest.main()
