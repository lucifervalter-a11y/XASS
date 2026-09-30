from __future__ import annotations

import unittest

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.enums import SourceType
from app.models import AgentCredential
from app.services.agent_pairing import (
    DEFAULT_AGENT_API_KEY,
    PAIR_CODE_ALPHABET,
    PairingError,
    authenticate_agent_api_key,
    claim_pair_code_and_issue_key,
    issue_pair_code,
    require_explicit_global_agent_key,
)


class AgentPairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def test_pair_alphabet_excludes_ambiguous_characters(self) -> None:
        self.assertTrue(set("0O1I").isdisjoint(PAIR_CODE_ALPHABET))

    async def test_one_pair_code_issues_one_key(self) -> None:
        async with self.sessions() as session:
            issued = await issue_pair_code(session, actor_user_id=7)
            first = await claim_pair_code_and_issue_key(
                session, pair_code=issued.code, source_name="PC", source_type=SourceType.PC_AGENT,
            )
            with self.assertRaises(PairingError):
                await claim_pair_code_and_issue_key(
                    session, pair_code=issued.code, source_name="PC", source_type=SourceType.PC_AGENT,
                )
            count = await session.scalar(select(func.count()).select_from(AgentCredential))
            self.assertEqual(count, 1)
            self.assertTrue(first.agent_api_key.startswith("ag_"))
            self.assertIsNotNone(
                await authenticate_agent_api_key(
                    session, api_key=first.agent_api_key, global_agent_api_key=DEFAULT_AGENT_API_KEY,
                )
            )

    async def test_default_global_key_never_authenticates(self) -> None:
        async with self.sessions() as session:
            self.assertIsNone(await authenticate_agent_api_key(
                session, api_key=DEFAULT_AGENT_API_KEY, global_agent_api_key=DEFAULT_AGENT_API_KEY, global_key_enabled=True,
            ))
            self.assertIsNone(await authenticate_agent_api_key(
                session, api_key="real-shared-key", global_agent_api_key="real-shared-key", global_key_enabled=False,
            ))
            auth = await authenticate_agent_api_key(
                session, api_key="real-shared-key", global_agent_api_key="real-shared-key", global_key_enabled=True,
            )
            self.assertIsNotNone(auth)
            self.assertEqual(auth.mode, "global")

    def test_enabled_default_global_key_refuses_startup(self) -> None:
        require_explicit_global_agent_key("real-shared-key", enabled=True)
        require_explicit_global_agent_key(DEFAULT_AGENT_API_KEY, enabled=False)
        with self.assertRaises(RuntimeError):
            require_explicit_global_agent_key(DEFAULT_AGENT_API_KEY, enabled=True)
        with self.assertRaises(RuntimeError):
            require_explicit_global_agent_key("", enabled=True)


if __name__ == "__main__":
    unittest.main()
