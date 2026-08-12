from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, Response
from starlette.requests import Request

import app.main as main


def make_request(*, host: str, proto: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/pwa/config",
            "raw_path": b"/api/pwa/config",
            "query_string": b"",
            "headers": [
                (b"x-forwarded-host", host.encode("ascii")),
                (b"x-forwarded-proto", proto.encode("ascii")),
            ],
            "client": ("127.0.0.1", 10000),
            "server": ("127.0.0.1", 8000),
        }
    )


class PwaConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_local_readiness_without_claiming_botfather_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            settings = SimpleNamespace(
                bot_token="123456:secret",
                telegram_bot_username="@XASS_AdminBot",
                telegram_bot_identity_cache_path=str(Path(raw_dir) / "identity.json"),
                owner_user_id=42,
            )
            with (
                patch.object(main, "settings", settings),
                patch.object(main, "bot_client", None),
                patch.object(main, "pwa_authenticate_session", return_value=None),
                patch.object(main, "passkey_count_credentials", AsyncMock(return_value=0)),
            ):
                payload = await main.pwa_config(make_request(host="xass.example", proto="https"))

        self.assertTrue(payload["login_ready"])
        self.assertEqual(payload["domain"], "xass.example")
        self.assertEqual(payload["domain_verification"], "telegram_only")
        self.assertEqual(
            payload["requirements"],
            {"bot_token": True, "bot_username": True, "owner_user_id": True, "https": True},
        )

    async def test_public_http_origin_is_not_marked_https_ready(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            settings = SimpleNamespace(
                bot_token="123456:secret",
                telegram_bot_username="xass_adminbot",
                telegram_bot_identity_cache_path=str(Path(raw_dir) / "identity.json"),
                owner_user_id=42,
            )
            with (
                patch.object(main, "settings", settings),
                patch.object(main, "bot_client", None),
                patch.object(main, "pwa_authenticate_session", return_value=None),
                patch.object(main, "passkey_count_credentials", AsyncMock(return_value=0)),
            ):
                payload = await main.pwa_config(make_request(host="xass.example", proto="http"))

        self.assertFalse(payload["login_ready"])
        self.assertFalse(payload["requirements"]["https"])

    def test_miniapp_contains_one_time_ios_pairing_flow(self) -> None:
        template = Path("miniapp.php").read_text(encoding="utf-8")
        self.assertIn("pwa/pair-link", template)
        self.assertIn("pwaApi('exchange'", template)
        self.assertIn("#pair=", Path("app/main.py").read_text(encoding="utf-8"))
        self.assertIn("Для iPhone нужен HTTPS-адрес", Path("app/main.py").read_text(encoding="utf-8"))

    async def test_pair_link_does_not_wait_for_telegram(self) -> None:
        request = make_request(host="xass.example", proto="https")
        response = Response()
        background = BackgroundTasks()
        config = SimpleNamespace(service_base_url="https://xass.example")
        issued = SimpleNamespace(
            token="xpw_test-token-for-background-menu-update",
            expires_at=datetime.now(timezone.utc),
            ttl_minutes=10,
        )
        fake_bot = SimpleNamespace(set_chat_menu_button=AsyncMock())
        with (
            patch.object(main, "bot_client", fake_bot),
            patch.object(main, "get_or_create_app_config", AsyncMock(return_value=config)),
            patch.object(main, "issue_pwa_pair_token", AsyncMock(return_value=issued)),
        ):
            payload = await main.mini_pwa_pair_link(
                request=request,
                response=response,
                background_tasks=background,
                payload=main.MiniPwaPairPayload(public_url="https://xass.example"),
                user=SimpleNamespace(user_id=42),
                session=SimpleNamespace(),
            )
            fake_bot.set_chat_menu_button.assert_not_awaited()
            self.assertTrue(payload["menu_update_scheduled"])
            self.assertEqual(len(background.tasks), 1)
            await background()
            fake_bot.set_chat_menu_button.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
