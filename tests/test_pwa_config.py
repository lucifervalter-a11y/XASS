from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
            ):
                payload = await main.pwa_config(make_request(host="xass.example", proto="http"))

        self.assertFalse(payload["login_ready"])
        self.assertFalse(payload["requirements"]["https"])


if __name__ == "__main__":
    unittest.main()
