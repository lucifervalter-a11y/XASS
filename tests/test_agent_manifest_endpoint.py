from __future__ import annotations

import hashlib
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.db import Base
from app.models import AgentCredential


class AgentManifestEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add_all([
                AgentCredential(source_name="PC", api_key_hash=hashlib.sha256(b"issued-key").hexdigest(), key_hint="test", is_active=True),
                AgentCredential(source_name="Revoked PC", api_key_hash=hashlib.sha256(b"revoked-key").hexdigest(), key_hint="test", is_active=False),
            ])
            await session.commit()

        async def get_session():
            async with self.sessions() as session:
                yield session

        self.context = ExitStack()
        self.context.enter_context(patch.dict(main.app.dependency_overrides, {main.get_session: get_session}))
        self.context.enter_context(patch.object(main, "settings", SimpleNamespace(agent_api_key="global-key", profile_public_url="https://fallback.example/profile")))
        self.config = SimpleNamespace(service_base_url="https://public.example/miniapp.php")
        self.context.enter_context(patch.object(main, "get_or_create_app_config", new=AsyncMock(return_value=self.config)))
        self.source = self.context.enter_context(patch.object(main, "build_update_manifest", return_value={"available": True, "distribution": "source"}))
        self.installer = self.context.enter_context(patch.object(main, "build_installer_manifest", return_value={"available": False, "distribution": "installer"}))
        self.deliver = self.context.enter_context(patch.object(main, "deliver_agent_commands", new=AsyncMock()))
        self.acknowledge = self.context.enter_context(patch.object(main, "acknowledge_agent_commands", new=AsyncMock()))
        self.heartbeat = self.context.enter_context(patch.object(main, "process_heartbeat", new=AsyncMock()))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://backend.internal:8001")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.context.close()
        await self.engine.dispose()

    async def test_missing_invalid_and_revoked_keys_are_rejected_before_manifest_work(self) -> None:
        for key in (None, "wrong-key", "revoked-key"):
            with self.subTest(key=key):
                response = await self.client.get("/agent/update-manifest", headers={"X-Api-Key": key} if key else {})
                self.assertEqual(response.status_code, 401)
        self.source.assert_not_called()
        self.installer.assert_not_called()

    async def test_issued_key_gets_source_manifest_without_command_or_heartbeat_side_effects(self) -> None:
        response = await self.client.get(
            "/agent/update-manifest",
            params={"agent_version": "0.13.3", "agent_revision": "local-revision", "agent_distribution": "source"},
            headers={"X-Api-Key": "issued-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["vary"].lower(), "x-api-key")
        self.assertEqual(response.json()["update"]["distribution"], "source")
        self.assertIsNone(response.json()["installer_update"])
        self.assertEqual(self.source.call_args.kwargs, {
            "api_key": "issued-key", "base_url": "https://public.example",
            "current_version": "0.13.3", "current_revision": "local-revision",
        })
        self.installer.assert_not_called()
        self.deliver.assert_not_awaited()
        self.acknowledge.assert_not_awaited()
        self.heartbeat.assert_not_awaited()

    async def test_installer_distribution_uses_its_builder_and_public_fallback(self) -> None:
        self.config.service_base_url = ""
        response = await self.client.get("/agent/update-manifest", params={"agent_distribution": "installer"}, headers={"X-Api-Key": "global-key"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["update"])
        self.assertEqual(response.json()["installer_update"]["distribution"], "installer")
        self.assertEqual(self.installer.call_args.kwargs["base_url"], "https://fallback.example")
        self.assertEqual(self.installer.call_args.kwargs["api_key"], "global-key")
        self.source.assert_not_called()

    async def test_invalid_distribution_and_unbounded_versions_are_rejected(self) -> None:
        for query in ({"agent_distribution": "unknown"}, {"agent_version": "x" * 129}, {"agent_revision": "x" * 129}):
            with self.subTest(query=query):
                response = await self.client.get("/agent/update-manifest", params=query, headers={"X-Api-Key": "global-key"})
                self.assertEqual(response.status_code, 400)
        self.source.assert_not_called()
        self.installer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
