"""Automated tests for Hermes MiniApp API router (app.hermes_api).

Validates:
1. Permission checks (require_mini_owner) guarding all Hermes endpoints.
2. GET /api/mini/hermes/status
3. GET /api/mini/hermes/sessions
4. POST /api/mini/hermes/sessions
5. POST /api/mini/hermes/chat
6. GET /api/mini/hermes/skills
7. POST /api/mini/hermes/skill-run
8. GET /api/mini/hermes/config and POST /api/mini/hermes/config
9. Validation errors for invalid bodies.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException, status

from app.config import Settings
from app.hermes_api import build_router


class HermesApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            owner_user_id=42,
            hermes_url="http://127.0.0.1:8787",
            hermes_api_key="test-key",
            hermes_enabled=True,
        )

        # Authenticated user stub
        self.mock_user = {"user_id": 42, "role": "owner"}
        self.auth_override = lambda: self.mock_user

        # Standalone mock auth dependency
        async def mock_require_mini_owner():
            return self.auth_override()

        self.require_mini_owner = mock_require_mini_owner

        # Build router and test app
        self.router = build_router(self.settings, self.require_mini_owner)
        from app.services.hermes import get_hermes_service
        service = get_hermes_service(self.settings)
        self.mock_client = AsyncMock(spec=httpx.AsyncClient)
        self.mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Offline"))
        self.mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Offline"))
        service.client = self.mock_client
        self.app = FastAPI()
        self.app.include_router(self.router)

        # In-memory AsyncClient via ASGI transport
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://xass.test",
        )
        self.addAsyncCleanup(self.client.aclose)

    # -------------------------------------------------------------------------
    # 1. Permission checks (require_mini_owner)
    # -------------------------------------------------------------------------
    async def test_permission_checks_reject_unauthorized(self) -> None:
        """All Hermes endpoints must reject requests when require_mini_owner raises 401."""
        def unauthenticated():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized owner")

        self.auth_override = unauthenticated

        endpoints = [
            ("GET", "/api/mini/hermes/status", None),
            ("GET", "/api/mini/hermes/sessions", None),
            ("POST", "/api/mini/hermes/sessions", {"title": "Test"}),
            ("POST", "/api/mini/hermes/chat", {"session_id": "s1", "prompt": "hello"}),
            ("GET", "/api/mini/hermes/skills", None),
            ("POST", "/api/mini/hermes/skill-run", {"skill_name": "system_metrics"}),
            ("GET", "/api/mini/hermes/config", None),
            ("POST", "/api/mini/hermes/config", {"hermes_enabled": False}),
        ]

        for method, url, payload in endpoints:
            with self.subTest(endpoint=f"{method} {url}"):
                if method == "GET":
                    resp = await self.client.get(url)
                else:
                    resp = await self.client.post(url, json=payload or {})
                self.assertEqual(
                    resp.status_code,
                    status.HTTP_401_UNAUTHORIZED,
                    f"Endpoint {url} failed to reject unauthenticated request: {resp.text}",
                )

    # -------------------------------------------------------------------------
    # 2. GET /api/mini/hermes/status
    # -------------------------------------------------------------------------
    async def test_get_status_authenticated(self) -> None:
        """GET /api/mini/hermes/status returns ok=True and status dictionary."""
        resp = await self.client.get("/api/mini/hermes/status")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("status", data)
        status_info = data["status"]
        self.assertIn("online", status_info)
        self.assertIn("active_model", status_info)
        self.assertIn("session_count", status_info)

    # -------------------------------------------------------------------------
    # 3. Sessions Endpoints
    # -------------------------------------------------------------------------
    async def test_get_sessions_list(self) -> None:
        """GET /api/mini/hermes/sessions returns list of sessions."""
        resp = await self.client.get("/api/mini/hermes/sessions")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIsInstance(data.get("sessions"), list)
        self.assertGreater(len(data["sessions"]), 0)

    async def test_post_create_session(self) -> None:
        """POST /api/mini/hermes/sessions creates a new session."""
        resp = await self.client.post(
            "/api/mini/hermes/sessions",
            json={"title": "Тестовая API сессия"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        session = data.get("session")
        self.assertIsNotNone(session)
        self.assertEqual(session.get("title"), "Тестовая API сессия")
        self.assertTrue(session.get("id").startswith("hermes-"))

    # -------------------------------------------------------------------------
    # 4. POST /api/mini/hermes/chat
    # -------------------------------------------------------------------------
    async def test_post_chat_prompt_success(self) -> None:
        """POST /api/mini/hermes/chat sends prompt and returns agent response and tokens."""
        resp = await self.client.post(
            "/api/mini/hermes/chat",
            json={
                "session_id": "hermes-sess-001",
                "prompt": "Как дела с сервисом?",
                "model": "hermes-3-llama-3.1-8b",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("response", data)
        self.assertIn("tokens_used", data)
        self.assertGreater(data["tokens_used"], 0)
        self.assertEqual(data["session_id"], "hermes-sess-001")

    async def test_post_chat_validation_error_on_missing_fields(self) -> None:
        """POST /api/mini/hermes/chat with missing prompt or session_id returns 422."""
        resp = await self.client.post("/api/mini/hermes/chat", json={"prompt": ""})
        self.assertEqual(resp.status_code, 422)

    # -------------------------------------------------------------------------
    # 5. Skills Endpoints
    # -------------------------------------------------------------------------
    async def test_get_skills_list(self) -> None:
        """GET /api/mini/hermes/skills returns catalog of available skills."""
        resp = await self.client.get("/api/mini/hermes/skills")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        skills = data.get("skills")
        self.assertIsInstance(skills, list)
        self.assertGreaterEqual(len(skills), 5)
        skill_names = [s["name"] for s in skills]
        self.assertIn("system_metrics", skill_names)

    async def test_post_skill_run(self) -> None:
        """POST /api/mini/hermes/skill-run triggers skill execution."""
        resp = await self.client.post(
            "/api/mini/hermes/skill-run",
            json={"skill_name": "database_health", "params": {"deep": False}},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("skill"), "database_health")

    # -------------------------------------------------------------------------
    # 6. Configuration Endpoints
    # -------------------------------------------------------------------------
    async def test_get_and_post_config(self) -> None:
        """GET and POST /api/mini/hermes/config read and update integration settings."""
        # Read initial config
        get_resp = await self.client.get("/api/mini/hermes/config")
        self.assertEqual(get_resp.status_code, 200)
        cfg = get_resp.json()["config"]
        self.assertEqual(cfg["hermes_url"], "http://127.0.0.1:8787")
        self.assertTrue(cfg["hermes_enabled"])

        # Update config
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_cfg_path = Path(tmp_dir) / "test_hermes_cfg.json"
            with patch("app.hermes_api.HERMES_CONFIG_PATH", tmp_cfg_path):
                post_resp = await self.client.post(
                    "/api/mini/hermes/config",
                    json={
                        "hermes_url": "http://10.0.0.99:8787",
                        "hermes_api_key": "new-api-token",
                        "hermes_enabled": False,
                    },
                )
                self.assertEqual(post_resp.status_code, 200)
                updated = post_resp.json()["config"]
                self.assertEqual(updated["hermes_url"], "http://10.0.0.99:8787")
                self.assertEqual(updated["hermes_api_key"], "new-api-token")
                self.assertFalse(updated["hermes_enabled"])
                self.assertTrue(tmp_cfg_path.is_file())


if __name__ == "__main__":
    unittest.main()
