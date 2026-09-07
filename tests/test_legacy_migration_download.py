from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.db import Base
from app.models import AgentCommand, AgentCredential


class LegacyMigrationDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.filename = "xass-app-20260908.tar.zst"
        (self.root / self.filename).write_bytes(b"PRIVATE_SERVER_ARCHIVE")
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[AgentCredential.__table__, AgentCommand.__table__]))
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.addAsyncCleanup(self.session.close)
        self.settings_patch = patch.object(main, "settings", SimpleNamespace(agent_api_key="shared-legacy-key", owner_user_id=42, agent_migration_export_dir=str(self.root)))
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        for source, key in (("ПК владельца", "owner-pc-key"), ("Other PC", "other-pc-key")):
            self.session.add(AgentCredential(source_name=source, source_type="PC_AGENT", api_key_hash=hashlib.sha256(key.encode()).hexdigest(), key_hint="fixture", is_active=True))
        await self.session.commit()

    async def authorize(self, *, source="ПК владельца", filename=None, status="delivered", owner=42, age=0):
        command = AgentCommand(source_name=source, command="migration_download", status=status, payload={"filename": filename or self.filename}, requested_by_user_id=owner, delivered_at=datetime.now(timezone.utc) - timedelta(seconds=age))
        self.session.add(command)
        await self.session.commit()
        return command

    async def download(self, *, key="owner-pc-key", source="", header=None, filename=None):
        return await main.agent_migration_export_download(filename or self.filename, source_name=source, session=self.session, x_api_key=key, x_xass_source=header)

    async def denied(self, **kwargs):
        with self.assertRaises(HTTPException) as raised:
            await self.download(**kwargs)
        self.assertEqual(raised.exception.status_code, 403)

    async def test_agent_key_alone_and_shared_key_never_grant_full_backup(self):
        await self.denied()
        await self.authorize()
        await self.denied(key="shared-legacy-key", source="ПК владельца")
        await self.denied(key="other-pc-key", source="ПК владельца")
        await self.denied(key="other-pc-key")

    async def test_owner_delivered_command_allows_matching_individual_recipient(self):
        await self.authorize()
        response = await self.download(source="ПК владельца")
        self.assertEqual(Path(response.path).read_bytes(), b"PRIVATE_SERVER_ARCHIVE")
        self.assertIn("no-store", response.headers["cache-control"])
        # Legacy ASCII-header transport remains supported for issued credentials.
        await self.authorize(source="Other PC")
        response = await self.download(key="other-pc-key", header="Other PC")
        self.assertEqual(Path(response.path).read_bytes(), b"PRIVATE_SERVER_ARCHIVE")
        await self.denied(source="Other PC", header="ПК владельца")

    async def test_pending_completed_wrong_owner_filename_and_expired_commands_fail(self):
        for status, owner, filename, age in [
            ("pending", 42, self.filename, 0),
            ("completed", 42, self.filename, 0),
            ("delivered", 7, self.filename, 0),
            ("delivered", 42, "xass-system-20260908.tar.zst", 0),
            ("delivered", 42, self.filename, 3700),
        ]:
            with self.subTest(status=status, owner=owner, filename=filename, age=age):
                await self.authorize(status=status, owner=owner, filename=filename, age=age)
                await self.denied()

    async def test_acknowledgement_closes_access_and_invalid_key_is_rejected(self):
        command = await self.authorize()
        await self.download()
        command.status = "completed"
        await self.session.commit()
        await self.denied()
        with self.assertRaises(HTTPException) as raised:
            await self.download(key="invalid-key")
        self.assertEqual(raised.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
