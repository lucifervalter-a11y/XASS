"""Security and verified-runtime tests for the XASS AI backend adapter."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import app.services.hermes as hermes_module
from app.services.hermes import (
    DEFAULT_HERMES_URL,
    HermesDependencyError,
    HermesService,
    HermesUnavailableError,
    get_hermes_service,
    load_hermes_config,
    normalize_hermes_url,
    save_hermes_config,
)


VALID_HEALTH = {
    "ok": True,
    "version": "0.22.2",
    "displayVersion": "Hermes Agent 0.22.2",
    "commit": "d923a5eb487a17c0a68801bba9ef0f56c81040a8",
    "auth_required": True,
}


def response(status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", "http://127.0.0.1:9119/api/health"),
    )


class FakeRuntimeClient:
    def __init__(self, result: httpx.Response | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.stream_calls: list[tuple[str, str, dict[str, object]]] = []

    def stream(self, method: str, url: str, **kwargs: object) -> "FakeStreamContext":
        self.stream_calls.append((method, url, kwargs))
        return FakeStreamContext(self.result, self.error)


class FakeStreamContext:
    def __init__(self, result: httpx.Response | None, error: Exception | None) -> None:
        self.result = result
        self.error = error

    async def __aenter__(self) -> httpx.Response:
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("Fake runtime has no response")
        return self.result

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class HermesUrlPolicyTests(unittest.TestCase):
    def test_default_and_loopback_origins_are_allowed(self) -> None:
        self.assertEqual(normalize_hermes_url(None), DEFAULT_HERMES_URL)
        self.assertEqual(normalize_hermes_url("localhost"), "http://localhost:9119")
        self.assertEqual(normalize_hermes_url("http://127.0.0.2:9443/"), "http://127.0.0.2:9443")
        self.assertEqual(normalize_hermes_url("http://[::1]"), "http://[::1]:9119")

    def test_non_loopback_and_url_smuggling_are_rejected(self) -> None:
        rejected = (
            "http://10.0.0.8:9119",
            "https://127.0.0.1:9119",
            "http://localhost.example.com:9119",
            "http://user:pass@127.0.0.1:9119",
            "http://127.0.0.1:9119/admin",
            "http://127.0.0.1:9119?next=http://example.com",
            "ftp://127.0.0.1:9119",
        )
        for candidate in rejected:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                normalize_hermes_url(candidate)


class HermesServiceTests(unittest.IsolatedAsyncioTestCase):
    def service_with(self, client: FakeRuntimeClient, *, url: str = DEFAULT_HERMES_URL) -> HermesService:
        return HermesService(url=url, api_key="must-not-be-sent", enabled=True, client=client)  # type: ignore[arg-type]

    async def test_health_requires_exact_verified_contract(self) -> None:
        invalid_payloads = (
            {},
            {**VALID_HEALTH, "ok": False},
            {**VALID_HEALTH, "version": "0.22.3"},
            {**VALID_HEALTH, "commit": "00000000"},
            {**VALID_HEALTH, "auth_required": False},
            {**VALID_HEALTH, "extra": True},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                service = self.service_with(FakeRuntimeClient(response(200, payload)))
                status_data = await service.get_status()
                self.assertFalse(status_data["online"])
                self.assertEqual(status_data["status"], "incompatible_health")
                self.assertFalse(status_data["mock"])
                self.assertFalse(status_data["interactive_available"])

        client = FakeRuntimeClient(response(200, VALID_HEALTH))
        status_data = await self.service_with(client).get_status()
        self.assertTrue(status_data["online"])
        self.assertEqual(status_data["version"], "0.22.2")
        self.assertEqual(status_data["runtime_commit"], VALID_HEALTH["commit"])
        self.assertEqual(status_data["active_model"], "")
        self.assertEqual(status_data["session_count"], 0)
        self.assertFalse(status_data["interactive_available"])
        self.assertEqual(status_data["interactive_status"], "unsupported_transport")
        self.assertEqual(client.stream_calls, [
            ("GET", "http://127.0.0.1:9119/api/health", {"headers": {"Accept": "application/json"}})
        ])

    async def test_health_body_is_bounded_before_json_parse(self) -> None:
        oversized = httpx.Response(
            200,
            content=b" " * ((64 * 1024) + 1),
            request=httpx.Request("GET", "http://127.0.0.1:9119/api/health"),
        )
        status_data = await self.service_with(FakeRuntimeClient(oversized)).get_status()
        self.assertFalse(status_data["online"])
        self.assertEqual(status_data["status"], "health_too_large")

    async def test_auth_not_found_and_server_errors_are_offline(self) -> None:
        expected = {401: "unauthorized", 403: "unauthorized", 404: "not_found", 500: "upstream_error"}
        for status_code, reason in expected.items():
            with self.subTest(status_code=status_code):
                service = self.service_with(FakeRuntimeClient(response(status_code, VALID_HEALTH)))
                status_data = await service.get_status()
                self.assertFalse(status_data["online"])
                self.assertEqual(status_data["status"], reason)

    async def test_disabled_unreachable_and_invalid_url_are_honest(self) -> None:
        disabled = HermesService(enabled=False)
        disabled_status = await disabled.get_status()
        self.assertEqual(disabled_status["status"], "disabled")
        self.assertFalse(disabled_status["mock"])

        offline = self.service_with(FakeRuntimeClient(error=httpx.ConnectError("offline")))
        offline_status = await offline.get_status()
        self.assertEqual(offline_status["status"], "unavailable")
        self.assertEqual(offline_status["version"], "")

        client = FakeRuntimeClient(response(200, VALID_HEALTH))
        invalid = self.service_with(client, url="http://169.254.169.254/latest")
        invalid_status = await invalid.get_status()
        self.assertEqual(invalid_status["status"], "invalid_config")
        self.assertEqual(client.stream_calls, [])

    async def test_offline_operations_raise_503_class_without_fabrication(self) -> None:
        service = self.service_with(FakeRuntimeClient(error=httpx.ConnectError("offline")))
        for operation in (
            service.list_sessions(),
            service.create_session("test"),
            service.send_prompt("session", "prompt"),
            service.list_skills(),
            service.run_skill("system_metrics"),
        ):
            with self.assertRaises(HermesUnavailableError):
                await operation

    async def test_verified_runtime_operations_fail_closed_until_websocket_is_approved(self) -> None:
        service = self.service_with(FakeRuntimeClient(response(200, VALID_HEALTH)))
        for operation in (
            service.list_sessions(),
            service.create_session("test"),
            service.send_prompt("session", "prompt"),
            service.list_skills(),
            service.run_skill("system_metrics"),
        ):
            with self.assertRaises(HermesDependencyError) as caught:
                await operation
            self.assertEqual(caught.exception.reason, "unsupported_transport")


class HermesConfigTests(unittest.TestCase):
    def test_config_round_trip_and_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "hermes_config.json"
            self.assertEqual(load_hermes_config(path), {})
            payload = {
                "hermes_url": DEFAULT_HERMES_URL,
                "hermes_api_key": "secret",
                "hermes_enabled": True,
            }
            expected = {
                "hermes_url": DEFAULT_HERMES_URL,
                "hermes_enabled": True,
            }
            self.assertEqual(save_hermes_config(path, payload), expected)
            self.assertEqual(load_hermes_config(path), expected)
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_hermes_config(path), {})

    def test_factory_defaults_to_port_9119_and_updates_existing_instance(self) -> None:
        with patch.object(hermes_module, "_hermes_service_instance", None):
            service = get_hermes_service()
            self.assertEqual(service.url, DEFAULT_HERMES_URL)
            settings = SimpleNamespace(
                hermes_url="http://localhost:9119",
                hermes_api_key="configured",
                hermes_enabled=False,
            )
            self.assertIs(get_hermes_service(settings), service)
            self.assertEqual(service.url, "http://localhost:9119")
            self.assertEqual(service.api_key, "configured")
            self.assertFalse(service.enabled)


if __name__ == "__main__":
    unittest.main()
