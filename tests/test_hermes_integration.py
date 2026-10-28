from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

CLIENT_ROOT = Path(__file__).resolve().parents[1] / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import hermes_client


class HermesClientTests(unittest.TestCase):
    def health(self, **changes):
        payload = {
            "ok": True,
            "version": "0.22.2",
            "displayVersion": "0.22.2 (d923a5eb)",
            "commit": "d923a5eb487a17c0a68801bba9ef0f56c81040a8",
            "auth_required": True,
        }
        payload.update(changes)
        return payload

    @staticmethod
    def response(status_code=200, payload=None, content=None):
        response = MagicMock()
        response.status_code = status_code
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        body = content if content is not None else json.dumps(payload).encode("utf-8")
        response.iter_bytes.return_value = [body]
        return response

    def test_normalize_hermes_url_is_loopback_only(self) -> None:
        self.assertEqual(hermes_client.DEFAULT_HERMES_URL, "http://127.0.0.1:9119")
        self.assertEqual(hermes_client.normalize_hermes_url(""), hermes_client.DEFAULT_HERMES_URL)
        self.assertEqual(hermes_client.normalize_hermes_url(None), hermes_client.DEFAULT_HERMES_URL)
        self.assertEqual(hermes_client.normalize_hermes_url("127.0.0.1"), "http://127.0.0.1:9119")
        self.assertEqual(hermes_client.normalize_hermes_url("localhost:9123"), "http://localhost:9123")
        self.assertEqual(hermes_client.normalize_hermes_url("http://[::1]:9119/"), "http://[::1]:9119")
        for value in (
            "https://127.0.0.1:9119",
            "http://192.168.1.50:9119",
            "http://hermes.example.com:9119",
            "http://127.0.0.1:9119/api/health",
            "http://user:secret@127.0.0.1:9119",
            "http://127.0.0.1:0",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hermes_client.normalize_hermes_url(value)

    def test_get_hermes_webui_url_rejects_remote_configuration(self) -> None:
        self.assertEqual(hermes_client.get_hermes_webui_url({}), hermes_client.DEFAULT_HERMES_URL)
        self.assertEqual(
            hermes_client.get_hermes_webui_url({"hermes_url": "http://localhost:9123"}),
            "http://localhost:9123",
        )
        with self.assertRaises(ValueError):
            hermes_client.get_hermes_webui_url({"hermes_url": "http://192.168.1.50:9119"})

    def test_check_hermes_status_uses_exact_health_contract(self) -> None:
        response = self.response(payload=self.health(displayVersion="XASS AI build 0.22.2"))
        with patch.object(httpx.Client, "stream", return_value=response) as request:
            status = hermes_client.check_hermes_status("http://127.0.0.1:9119")
        request.assert_called_once_with(
            "GET",
            "http://127.0.0.1:9119/api/health",
            headers={"Accept": "application/json"},
        )
        self.assertTrue(status["online"])
        self.assertTrue(status["ok"])
        self.assertEqual(status["endpoint"], "/api/health")
        self.assertEqual(status["details"]["version"], "0.22.2")
        self.assertEqual(status["details"]["display_version"], "XASS AI build 0.22.2")
        self.assertEqual(status["details"]["commit"], hermes_client.SUPPORTED_HERMES_COMMIT)
        self.assertTrue(status["details"]["auth_required"])

    def test_auth_missing_and_http_errors_are_offline(self) -> None:
        for code in (401, 403, 404, 500, 503):
            with self.subTest(code=code), patch.object(
                httpx.Client, "stream", return_value=self.response(status_code=code, payload={"detail": "ignored"})
            ) as request:
                status = hermes_client.check_hermes_status()
            self.assertFalse(status["online"])
            self.assertFalse(status["ok"])
            self.assertEqual(status["status_code"], code)
            self.assertEqual(status["error_code"], "http_status")
            request.assert_called_once()

    def test_wrong_shape_or_version_is_offline(self) -> None:
        invalid = (
            {"status": "ok"},
            self.health(version="0.22.1"),
            self.health(ok=False),
            {**self.health(), "extra": True},
            self.health(commit=""),
            self.health(commit="aaaaaaaa1234567890"),
            self.health(auth_required="yes"),
            self.health(displayVersion=22),
        )
        for payload in invalid:
            with self.subTest(payload=payload), patch.object(
                httpx.Client, "stream", return_value=self.response(payload=payload)
            ):
                status = hermes_client.check_hermes_status()
            self.assertFalse(status["online"])
            self.assertEqual(status["error_code"], "incompatible_health")

    def test_transport_failure_is_offline(self) -> None:
        with patch.object(httpx.Client, "stream", side_effect=httpx.ConnectError("refused")):
            status = hermes_client.check_hermes_status()
        self.assertFalse(status["online"])
        self.assertEqual(status["error_code"], "transport_error")

    def test_remote_status_is_rejected_without_network(self) -> None:
        with patch.object(httpx.Client, "stream") as request:
            status = hermes_client.check_hermes_status("http://10.0.0.5:9119")
        request.assert_not_called()
        self.assertFalse(status["online"])
        self.assertEqual(status["error_code"], "invalid_url")

    def test_health_body_is_bounded_to_64_kib(self) -> None:
        response = self.response(content=b"x" * (hermes_client.MAX_HEALTH_BYTES + 1))
        with patch.object(httpx.Client, "stream", return_value=response):
            status = hermes_client.check_hermes_status()
        self.assertFalse(status["online"])
        self.assertEqual(status["error_code"], "health_too_large")

    def test_send_hermes_prompt_empty(self) -> None:
        with patch.object(hermes_client, "check_hermes_status") as check:
            result = hermes_client.send_hermes_prompt("")
        check.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "empty_prompt")

    def test_send_hermes_prompt_offline(self) -> None:
        with patch.object(
            hermes_client,
            "check_hermes_status",
            return_value={"online": False, "error": "Не в сети", "error_code": "transport_error"},
        ):
            result = hermes_client.send_hermes_prompt("Проверь систему")
        self.assertFalse(result["ok"])
        self.assertFalse(result["online"])
        self.assertEqual(result["error_code"], "transport_error")

    def test_prompt_fails_closed_without_websocket_token(self) -> None:
        with patch.object(hermes_client, "check_hermes_status", return_value={"online": True}):
            result = hermes_client.send_hermes_prompt("Проверь систему")
        self.assertFalse(result["ok"])
        self.assertTrue(result["online"])
        self.assertFalse(result["supported"])
        self.assertEqual(result["error_code"], "websocket_token_unavailable")
        self.assertIn("защищённый канал", result["error"])
        self.assertNotIn("Web UI", result["error"])
        self.assertNotIn("Hermes", result["error"])
        self.assertNotIn("response", result)

    def test_daemon_url_keyword_is_not_silently_accepted(self) -> None:
        with self.assertRaises(TypeError):
            hermes_client.send_hermes_prompt("test", daemon_url="http://127.0.0.1:9119")


if __name__ == "__main__":
    unittest.main()
