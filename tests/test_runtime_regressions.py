"""Regression coverage for test-branch runtime failures; no live services."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot_api import TelegramBotClient
from app.config import Settings
from app.db import Base
from app import poller
from app.services import rules_engine
from app.services.notifications import emit_notification, list_notifications
from pc_client import archive_store


class TelegramTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.timeouts = []

        def respond(request):
            self.timeouts.append(request.extensions["timeout"])
            if "/file/" in request.url.path:
                return httpx.Response(200, content=b"fixture")
            return httpx.Response(200, json={"ok": True, "result": []})

        self.bot = TelegramBotClient("fixture-token")
        await self.bot.client.aclose()
        self.bot.client = httpx.AsyncClient(timeout=20, transport=httpx.MockTransport(respond))

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_api_requests_inherit_client_timeout(self):
        await self.bot.get_me()
        await self.bot.send_message(1, "fixture")
        self.assertEqual(self.timeouts, [dict.fromkeys(("connect", "read", "write", "pool"), 20)] * 2)

    async def test_file_download_inherits_client_timeout(self):
        data, _ = await self.bot.download_file_bytes("fixture.bin", max_bytes=100)
        self.assertEqual(data, b"fixture")
        self.assertEqual(self.timeouts, [dict.fromkeys(("connect", "read", "write", "pool"), 20)])

    async def test_long_poll_keeps_its_explicit_transport_timeout(self):
        await self.bot.get_updates(timeout=25)
        self.assertEqual(self.timeouts, [{"connect": 10.0, "read": 40.0, "write": 40.0, "pool": 40.0}])

    async def test_explicit_api_and_download_timeouts_are_preserved(self):
        await self.bot._request("getMe", timeout=3.0)
        await self.bot.download_file_bytes("fixture.bin", max_bytes=100, timeout=httpx.Timeout(7, connect=2))
        self.assertEqual(self.timeouts, [dict.fromkeys(("connect", "read", "write", "pool"), 3.0),
                                         {"connect": 2, "read": 7, "write": 7, "pool": 7}])


class PollingOffsetTests(unittest.IsolatedAsyncioTestCase):
    async def run_poll(self, *, fail_update=None, fail_exit=False):
        stop = asyncio.Event()
        calls, handled = [], []
        updates = [{"update_id": 10}, {"update_id": 11}, {"update_id": 12}]
        failed = False

        async def get_updates(**kwargs):
            calls.append(kwargs["offset"])
            if len(calls) == 3:
                stop.set()
                return []
            # Simulate Telegram's acknowledgment semantics: higher offsets
            # discard all previous updates, including a prematurely acked one.
            return [u for u in updates if kwargs["offset"] is None or u["update_id"] >= kwargs["offset"]]

        async def handle_update(session, update):
            nonlocal failed
            handled.append(update["update_id"])
            if update["update_id"] == fail_update and not failed:
                failed = True
                raise RuntimeError("transient fixture failure")

        @asynccontextmanager
        async def sessions():
            nonlocal failed
            yield object()
            if fail_exit and not failed:
                failed = True
                raise RuntimeError("session exit failure")

        settings = SimpleNamespace(polling_drop_pending_updates=False, polling_request_timeout_sec=25,
                                   polling_retry_delay_sec=0.001)
        bot = SimpleNamespace(delete_webhook=AsyncMock(), get_updates=get_updates)
        handler = SimpleNamespace(handle_update=handle_update)
        with patch.object(poller, "SessionLocal", sessions), patch.object(poller, "logger"):
            await asyncio.wait_for(poller.telegram_polling_loop(settings, bot, handler, stop), timeout=2)
        return calls, handled

    async def test_first_failed_update_is_retried_without_acknowledging_it(self):
        calls, handled = await self.run_poll(fail_update=10)
        self.assertEqual(calls, [None, None, 13])
        self.assertEqual(handled, [10, 10, 11, 12])

    async def test_batch_keeps_successful_prefix_but_retries_failed_update(self):
        calls, handled = await self.run_poll(fail_update=11)
        self.assertEqual(calls, [None, 11, 13])
        self.assertEqual(handled, [10, 11, 11, 12])

    async def test_session_exit_failure_does_not_acknowledge_update(self):
        calls, handled = await self.run_poll(fail_exit=True)
        self.assertEqual(calls, [None, None, 13])
        self.assertEqual(handled, [10, 10, 11, 12])

    async def test_successful_batch_advances_past_last_update(self):
        calls, handled = await self.run_poll()
        self.assertEqual(calls, [None, 13, 13])
        self.assertEqual(handled, [10, 11, 12])


class ArchiveQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"archive_folder": self.temp.name}
        self.connection = archive_store._connect(self.root)
        self.addCleanup(self.connection.close)

    def media(self, asset_id, name, *, size=10, age_days=0):
        target = self.root / "media" / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b"x" * size)
        saved_at = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
        self.connection.execute(
            "INSERT INTO media(asset_id,message_id,local_path,saved_at,saved) VALUES(?,?,?,?,1)",
            (asset_id, asset_id, str(target), saved_at))
        self.connection.commit()
        return target

    def saved_ids(self):
        return [row[0] for row in self.connection.execute("SELECT asset_id FROM media WHERE saved=1 ORDER BY asset_id")]

    def test_shared_file_under_quota_is_not_removed(self):
        file = self.media(1, "shared.bin")
        self.media(2, "shared.bin")
        self.config["archive_max_gb"] = 15 / 1024**3
        self.assertEqual(archive_store.cleanup_archive(self.config), {"removed_files": 0, "freed_bytes": 0})
        self.assertTrue(file.is_file())
        self.assertEqual(self.saved_ids(), [1, 2])

    def test_removing_shared_file_stops_at_actual_quota(self):
        shared = self.media(1, "shared.bin", age_days=10)
        self.media(2, "shared.bin", age_days=10)
        recent = self.media(3, "recent.bin")
        self.config["archive_max_gb"] = 10 / 1024**3
        self.assertEqual(archive_store.cleanup_archive(self.config), {"removed_files": 1, "freed_bytes": 10})
        self.assertFalse(shared.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(self.saved_ids(), [3])

    def test_expired_reference_keeps_file_used_by_fresh_reference(self):
        file = self.media(1, "shared.bin", age_days=10)
        self.media(2, "shared.bin")
        self.config["archive_retention_days"] = 5
        self.assertEqual(archive_store.cleanup_archive(self.config), {"removed_files": 0, "freed_bytes": 0})
        self.assertTrue(file.is_file())
        self.assertEqual(self.saved_ids(), [2])

    def test_force_cleanup_reports_unique_file_count_and_bytes(self):
        self.media(1, "shared.bin")
        self.media(2, "shared.bin")
        self.media(3, "other.bin", size=7)
        self.assertEqual(archive_store.cleanup_archive(self.config, force=True), {"removed_files": 2, "freed_bytes": 17})
        self.assertEqual(self.saved_ids(), [])


class ServiceRuleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        rules_engine._active_since.clear()
        rules_engine._last_triggered.clear()

    async def asyncTearDown(self):
        rules_engine._active_since.clear()
        rules_engine._last_triggered.clear()
        await self.engine.dispose()

    async def test_service_down_creates_notification_without_device_and_obeys_cooldown(self):
        rule = {"id": "svc", "name": "Service down", "condition": "service_down", "service": "fixture-service",
                "priority": "warning", "cooldown_minutes": 60, "duration_minutes": 0}
        async with self.sessions() as session:
            with patch.object(rules_engine, "load_rules", return_value=[rule]), \
                    patch.object(rules_engine, "get_agent_installer", return_value=None):
                first = await rules_engine.evaluate_rules(session, Settings(_env_file=None), sources=[], services={"fixture-service": "failed"})
                second = await rules_engine.evaluate_rules(session, Settings(_env_file=None), sources=[], services={"fixture-service": "failed"})
            rows = await list_notifications(session)
            self.assertEqual(first, ["svc:fixture-service"])
            self.assertEqual(second, [])
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0].device)
            self.assertEqual(rows[0].details["target"], "fixture-service")

    async def test_none_device_is_normalized_at_notification_boundary(self):
        async with self.sessions() as session:
            item, _ = await emit_notification(session, event_type="automation_rule", title="fixture", message="fixture", device=None)
            self.assertIsNone(item.device)

    async def test_active_service_does_not_notify(self):
        rule = {"id": "svc", "name": "Service down", "condition": "service_down", "service": "fixture-service", "priority": "warning"}
        async with self.sessions() as session:
            with patch.object(rules_engine, "load_rules", return_value=[rule]), \
                    patch.object(rules_engine, "get_agent_installer", return_value=None):
                result = await rules_engine.evaluate_rules(session, Settings(_env_file=None), sources=[], services={"fixture-service": "active"})
            self.assertEqual(result, [])
            self.assertEqual(await list_notifications(session), [])


if __name__ == "__main__":
    unittest.main()
