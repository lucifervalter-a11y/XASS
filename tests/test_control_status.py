from __future__ import annotations

import asyncio
import json
import socket
import ssl
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import HeartbeatSource, InternalNotification
from app.services import control_status
from app.services.control_status import (
    PublicSiteProbe, agent_attention, canonical_web_app_url, normalize_site_url,
    source_is_online, summarize_control_status,
)
from app.services.notifications import notification_review_groups, unread_notification_count


class PublicSiteStatusTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        real_client = httpx.AsyncClient
        return patch.object(control_status.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(handler)
        ))

    async def test_bare_production_hostname_is_normalized_to_https(self) -> None:
        requests = []

        def respond(request):
            requests.append(str(request.url))
            return httpx.Response(200)

        with self.client(respond):
            result = await PublicSiteProbe()("redvps.site")
        self.assertEqual(requests, ["https://redvps.site/"])
        self.assertEqual(result["status"], "online")
        self.assertIs(result["available"], True)

    async def test_preserves_configured_route_but_does_not_expose_query(self) -> None:
        with self.client(lambda request: httpx.Response(200)):
            result = await PublicSiteProbe()("https://xass.example/about?token=test-private")
        self.assertEqual(result["url"], "https://xass.example/about")
        self.assertNotIn("test-private", json.dumps(result))

    async def test_real_http_failures_are_not_reported_online(self) -> None:
        for code in (401, 403, 404, 500, 503):
            with self.subTest(code=code), self.client(lambda request: httpx.Response(code)):
                result = await PublicSiteProbe()("xass.example")
            self.assertIs(result["available"], False)
            self.assertEqual(result["http_status"], code)
            self.assertEqual(result["status"], "error")

    async def test_dns_failure_is_unverified_not_false_offline(self) -> None:
        def fail(request):
            try:
                raise socket.gaierror(socket.EAI_AGAIN, "name resolution")
            except socket.gaierror as exc:
                raise httpx.ConnectError("private connection context", request=request) from exc

        with self.client(fail):
            result = await PublicSiteProbe()("xass.example")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "dns_error")
        self.assertIsNone(result["available"])
        self.assertNotIn("private connection context", json.dumps(result))

    async def test_timeout_is_unknown_and_actual_refusal_is_offline(self) -> None:
        for error, state, available in ((httpx.ReadTimeout("timeout"), "unknown", None),
                                        (ConnectionRefusedError(), "offline", False)):
            def fail(request):
                raise error
            with self.subTest(error=type(error).__name__), self.client(fail):
                result = await PublicSiteProbe()("xass.example")
            self.assertEqual(result["status"], state)
            self.assertIs(result["available"], available)

    async def test_bad_certificate_remains_a_real_failure(self) -> None:
        def fail(request):
            raise httpx.ConnectError("TLS failed") from ssl.SSLCertVerificationError("bad certificate")
        with self.client(fail):
            result = await PublicSiteProbe()("xass.example")
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "tls_error")

    async def test_invalid_or_missing_config_does_not_issue_network_requests(self) -> None:
        with patch.object(control_status.httpx, "AsyncClient") as client:
            for raw in ("https://user:password@xass.example", "https://@xass.example", "https://:@xass.example",
                        "file:///etc/passwd", "https://xass.example:999999", "not a hostname"):
                with self.subTest(raw=raw):
                    result = await PublicSiteProbe()(raw)
                    self.assertEqual(result["status"], "invalid_config")
                    self.assertIs(result["available"], False)
                    self.assertEqual(result["url"], "")
            result = await PublicSiteProbe()("")
            self.assertEqual(result["status"], "not_configured")
            self.assertIsNone(result["available"])
            client.assert_not_called()

    async def test_concurrent_bootstraps_share_one_probe_and_cached_result(self) -> None:
        calls = 0
        async def respond(request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return httpx.Response(200)
        probe = PublicSiteProbe()
        with self.client(respond):
            results = await asyncio.gather(*(probe("xass.example") for _ in range(4)))
            cached = await probe("xass.example")
        self.assertEqual(calls, 1)
        self.assertTrue(all(item["available"] for item in results))
        self.assertTrue(cached["cached"])
        cached["available"] = False
        self.assertIs((await probe("xass.example"))["available"], True)

    async def test_failure_cache_expires_and_recovery_is_shown(self) -> None:
        probe = PublicSiteProbe()
        with self.client(lambda request: httpx.Response(503)):
            self.assertIs((await probe("xass.example"))["available"], False)
        key = normalize_site_url("xass.example")
        probe._cache[key] = (0, probe._cache[key][1])
        with self.client(lambda request: httpx.Response(200)):
            self.assertIs((await probe("xass.example"))["available"], True)


class CurrentStatusSummaryTests(unittest.TestCase):
    def system(self):
        return {key: {"status": "online", "available": True} for key in control_status.CORE_SERVICES}

    def test_review_history_and_updates_do_not_reduce_service_health(self) -> None:
        health, attention = summarize_control_status(
            self.system(), [{"source_name": "PC", "requires_update": True, "attention_reasons": ["update_available"]}],
            review_groups=[{"event_type": "auth_failed", "device": "", "count": 4159}],
        )
        self.assertEqual(health["score"], 100)
        self.assertEqual(attention["active_count"], 0)
        self.assertEqual(attention["review_count"], 1)
        self.assertEqual(attention["update_count"], 1)

    def test_unknown_website_never_produces_fake_100_percent(self) -> None:
        system = self.system()
        system["public_site"] = {"status": "unknown", "available": None, "reason": "dns_error"}
        health, attention = summarize_control_status(system, [])
        self.assertIsNone(health["score"])
        self.assertEqual(health["checked_services"], 3)
        self.assertEqual(health["unknown_services"], 1)
        self.assertEqual(health["status"], "unknown")
        self.assertEqual(attention["active_count"], 0)

    def test_real_service_device_and_resource_problems_remain_grouped_visible(self) -> None:
        system = self.system()
        system["public_site"] = {"available": False, "status": "error", "reason": "http_error"}
        health, attention = summarize_control_status(
            system, [{"source_name": "PC", "attention_reasons": ["high_cpu", "high_ram", "archive_error"]}],
            metrics={"cpu_percent": 99, "disk_used_percent": 97},
        )
        self.assertEqual(health["score"], 75)
        self.assertEqual(attention["active_count"], 3)
        self.assertEqual(attention["affected_sources"], 1)
        self.assertEqual(attention["active_issues"][1]["reasons"], ["high_cpu", "high_ram", "archive_error"])

    def test_stale_online_flag_cannot_hide_missing_heartbeats(self) -> None:
        now = datetime.now(timezone.utc)
        source = SimpleNamespace(is_online=True, last_seen_at=now - timedelta(minutes=10))
        self.assertFalse(source_is_online(source, 5, now=now))
        source.last_seen_at = (now - timedelta(seconds=15)).replace(tzinfo=None)
        self.assertTrue(source_is_online(source, 5, now=now))
        source.is_online = False
        self.assertFalse(source_is_online(source, 5, now=now))

    def test_disconnected_device_does_not_claim_old_cpu_as_current_load(self) -> None:
        reasons, update = agent_attention({"agent_version": "0.14.0", "metrics": {"cpu_percent": 100}, "last_error": "old"},
                                          is_online=False, latest_version="0.14.0")
        self.assertEqual(reasons, ["offline"])
        self.assertFalse(update)

    def test_newer_client_is_not_incorrectly_told_to_downgrade(self) -> None:
        reasons, update = agent_attention({"agent_version": "0.15.0"}, is_online=True, latest_version="0.14.0")
        self.assertEqual(reasons, [])
        self.assertFalse(update)

    def test_canonical_miniapp_url_prefers_saved_public_origin(self) -> None:
        self.assertEqual(canonical_web_app_url("https://new.example/profile.php?key=private", "old.example", "http://old.example:8000"),
                         "https://new.example/miniapp.php?standalone=1")
        self.assertEqual(canonical_web_app_url("invalid url", "redvps.site"), "https://redvps.site/miniapp.php?standalone=1")
        self.assertEqual(canonical_web_app_url("javascript:alert(1)", "file:///tmp/x"), "")


class NotificationReviewSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_4159_old_state_events_stay_in_inbox_not_active_attention(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=60)
        async with self.sessions() as session:
            await session.execute(insert(InternalNotification), [
                {"event_type": "agent_offline", "title": "Past offline", "message": "Historical transition",
                 "device": "PC", "requires_action": True, "status": "action", "created_at": old}
                for _ in range(4159)
            ])
            await session.commit()
            reviews = await notification_review_groups(session, source_names=["PC"])
            self.assertEqual(reviews, [])
            self.assertEqual(await unread_notification_count(session), 4159)
            self.assertEqual(await session.scalar(select(func.count(InternalNotification.id))), 4159)

    async def test_explicit_unresolved_reviews_are_grouped_not_expired_or_silenced(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=120)
        async with self.sessions() as session:
            for event_type, device, count in (("auth_failed", None, 50), ("dangerous_command", "PC", 2),
                                               ("archive_error", None, 1), ("media_error", "PC", 1)):
                await session.execute(insert(InternalNotification), [
                    {"event_type": event_type, "title": "Review", "message": "Review required", "device": device,
                     "requires_action": True, "status": "action", "created_at": old} for _ in range(count)
                ])
            session.add(InternalNotification(event_type="new_login", title="Info", message="Info", requires_action=False, status="new"))
            session.add(InternalNotification(event_type="auth_failed", title="Read", message="Read", requires_action=True, status="read"))
            await session.commit()
            groups = await notification_review_groups(session, source_names=["PC"])
            self.assertEqual(len(groups), 4)
            self.assertEqual(next(item["count"] for item in groups if item["event_type"] == "auth_failed"), 50)
            self.assertEqual(await unread_notification_count(session), 55)

    async def test_bootstrap_uses_current_state_summary_without_mutating_inbox_or_source(self) -> None:
        import app.main as main
        from app.config import Settings

        settings = Settings(_env_file=None, owner_user_id=42, profile_public_url="xass.example")
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            session.add_all([
                HeartbeatSource(source_name="fresh", source_type="PC_AGENT", is_online=True, last_seen_at=now,
                                last_payload={"agent_version": "0.14.0"}),
                HeartbeatSource(source_name="stale", source_type="PC_AGENT", is_online=True,
                                last_seen_at=now - timedelta(days=1),
                                last_payload={"agent_version": "0.15.0", "metrics": {"cpu_percent": 100}}),
                InternalNotification(event_type="agent_offline", title="History", message="History", device="fresh",
                                     status="action", requires_action=True, created_at=now - timedelta(days=1)),
            ])
            await session.commit()
            with (
                patch.object(main, "settings", settings), patch.object(main, "bot_client", object()),
                patch.object(main, "load_profile", return_value={}),
                patch.object(main, "collect_server_metrics", return_value={"cpu_percent": 1}),
                patch.object(main, "collect_systemd_statuses", return_value={}),
                patch.object(main, "load_quotes", return_value=[]),
                patch.object(main, "installer_public_info", return_value={"version": "0.14.0"}),
                patch.object(main, "_public_site_status", AsyncMock(return_value={"status": "online", "available": True})),
                patch.object(main, "all_scenarios", return_value=[]), patch.object(main, "load_rules", return_value=[]),
            ):
                payload = await main.mini_bootstrap(
                    user=SimpleNamespace(user_id=42, first_name="Test", username="test", is_owner=True), session=session
                )
            self.assertEqual(payload["notifications_unread"], 1)
            self.assertEqual(payload["health_summary"]["score"], 100)
            self.assertEqual(payload["attention_summary"]["active_count"], 1)
            self.assertEqual(payload["attention_summary"]["review_count"], 0)
            stale = next(source for source in payload["sources"] if source["source_name"] == "stale")
            self.assertFalse(stale["is_online"])
            self.assertEqual(stale["attention_reasons"], ["offline"])
            self.assertFalse(stale["requires_update"])
            self.assertTrue(await session.scalar(select(HeartbeatSource.is_online).where(HeartbeatSource.source_name == "stale")))
            self.assertEqual(await unread_notification_count(session), 1)


if __name__ == "__main__":
    unittest.main()
