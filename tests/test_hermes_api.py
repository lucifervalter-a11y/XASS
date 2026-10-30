"""Owner API tests for honest, redacted XASS AI behavior."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI, HTTPException, status

from app.config import Settings
from app.db import get_session
from app.hermes_api import build_router
import app.services.hermes as hermes_module


VALID_HEALTH = {
    "ok": True,
    "version": "0.22.2",
    "displayVersion": "Hermes Agent 0.22.2",
    "commit": "d923a5eb487a17c0a68801bba9ef0f56c81040a8",
    "auth_required": True,
}


def response(status_code: int, payload: object, path: str = "/api/health") -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", f"http://127.0.0.1:9119{path}"),
    )


class FakeStreamContext:
    def __init__(self, result: httpx.Response | None = None, error: Exception | None = None) -> None:
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


class HermesApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "hermes_config.json"
        self.settings = Settings(
            _env_file=None,
            owner_user_id=42,
            hermes_url="http://127.0.0.1:9119",
            hermes_api_key="test-key",
            hermes_enabled=True,
        )
        self.auth_override = lambda: {"user_id": 42, "role": "owner"}

        async def require_owner():
            return self.auth_override()

        self.require_action_proof = AsyncMock()
        hermes_module._hermes_service_instance = None
        with patch("app.hermes_api.HERMES_CONFIG_PATH", self.config_path):
            router = build_router(self.settings, require_owner, self.require_action_proof)
        self.service = hermes_module.get_hermes_service(self.settings)
        self.runtime = AsyncMock(spec=httpx.AsyncClient)
        self.runtime.stream = MagicMock(
            return_value=FakeStreamContext(error=httpx.ConnectError("offline"))
        )
        self.runtime.request = AsyncMock(side_effect=httpx.ConnectError("offline"))
        self.service.client = self.runtime

        app = FastAPI()
        self.db_session = object()
        app.dependency_overrides[get_session] = lambda: self.db_session
        app.include_router(router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://xass.test",
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_owner_dependency_guards_all_endpoints(self) -> None:
        def unauthorized():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="owner required")

        self.auth_override = unauthorized
        requests = (
            ("GET", "/api/mini/hermes/status", None),
            ("GET", "/api/mini/hermes/sessions", None),
            ("POST", "/api/mini/hermes/sessions", {"title": "test"}),
            ("POST", "/api/mini/hermes/chat", {"session_id": "s1", "prompt": "hello"}),
            ("GET", "/api/mini/hermes/skills", None),
            ("POST", "/api/mini/hermes/skill-run", {"skill_name": "tool"}),
            ("GET", "/api/mini/hermes/config", None),
            ("POST", "/api/mini/hermes/config", {"hermes_enabled": False}),
        )
        for method, url, body in requests:
            with self.subTest(method=method, url=url):
                result = await self.client.request(method, url, json=body)
                self.assertEqual(result.status_code, 401)
        self.require_action_proof.assert_not_awaited()

    async def test_status_reports_offline_without_fake_metadata(self) -> None:
        result = await self.client.get("/api/mini/hermes/status")
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["status"]["online"])
        self.assertFalse(body["status"]["interactive_available"])
        self.assertFalse(body["status"]["mock"])
        self.assertEqual(body["status"]["active_model"], "")
        self.assertEqual(body["status"]["session_count"], 0)

    async def test_offline_operations_return_503_instead_of_mock_success(self) -> None:
        requests = (
            ("GET", "/api/mini/hermes/sessions", None),
            ("POST", "/api/mini/hermes/sessions", {"title": "test"}),
            ("POST", "/api/mini/hermes/chat", {"session_id": "s1", "prompt": "hello"}),
            ("GET", "/api/mini/hermes/skills", None),
            ("POST", "/api/mini/hermes/skill-run", {"skill_name": "tool", "params": {}}),
        )
        for method, url, body in requests:
            with self.subTest(method=method, url=url):
                result = await self.client.request(method, url, json=body)
                self.assertEqual(result.status_code, 503, result.text)
                detail = result.json()["detail"]
                self.assertEqual(detail["code"], "unavailable")

    async def test_verified_runtime_without_approved_websocket_returns_424(self) -> None:
        self.runtime.stream = MagicMock(
            return_value=FakeStreamContext(result=response(200, VALID_HEALTH))
        )
        result = await self.client.get("/api/mini/hermes/sessions")
        self.assertEqual(result.status_code, 424, result.text)
        self.assertEqual(result.json()["detail"]["code"], "unsupported_transport")
        self.runtime.request.assert_not_awaited()

    async def test_health_uses_exact_endpoint_and_contract(self) -> None:
        self.runtime.stream = MagicMock(
            return_value=FakeStreamContext(result=response(200, VALID_HEALTH))
        )
        result = await self.client.get("/api/mini/hermes/status")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["status"]["online"])
        self.assertEqual(result.json()["status"]["runtime_commit"], VALID_HEALTH["commit"])
        self.assertFalse(result.json()["status"]["interactive_available"])
        self.assertEqual(result.json()["status"]["interactive_status"], "unsupported_transport")
        self.runtime.stream.assert_called_once_with(
            "GET",
            "http://127.0.0.1:9119/api/health",
            headers={"Accept": "application/json"},
        )

    async def test_config_is_redacted_and_reports_key_presence(self) -> None:
        result = await self.client.get("/api/mini/hermes/config")
        self.assertEqual(result.status_code, 200)
        config = result.json()["config"]
        self.assertEqual(config["hermes_api_key"], "")
        self.assertFalse(config["hermes_api_key_configured"])
        self.assertNotIn("test-key", result.text)

    async def test_router_scrubs_legacy_persisted_key(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.json"
        legacy_path.write_text(
            json.dumps({
                "hermes_url": "http://127.0.0.1:9119",
                "hermes_api_key": "legacy-secret",
                "hermes_enabled": True,
            }),
            encoding="utf-8",
        )
        settings = Settings(
            _env_file=None,
            owner_user_id=42,
            hermes_api_key="environment-secret",
        )

        async def require_owner():
            return {"user_id": 42, "role": "owner"}

        with (
            patch.object(hermes_module, "_hermes_service_instance", None),
            patch("app.hermes_api.HERMES_CONFIG_PATH", legacy_path),
        ):
            build_router(settings, require_owner, AsyncMock())

        self.assertEqual(settings.hermes_api_key, "")
        persisted = json.loads(legacy_path.read_text(encoding="utf-8"))
        self.assertNotIn("hermes_api_key", persisted)
        self.assertNotIn("legacy-secret", legacy_path.read_text(encoding="utf-8"))

    async def test_config_rejects_non_loopback_urls_without_mutation(self) -> None:
        original_url = self.settings.hermes_url
        with patch("app.hermes_api.HERMES_CONFIG_PATH", self.config_path):
            result = await self.client.post(
                "/api/mini/hermes/config",
                json={"hermes_url": "http://10.0.0.99:9119"},
            )
        self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.settings.hermes_url, original_url)
        self.assertFalse(self.config_path.exists())

    async def test_mutating_routes_require_action_proof_before_access(self) -> None:
        self.require_action_proof.side_effect = HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="proof required",
        )
        requests = (
            ("/api/mini/hermes/sessions", {"title": "test"}),
            ("/api/mini/hermes/chat", {"session_id": "s1", "prompt": "hello"}),
            ("/api/mini/hermes/skill-run", {"skill_name": "tool", "params": {}}),
            ("/api/mini/hermes/config", {"hermes_enabled": False}),
        )
        for url, payload in requests:
            with self.subTest(url=url):
                result = await self.client.post(url, json=payload)
                self.assertEqual(result.status_code, 428, result.text)
        self.runtime.stream.assert_not_called()
        self.assertEqual(self.require_action_proof.await_count, len(requests))
        self.assertFalse(self.config_path.exists())

    async def test_prompt_passes_established_action_proof_contract(self) -> None:
        result = await self.client.post(
            "/api/mini/hermes/chat",
            headers={
                "X-Telegram-Init-Data": "telegram-proof",
                "X-XASS-Action-Proof": "action-proof",
            },
            json={"session_id": "s1", "prompt": "hello", "model": "m1"},
        )
        self.assertEqual(result.status_code, 503, result.text)
        self.require_action_proof.assert_awaited_once_with(
            session=self.db_session,
            user={"user_id": 42, "role": "owner"},
            telegram_init_data="telegram-proof",
            action_proof="action-proof",
            purpose="xass-ai:prompt",
            binding={"session_id": "s1", "prompt": "hello", "model": "m1"},
        )

    async def test_config_rejects_reusable_key(self) -> None:
        with patch("app.hermes_api.HERMES_CONFIG_PATH", self.config_path):
            result = await self.client.post(
                "/api/mini/hermes/config",
                json={"hermes_api_key": "new-secret"},
            )
        self.assertEqual(result.status_code, 400, result.text)
        self.assertNotIn("new-secret", result.text)
        self.assertFalse(self.config_path.exists())

    async def test_config_accepts_loopback_and_does_not_persist_key_field(self) -> None:
        with patch("app.hermes_api.HERMES_CONFIG_PATH", self.config_path):
            result = await self.client.post(
                "/api/mini/hermes/config",
                json={
                    "hermes_url": "http://localhost:9119/",
                    "hermes_api_key": "",
                    "hermes_enabled": False,
                },
            )
        self.assertEqual(result.status_code, 200, result.text)
        config = result.json()["config"]
        self.assertEqual(config["hermes_url"], "http://localhost:9119")
        self.assertEqual(config["hermes_api_key"], "")
        self.assertFalse(config["hermes_api_key_configured"])
        self.assertFalse(config["hermes_enabled"])
        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("hermes_api_key", persisted)
        self.require_action_proof.assert_awaited_once_with(
            session=self.db_session,
            user={"user_id": 42, "role": "owner"},
            telegram_init_data="",
            action_proof="",
            purpose="xass-ai:config",
            binding={"hermes_url": "http://localhost:9119", "hermes_enabled": False},
        )

    async def test_chat_body_validation_still_returns_422(self) -> None:
        result = await self.client.post("/api/mini/hermes/chat", json={"prompt": ""})
        self.assertEqual(result.status_code, 422)


if __name__ == "__main__":
    unittest.main()
