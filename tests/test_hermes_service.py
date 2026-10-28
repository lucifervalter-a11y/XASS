"""Automated tests for HermesService (app.services.hermes).

Validates:
1. HermesService.get_status() and check_health() with both live responses and graceful offline fallback.
2. HermesService.send_prompt() with session creation, message sending, and token tracking.
3. HermesService.list_skills() and skill execution (both live and fallback).
4. HermesService.list_sessions() and create_session().
5. HermesService configuration persistence (load_hermes_config / save_hermes_config).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.services.hermes import (
    HermesService,
    _default_sessions,
    _default_skills,
    get_hermes_service,
    load_hermes_config,
    save_hermes_config,
)


class HermesServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.mock_client = AsyncMock(spec=httpx.AsyncClient)
        # By default mock client simulates offline/connection error unless overridden
        self.mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Offline"))
        self.mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Offline"))
        self.service = HermesService(
            url="http://127.0.0.1:8787",
            api_key="secret-key",
            enabled=True,
            client=self.mock_client,
            default_model="hermes-3-llama-3.1-8b",
        )

    # -------------------------------------------------------------------------
    # 1. Status & Health Checks
    # -------------------------------------------------------------------------
    async def test_get_status_live_success(self) -> None:
        """When Hermes server responds 200, return live status."""
        mock_response = httpx.Response(
            200,
            json={
                "version": "hermes-agent-v1.2.0",
                "active_model": "hermes-pro-70b",
                "session_count": 8,
            },
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "http://127.0.0.1:8787/api/status"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        self.service.client = mock_client

        status = await self.service.get_status()
        self.assertTrue(status["online"])
        self.assertTrue(status["enabled"])
        self.assertFalse(status["mock"])
        self.assertEqual(status["version"], "hermes-agent-v1.2.0")
        self.assertEqual(status["active_model"], "hermes-pro-70b")
        self.assertEqual(status["session_count"], 8)
        self.assertEqual(status["status"], "online")

    async def test_get_status_offline_graceful_fallback(self) -> None:
        """When Hermes server is unreachable, gracefully fall back to mock status without crashing."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        self.service.client = mock_client

        status = await self.service.get_status()
        self.assertFalse(status["online"])
        self.assertTrue(status["enabled"])
        self.assertTrue(status["mock"])
        self.assertEqual(status["status"], "offline_fallback")
        self.assertIn("fallback", status["details"])
        self.assertGreaterEqual(status["session_count"], 1)

    async def test_get_status_when_disabled(self) -> None:
        """When Hermes is disabled, return disabled status immediately without HTTP calls."""
        disabled_service = HermesService(enabled=False)
        status = await disabled_service.get_status()
        self.assertFalse(status["online"])
        self.assertFalse(status["enabled"])
        self.assertEqual(status["status"], "disabled")
        self.assertTrue(status["mock"])

    async def test_check_health_delegates_to_get_status(self) -> None:
        """check_health() should mirror get_status()."""
        health = await self.service.check_health()
        self.assertIn("online", health)
        self.assertIn("status", health)

    # -------------------------------------------------------------------------
    # 2. Session Management
    # -------------------------------------------------------------------------
    async def test_list_sessions_live_success(self) -> None:
        """list_sessions returns parsed sessions from live API."""
        mock_sessions_data = [
            {"id": "sess-live-1", "title": "Live Session 1", "message_count": 4, "model": "hermes-3-8b"},
            {"id": "sess-live-2", "title": "Live Session 2", "message_count": 0, "model": "hermes-3-8b"},
        ]
        mock_response = httpx.Response(
            200,
            json={"sessions": mock_sessions_data},
            request=httpx.Request("GET", "http://127.0.0.1:8787/api/sessions"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        self.service.client = mock_client

        sessions = await self.service.list_sessions()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["id"], "sess-live-1")
        self.assertEqual(sessions[0]["message_count"], 4)

    async def test_list_sessions_offline_fallback(self) -> None:
        """list_sessions returns default seed sessions when offline."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectTimeout("Timeout"))
        self.service.client = mock_client

        sessions = await self.service.list_sessions()
        self.assertIsInstance(sessions, list)
        self.assertGreaterEqual(len(sessions), 2)
        self.assertIn("id", sessions[0])
        self.assertIn("title", sessions[0])

    async def test_create_session_live_success(self) -> None:
        """create_session posts to live Hermes and returns new session."""
        mock_response = httpx.Response(
            201,
            json={
                "id": "sess-new-123",
                "title": "Новая сессия пользователя",
                "message_count": 0,
                "model": "hermes-3-llama-3.1-8b",
            },
            request=httpx.Request("POST", "http://127.0.0.1:8787/api/sessions"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        self.service.client = mock_client

        created = await self.service.create_session(title="Новая сессия пользователя")
        self.assertEqual(created["id"], "sess-new-123")
        self.assertEqual(created["title"], "Новая сессия пользователя")
        self.assertFalse(created["mock"])

    async def test_create_session_offline_fallback(self) -> None:
        """create_session generates a mock session with unique id when offline."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Refused"))
        self.service.client = mock_client

        created = await self.service.create_session(title="Офлайн сессия")
        self.assertTrue(created["mock"])
        self.assertTrue(created["id"].startswith("hermes-"))
        self.assertEqual(created["title"], "Офлайн сессия")

        # Session should be added to _mock_sessions
        all_sessions = await self.service.list_sessions()
        self.assertIn(created["id"], [s["id"] for s in all_sessions])

    # -------------------------------------------------------------------------
    # 3. Prompt Execution & Token Tracking
    # -------------------------------------------------------------------------
    async def test_send_prompt_live_success(self) -> None:
        """send_prompt sends message to live Hermes and extracts tokens & tool calls."""
        mock_response = httpx.Response(
            200,
            json={
                "response": "Ответ от Гермеса: сервер работает стабильно.",
                "tokens_used": 42,
                "tool_calls": [{"tool": "system_metrics", "result": "ok"}],
                "model": "hermes-3-llama-3.1-8b",
            },
            request=httpx.Request("POST", "http://127.0.0.1:8787/api/chat"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        self.service.client = mock_client

        res = await self.service.send_prompt(
            session_id="hermes-sess-001",
            prompt="Проверь статус сервера",
            model="hermes-3-llama-3.1-8b",
        )
        self.assertFalse(res["mock"])
        self.assertEqual(res["response"], "Ответ от Гермеса: сервер работает стабильно.")
        self.assertEqual(res["tokens_used"], 42)
        self.assertEqual(len(res["tool_calls"]), 1)

    async def test_send_prompt_offline_fallback_and_token_tracking(self) -> None:
        """When offline, send_prompt generates contextual mock response and tracks tokens."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Offline"))
        self.service.client = mock_client

        # Prompt about system metrics
        res = await self.service.send_prompt(
            session_id="hermes-sess-001",
            prompt="Какая сейчас нагрузка на CPU и память?",
        )
        self.assertTrue(res["mock"])
        self.assertIn("Hermes Agent:", res["response"])
        self.assertGreater(res["tokens_used"], 0)
        self.assertTrue(any(tc["tool"] == "system_metrics" for tc in res["tool_calls"]))

        # Verify session message count was incremented
        sessions = await self.service.list_sessions()
        sess_001 = next(s for s in sessions if s["id"] == "hermes-sess-001")
        self.assertGreater(sess_001["message_count"], 6)

    async def test_send_prompt_creates_session_if_missing(self) -> None:
        """If session_id does not exist, offline send_prompt registers it automatically."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Offline"))
        self.service.client = mock_client

        new_sess_id = "sess-brand-new-999"
        res = await self.service.send_prompt(
            session_id=new_sess_id,
            prompt="Привет, Гермес!",
        )
        self.assertTrue(res["mock"])
        self.assertEqual(res["session_id"], new_sess_id)

        sessions = await self.service.list_sessions()
        self.assertIn(new_sess_id, [s["id"] for s in sessions])

    # -------------------------------------------------------------------------
    # 4. Skills Catalog & Execution
    # -------------------------------------------------------------------------
    async def test_list_skills_live_success(self) -> None:
        """list_skills fetches skills from live API."""
        live_skills = [
            {"name": "custom_scanner", "description": "Custom scanner", "enabled": True}
        ]
        mock_response = httpx.Response(
            200,
            json=live_skills,
            request=httpx.Request("GET", "http://127.0.0.1:8787/api/skills"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        self.service.client = mock_client

        skills = await self.service.list_skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "custom_scanner")

    async def test_list_skills_offline_returns_catalog(self) -> None:
        """list_skills returns default catalog when offline."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Offline"))
        self.service.client = mock_client

        skills = await self.service.list_skills()
        self.assertGreaterEqual(len(skills), 6)
        names = [s["name"] for s in skills]
        self.assertIn("system_metrics", names)
        self.assertIn("service_control", names)
        self.assertIn("music_diagnostics", names)
        self.assertIn("database_health", names)
        self.assertIn("web_search", names)
        self.assertIn("log_inspector", names)

    async def test_run_skill_live_success(self) -> None:
        """run_skill calls skill run endpoint on live server."""
        mock_response = httpx.Response(
            200,
            json={"status": "ok", "result": {"output": "100%"}},
            request=httpx.Request("POST", "http://127.0.0.1:8787/api/skills/test_tool/run"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        self.service.client = mock_client

        res = await self.service.run_skill("test_tool", {"param": 1})
        self.assertTrue(res["ok"])
        self.assertFalse(res["mock"])
        self.assertEqual(res["skill"], "test_tool")

    async def test_run_skill_offline_mock_dispatch(self) -> None:
        """run_skill executes fallback handlers locally for standard tools."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Offline"))
        self.service.client = mock_client

        # system_metrics
        res_metrics = await self.service.run_skill("system_metrics", {"top_processes": 3})
        self.assertTrue(res_metrics["ok"])
        self.assertIn("result", res_metrics)

        # database_health
        res_db = await self.service.run_skill("database_health", {"deep": False})
        self.assertTrue(res_db["ok"])
        self.assertEqual(res_db["result"]["status"], "healthy")

        # web_search
        res_search = await self.service.run_skill("web_search", {"query": "XASS Telegram"})
        self.assertTrue(res_search["ok"])
        self.assertGreater(len(res_search["result"]["results"]), 0)

        # unknown tool fallback
        res_unknown = await self.service.run_skill("non_existent_tool")
        self.assertTrue(res_unknown["ok"])
        self.assertTrue(res_unknown["mock"])

    # -------------------------------------------------------------------------
    # 5. Config Persistence & Factory
    # -------------------------------------------------------------------------
    def test_config_load_and_save(self) -> None:
        """load_hermes_config and save_hermes_config work reliably and handle missing files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "hermes_config.json"
            # Missing file
            self.assertEqual(load_hermes_config(path), {})

            # Save
            payload = {"url": "http://10.0.0.5:8787", "api_key": "abc", "enabled": True}
            save_hermes_config(path, payload)
            self.assertEqual(load_hermes_config(path), payload)

            # Corrupt file safety
            path.write_text("not a json", encoding="utf-8")
            self.assertEqual(load_hermes_config(path), {})

    def test_get_hermes_service_factory(self) -> None:
        """get_hermes_service instantiates a configured HermesService instance."""
        settings = SimpleNamespace(
            hermes_url="http://custom-host:9999",
            hermes_api_key="custom-key",
            hermes_enabled=True,
        )
        svc = get_hermes_service(settings)
        self.assertEqual(svc.url, "http://custom-host:9999")
        self.assertEqual(svc.api_key, "custom-key")
        self.assertTrue(svc.enabled)


if __name__ == "__main__":
    unittest.main()
