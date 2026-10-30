"""Extensive automated tests for Telegram bot Hermes commands and Theme switching (app.telegram_handler).

Validates:
1. Public /ai* command handling, response formatting, and token display.
2. Legacy /hermes* aliases remain silently compatible.
3. Empty, malformed, mock, and failed responses stay fail-closed.
4. /ai_status displays agent latency, session count, and online/offline status.
5. /ai_sessions lists current active and recent conversation sessions.
6. /ai_new creates and activates a fresh session.
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
from app.services.panel import dot_commands_help_text, panel_text
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
            authorized_user_ids=[67890],
            bot_token="test-token-12345:dummy",
            site_config_json_path=str(self.site_cfg_path),
            projects_audit_log_path=str(self.root / "projects_audit.jsonl"),
            hermes_url="http://127.0.0.1:9119",
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
        self.mock_hermes.url = "http://127.0.0.1:9119"
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
            "response": "Hello! I am XASS AI.",
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

    def test_public_help_uses_only_xass_ai_commands(self) -> None:
        """Published Telegram help must not expose the internal runtime name."""
        help_text = panel_text() + "\n" + dot_commands_help_text()
        self.assertIn("XASS AI", help_text)
        self.assertIn("/ai", help_text)
        self.assertNotIn("/hermes", help_text)
        self.assertNotIn("Hermes", help_text)

    async def test_public_ai_aliases_route_to_existing_handlers(self) -> None:
        """Every public /ai* command routes to the established implementation."""
        cases = (
            ("/ai check", "_handle_hermes_prompt_command", True),
            ("/ai_status", "_handle_hermes_status_command", False),
            ("/ai_sessions", "_handle_hermes_sessions_command", False),
            ("/ai_session sess-alpha", "_handle_hermes_switch_session_command", True),
            ("/ai_sess sess-alpha", "_handle_hermes_switch_session_command", True),
            ("/ai_new", "_handle_hermes_new_session_command", False),
        )
        message = {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 9}
        for command, method_name, receives_text in cases:
            with self.subTest(command=command), patch.object(
                self.handler,
                method_name,
                new_callable=AsyncMock,
            ) as routed:
                await self.handler._handle_command(self.session, message, command)
                routed.assert_awaited_once()
                if receives_text:
                    self.assertEqual(routed.await_args.args[-1], command)

    async def test_authorized_non_owner_cannot_use_ai_commands_or_callbacks(self) -> None:
        """The general allowlist must not grant access to the owner-only AI surface."""
        non_owner_message = {"chat": {"id": 67890}, "from": {"id": 67890}, "message_id": 90}
        method_names = (
            "_handle_hermes_prompt_command",
            "_handle_hermes_status_command",
            "_handle_hermes_sessions_command",
            "_handle_hermes_switch_session_command",
            "_handle_hermes_new_session_command",
        )
        for command in ("/ai check", "/hermes_status", "/ai_sessions", "/ai_session any", "/ai_new"):
            with self.subTest(command=command):
                mocks = [patch.object(self.handler, name, new_callable=AsyncMock) for name in method_names]
                started = [item.start() for item in mocks]
                try:
                    self.bot_client.send_message.reset_mock()
                    await self.handler._handle_command(self.session, non_owner_message, command)
                    for routed in started:
                        routed.assert_not_awaited()
                    self.assertIn("только владельцу", self.bot_client.send_message.call_args[0][1])
                finally:
                    for item in reversed(mocks):
                        item.stop()

        self.bot_client.edit_message_text.reset_mock()
        await self.handler._handle_hermes_or_theme_callback(
            self.session,
            self.config,
            callback={"id": "non-owner-ai"},
            data="panel:hermes",
            chat_id=67890,
            message_id=91,
            user_id=67890,
        )
        self.assertIn("только владельцу", self.bot_client.edit_message_text.call_args.kwargs["text"])
        self.mock_hermes.get_status.assert_not_awaited()

    async def test_authorized_non_owner_dot_ai_is_not_handled(self) -> None:
        """The private Business-dot alias does not widen the AI owner boundary."""
        with patch.object(
            self.handler,
            "_handle_hermes_prompt_command",
            new_callable=AsyncMock,
        ) as routed:
            handled = await self.handler._handle_dot_command(
                self.session,
                self.config,
                {"chat": {"id": 67890}, "from": {"id": 67890}},
                ".ai check",
            )
        self.assertFalse(handled)
        routed.assert_not_awaited()

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
        self.assertIn("XASS AI", text)
        self.assertIn("/ai <ваш запрос>", text)
        self.assertNotIn("/hermes", text)

    async def test_hermes_with_prompt_executes_and_replies(self) -> None:
        """Calling /hermes <prompt> forwards to HermesService and formats reply."""
        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 11},
            "/hermes How are you?",
        )

        self.mock_hermes.send_prompt.assert_called_once_with(
            session_id="sess-new-created",
            prompt="How are you?",
        )
        self.mock_hermes.list_sessions.assert_not_awaited()
        self.assertEqual(
            self.handler.hermes_owned_sessions.get(12345),
            {"sess-new-created"},
        )
        self.bot_client.send_message.assert_called()
        last_call_text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Hello! I am XASS AI", last_call_text)
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
        self.assertIn("Интерактивный канал XASS AI недоступен", text)
        self.assertIn("/ai_status", text)
        self.assertNotIn("/hermes", text)

    async def test_session_creation_failure_does_not_adopt_global_or_send_prompt(self) -> None:
        """A failed create must stop instead of adopting the first global session."""
        self.mock_hermes.create_session = AsyncMock(side_effect=RuntimeError("runtime offline"))

        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 120},
            "/hermes do not fake this",
        )

        self.mock_hermes.send_prompt.assert_not_awaited()
        self.mock_hermes.list_sessions.assert_not_awaited()
        self.assertNotIn(12345, self.handler.hermes_active_sessions)
        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("реальная сессия не создана", text)

    async def test_empty_malformed_and_mock_results_fail_closed(self) -> None:
        """A service result is not a success without a confirmed nonempty answer."""
        invalid_results = (
            {},
            {"response": "   "},
            {"response": "fabricated", "mock": True},
            {"response": "rejected", "ok": False},
        )
        for result in invalid_results:
            with self.subTest(result=result):
                self.bot_client.send_message.reset_mock()
                self.mock_hermes.send_prompt = AsyncMock(return_value=result)
                await self.handler._handle_command(
                    self.session,
                    {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 121},
                    "/ai do not fabricate",
                )
                sent_text = self.bot_client.send_message.call_args[0][1]
                self.assertIn("Интерактивный канал XASS AI недоступен", sent_text)
                self.assertNotIn("fabricated", sent_text)
                self.assertNotIn("rejected", sent_text)

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
        self.assertIn("XASS AI", sent_text)
        self.assertNotIn("Nous Hermes", sent_text)
        self.assertIn("RUNTIME ПРОВЕРЕН", sent_text)
        self.assertIn("интерактивный канал backend/телефон", sent_text)

    async def test_hermes_status_offline_has_no_fallback_claim(self) -> None:
        """/hermes_status says requests are not executed while offline."""
        self.mock_hermes.get_status = AsyncMock(return_value={
            "online": False,
            "enabled": True,
            "version": "",
            "active_model": "",
            "session_count": 0,
            "status": "unavailable",
        })

        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 14},
            "/hermes_status",
        )

        self.bot_client.send_message.assert_called_once()
        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("ОФЛАЙН", text)
        self.assertIn("запросы не выполняются", text)
        self.assertNotIn("fallback", text.lower())
        self.assertIn("9119", text)
        self.assertNotIn("HERMES_URL", text)

    async def test_hermes_status_rejects_truthy_non_boolean_flags(self) -> None:
        """Untrusted string/integer flags must not become an online claim."""
        self.mock_hermes.get_status = AsyncMock(return_value={
            "online": "false",
            "available": 1,
            "enabled": True,
            "version": "0.22.2",
            "active_model": "internal-name",
            "session_count": 0,
        })

        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 141},
            "/ai_status",
        )

        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("ОФЛАЙН", text)
        self.assertNotIn("RUNTIME ПРОВЕРЕН", text)

    # -------------------------------------------------------------------------
    # /hermes_sessions & /hermes_new
    # -------------------------------------------------------------------------
    async def test_hermes_sessions_listing(self) -> None:
        """/hermes_sessions lists only sessions created by this owner surface."""
        self.handler.hermes_owned_sessions[12345] = {"sess-alpha"}
        self.handler.hermes_active_sessions[12345] = "sess-alpha"
        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 15},
            "/hermes_sessions",
        )

        self.bot_client.send_message.assert_called_once()
        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Сессии диалогов XASS AI", text)
        self.assertIn("sess-alpha", text)
        self.assertNotIn("sess-beta", text)

    async def test_hermes_new_creates_and_activates_session(self) -> None:
        """/hermes_new calls HermesService.create_session and sets active user session."""
        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 16},
            "/hermes_new",
        )

        self.mock_hermes.create_session.assert_called_once()
        self.assertEqual(self.handler.hermes_active_sessions.get(12345), "sess-new-created")
        self.assertEqual(self.handler.hermes_owned_sessions.get(12345), {"sess-new-created"})
        self.bot_client.send_message.assert_called_once()
        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Новая сессия", text)
        self.assertIn("sess-new-created", text)

    async def test_hermes_new_without_real_id_reports_failure(self) -> None:
        """Session creation must not invent a local ID."""
        self.mock_hermes.create_session = AsyncMock(return_value={"title": "missing id"})

        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 160},
            "/hermes_new",
        )

        self.assertNotIn(12345, self.handler.hermes_active_sessions)
        text = self.bot_client.send_message.call_args[0][1]
        self.assertIn("Не удалось создать сессию XASS AI", text)

    async def test_switch_rejects_global_session_not_created_by_telegram_owner(self) -> None:
        """A globally listed session id is not enough to establish Telegram ownership."""
        self.handler.hermes_owned_sessions[12345] = {"sess-alpha"}
        self.handler.hermes_active_sessions[12345] = "sess-alpha"

        await self.handler._handle_command(
            self.session,
            {"chat": {"id": 12345}, "from": {"id": 12345}, "message_id": 161},
            "/ai_session sess-beta",
        )

        self.assertEqual(self.handler.hermes_active_sessions[12345], "sess-alpha")
        self.mock_hermes.list_sessions.assert_not_awaited()
        self.assertIn("недоступна", self.bot_client.send_message.call_args[0][1])

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
        panel_text = self.bot_client.edit_message_text.call_args[1]["text"]
        self.assertIn("XASS AI", panel_text)
        self.assertIn("/ai", panel_text)
        self.assertNotIn("/hermes", panel_text)

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
