from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import PwaPairToken
from app.services.pwa_pairing import PwaPairingError, consume_pwa_pair_token, issue_pwa_pair_token


class PwaPairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_link_is_one_time_and_plain_token_is_not_stored(self) -> None:
        async with self.sessions() as session:
            issued = await issue_pwa_pair_token(session, actor_user_id=42)
            row = await session.scalar(select(PwaPairToken))
            self.assertIsNotNone(row)
            self.assertNotEqual(row.token_hash, issued.token)
            self.assertNotIn(issued.token, row.token_hint)

            self.assertEqual(await consume_pwa_pair_token(session, issued.token), 42)
            with self.assertRaises(PwaPairingError):
                await consume_pwa_pair_token(session, issued.token)

    async def test_new_link_revokes_previous_link(self) -> None:
        async with self.sessions() as session:
            first = await issue_pwa_pair_token(session, actor_user_id=42)
            second = await issue_pwa_pair_token(session, actor_user_id=42)
            with self.assertRaises(PwaPairingError):
                await consume_pwa_pair_token(session, first.token)
            self.assertEqual(await consume_pwa_pair_token(session, second.token), 42)

    async def test_expired_link_is_rejected(self) -> None:
        async with self.sessions() as session:
            issued = await issue_pwa_pair_token(session, actor_user_id=42)
            await session.execute(
                update(PwaPairToken).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
            await session.commit()
            with self.assertRaises(PwaPairingError):
                await consume_pwa_pair_token(session, issued.token)


if __name__ == "__main__":
    unittest.main()
