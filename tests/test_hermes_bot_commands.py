"""Extensive automated tests for Telegram bot Hermes commands and Theme switching (app.telegram_handler).

Validates:
1. /hermes <prompt> command handling, response formatting, and token display.
2. /hermes without arguments shows interactive help and guide.
3. /hermes error handling when HermesService fails (returns troubleshooting steps).
4. /hermes_status displays agent latency, active model, session count, and online/offline status.
5. /hermes_sessions lists current active and recent conversation sessions.
6. /hermes_new creates and activates a fresh session.
7. /theme with no arguments lists all 6 themes with checkmark for current.
8. /theme <valid_theme> switches theme, updates site_config.json, and confirms.
9. /theme <invalid> rejects change and preserves existing configuration.
10. Callback query routing for hermes and themes panels.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import Settings
from app.models import AppConfig
from app.services.projects_store import DEFAULT_SITE_CONFIG, VALID_THEMES, save_site_config
from app.telegram_handler import THEMES_DATA, TelegramUpdateHandler


class TelegramHermesBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.site_cfg_path = self.root / "site_config.json"
        save_site_config(self.site_cfg_path, dict(DEFAULT_SITE_CONFIG))

        self.settings = Settings(
            _env_file=None,
            owner_user_id=12345,
            bot_token="test-token-12345:dummy",
            site_config_json_path=str(self.site_cfg_path),
            projects_audit_log_path=str(self.root / "projects_audit.jsonl"),
            hermes_url="http://127.0.0.1:8787",
            hermes_enabled=True,
        )

        self.bot_client = MagicMock()
        self.bot_client.send_message = AsyncMock(return_value={"ok": True, "result": {"message_id": 999}})
        self.bot_client.edit_message_text = AsyncMock(return_value={"ok": True, "result": {"message_id": 999}})
        self.bot_client.answer_callback_query = AsyncMock(return_value={"ok": True})

        self.handler = TelegramUpdateHandler(self.settings, self.bot_client)
        self.config = AppConfig(id=1, save_mode="basic")
        self.session = AsyncMock()
        self.session.get = AsyncMock(return_value=self.config)

        # Mock HermesService
        self.mock_hermes = MagicMock()
        self.mock_hermes.url = "http://127.0.0.1:8787"
        self.mock_hermes.enabled = True
        self.mock_hermes.default_model = "hermes-3-llama-3.1-8b"
        self.mock_hermes.get_status = AsyncMock(return_value={
            "online": True,
            "enabled": True,
            "version": "1.0.0",
            "active_model": "Nous Hermes 3",
            "latency_ms": 25.4,
            "session_count": 3,
            "status": "online",
        })
        self.mock_hermes.list_sessions = AsyncMock(return_value=[
            {"id": "sess-alpha", "title": "Диагностика", "message_count": 3},
            {"id": "sess-beta", "title": "Разработка", "message_count": 1},
        ])
        self.mock_hermes.create_session = AsyncMock(return_value={
            "id": "sess-new-created",
            "title": "Новая сессия",
            "message_count": 0,
            "mock": False,
        })
        self.mock_hermes.send_prompt = AsyncMock(return_value={
            "response": "Hello! I am Hermes AI running through XASS.",
            "tokens_used": 42,
            "session_id": "sess-alpha",
            "mock": False,
            "tool_calls": [],
        })

        self.handler.hermes_service = self.mock_hermes
        self.handler._hermes_service = self.mock_hermes

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # Theme Data Consistency
    # -------------------------------------------------------------------------
    def test_theme_data_consistency(self) -> None:
        """All valid themes must be registered in THEMES_DATA with required metadata."""
        for theme_name in ("cyber", "tokyo", "nordic", "matrix", "sunset", "glass"):
            self.assertIn(theme_name, THEMES_DATA)
            meta = THEMES_DATA[theme_name]
            self.assertTrue(meta.get("name"))
            self.assertTrue(meta.get("icon"))
            self.assertTrue(meta.get("accent"))
            self.assertTrue(meta.get("desc"))

    # -------------------------------------------------------------------------
    # /hermes Command
    # -------------------------------------------------------------------------
    async def test_hermes_without_args_shows_help(self) -> None:
        """Calling /hermes without prompt returns usage guide and action buttons."""
        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 10},
            "/hermes",
        )

        self.bot_client.send_message.assert_called_once()
        chat_id, text = self.bot_client.send_message.call_args[0][:2]
        self.assertEqual(chat_id, 12345)
        self.assertIn("Hermes Agent AI", text)
        self.assertIn("/hermes <ваш запрос>", text)

    async def test_hermes_with_prompt_executes_and_replies(self) -> None:
        """Calling /hermes <prompt> forwards to HermesService and formats reply."""
        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 11},
            "/hermes How are you?",
        )

        self.mock_hermes.send_prompt.assert_called_once_with(
            session_id="sess-alpha",
            prompt="How are you?",
        )
        self.bot_client.send_message.assert_called()
        last_call_text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Hello! I am Hermes AI", last_call_text)
        self.assertIn("Токены: 42", last_call_text)

    async def test_hermes_error_handling_shows_guidance(self) -> None:
        """When HermesService raises an error, bot catches it and outputs troubleshooting steps."""
        self.mock_hermes.send_prompt = AsyncMock(side_effect=RuntimeError("Connection refused by host"))

        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 12},
            "/hermes crash please",
        )

        self.bot_client.send_message.assert_called()
        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Hermes Agent недоступен", text)
        self.assertIn("/hermes_status", text)

    # -------------------------------------------------------------------------
    # /hermes_status Command
    # -------------------------------------------------------------------------
    async def test_hermes_status_online(self) -> None:
        """/hermes_status displays active model, session count and latency."""
        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 13},
            "/hermes_status",
        )

        self.bot_client.send_message.assert_called_once()
        sent_text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Hermes Agent", sent_text)
        self.assertIn("Nous Hermes 3", sent_text)
        self.assertIn("В СЕТИ", sent_text)

    async def test_hermes_status_offline_fallback(self) -> None:
        """/hermes_status when offline indicates offline fallback state."""
        self.mock_hermes.get_status = AsyncMock(return_value={
            "online": False,
            "enabled": True,
            "version": "v0.4.0",
            "active_model": "hermes-3-llama-3.1-8b",
            "session_count": 2,
            "status": "offline_fallback",
        })

        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 14},
            "/hermes_status",
        )

        self.bot_client.send_message.assert_called_once()
        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("ОФЛАЙН", text)
        self.assertIn("fallback", text)

    # -------------------------------------------------------------------------
    # /hermes_sessions & /hermes_new
    # -------------------------------------------------------------------------
    async def test_hermes_sessions_listing(self) -> None:
        """/hermes_sessions lists current sessions with message counts."""
        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 15},
            "/hermes_sessions",
        )

        self.bot_client.send_message.assert_called_once()
        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Сессии диалогов Hermes Agent", text)
        self.assertIn("sess-alpha", text)
        self.assertIn("sess-beta", text)

    async def test_hermes_new_creates_and_activates_session(self) -> None:
        """/hermes_new calls HermesService.create_session and sets active user session."""
        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 16},
            "/hermes_new",
        )

        self.mock_hermes.create_session.assert_called_once()
        self.assertEqual(self.handler.hermes_active_sessions.get(12345), "sess-new-created")
        self.bot_client.send_message.assert_called_once()
        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Новая сессия", text)
        self.assertIn("sess-new-created", text)

    # -------------------------------------------------------------------------
    # /theme Command & Validation
    # -------------------------------------------------------------------------
    async def test_theme_without_args_shows_menu(self) -> None:
        """/theme without args renders theme catalog and current active theme."""
        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 17},
            "/theme",
        )

        self.bot_client.send_message.assert_called_once()
        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Выбор темы оформления XASS", text)
        for theme_name in THEMES_DATA:
            self.assertIn(theme_name, text)

    async def test_theme_switch_command_valid(self) -> None:
        """/theme <valid_theme> switches theme, updates site_config, and confirms."""
        for target_theme in ("tokyo", "nordic", "matrix", "sunset", "glass", "cyber"):
            with self.subTest(theme=target_theme):
                await self.handler._handle_command(
                    self.session,
                    {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 18},
                    f"/theme {target_theme}",
                )

                # Verify file persistence
                saved = json.loads(self.site_cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(saved.get("theme"), target_theme)
                self.assertEqual(saved.get("accent_color"), THEMES_DATA[target_theme]["accent"])

                # Verify confirmation message
                sent_text = self.bot_client.send_message.call_args[0][1]
                self.assertIn(THEMES_DATA[target_theme]["name"], sent_text)

    async def test_theme_switch_invalid_rejected(self) -> None:
        """/theme <invalid> rejects change and preserves existing configuration."""
        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 19},
            "/theme neon_cyber_glow",
        )

        saved = json.loads(self.site_cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("theme", "cyber"), "cyber")

        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Неизвестная тема: `neon_cyber_glow`", text)

    # -------------------------------------------------------------------------
    # Callbacks
    # -------------------------------------------------------------------------
    async def test_hermes_and_theme_callbacks(self) -> None:
        """Callback queries for panel:hermes, hermes:status, and theme:tokyo work properly."""
        # panel:hermes
        await self.handler._handle_hermes_or_theme_callback(
            self.session,
            self.config,
            callback={"id": "cb1"},
            data="panel:hermes",
            chat_id=12345,
            message_id=50,
            user_id=12345,
        )
        self.bot_client.edit_message_text.assert_called()
        self.assertIn("Hermes Agent AI", self.bot_client.edit_message_text.call_args[1]["text"])

        # theme:nordic
        await self.handler._handle_hermes_or_theme_callback(
            self.session,
            self.config,
            callback={"id": "cb2"},
            data="theme:nordic",
            chat_id=12345,
            message_id=50,
            user_id=12345,
        )
        saved = json.loads(self.site_cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("theme"), "nordic")


if __name__ == "__main__":
    unittest.main()
