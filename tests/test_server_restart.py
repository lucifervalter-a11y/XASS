from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks

import app.main as main
import app.services.updater as updater
from app.services.miniapp import MiniAppUser


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


class _SlowBot:
    def __init__(self) -> None:
        self.menu_started = asyncio.Event()
        self.blocker = asyncio.Event()
        self.closed = False

    async def send_message(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def set_chat_menu_button(self, *_args: object, **_kwargs: object) -> None:
        self.menu_started.set()
        await self.blocker.wait()

    async def set_my_commands(self, *_args: object, **_kwargs: object) -> None:
        await self.blocker.wait()

    async def close(self) -> None:
        self.closed = True


async def _idle_loop(_settings: object, _bot: object, stop_event: asyncio.Event) -> None:
    await stop_event.wait()


class ServerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_telegram_setup_does_not_block_http_readiness(self) -> None:
        bot = _SlowBot()
        fake_settings = SimpleNamespace(
            use_polling=False,
            profile_public_url="https://xass.example",
            profile_json_path="profile.json",
            projects_json_path="projects.json",
            site_config_json_path="site.json",
            quotes_json_path="quotes.json",
        )
        config = SimpleNamespace(service_base_url="https://xass.example")
        patches = (
            patch.object(main, "bot_client", bot),
            patch.object(main, "settings", fake_settings),
            patch.object(main, "ensure_data_dirs"),
            patch.object(main, "ensure_profile_exists"),
            patch.object(main, "ensure_projects_exists"),
            patch.object(main, "ensure_site_config_exists"),
            patch.object(main, "ensure_quotes_exists"),
            patch.object(main, "init_db", new=AsyncMock()),
            patch.object(main, "SessionLocal", side_effect=lambda: _SessionContext()),
            patch.object(main, "get_or_create_app_config", new=AsyncMock(return_value=config)),
            patch.object(main, "get_restart_notice", return_value=None),
            patch.object(main, "offline_check_loop", side_effect=_idle_loop),
        )
        for item in patches:
            item.start()
        manager = main.lifespan(None)
        try:
            started = time.perf_counter()
            await manager.__aenter__()
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.2)
            await asyncio.wait_for(bot.menu_started.wait(), timeout=1)
        finally:
            await manager.__aexit__(None, None, None)
            for item in reversed(patches):
                item.stop()
        self.assertTrue(bot.closed)

    async def test_mini_update_returns_before_scheduled_restart(self) -> None:
        before = SimpleNamespace(full_hash="a", short_hash="aaaa", subject="old")
        after = SimpleNamespace(full_hash="b", short_hash="bbbb", subject="new")
        result = SimpleNamespace(
            ok=True,
            branch="main",
            before=before,
            after=after,
            steps=["git pull"],
            restart_required=True,
            restart_performed=False,
            error=None,
        )
        background = BackgroundTasks()
        user = MiniAppUser(1, "A", "", "a", True)
        with patch.object(main, "run_update", return_value=result):
            payload = await main.mini_run_update(background, user)
        self.assertTrue(payload["restart_scheduled"])
        self.assertFalse(payload["restart_performed"])
        self.assertEqual(len(background.tasks), 1)


class UpdateStatusTests(unittest.TestCase):
    def test_fast_status_can_skip_release_api(self) -> None:
        settings = SimpleNamespace(update_branch="main")
        with (
            patch.object(updater, "_repo_root"),
            patch.object(updater, "_is_git_repo", return_value=False),
            patch.object(updater, "get_latest_release_notes", side_effect=AssertionError("release API called")),
        ):
            result = updater.get_update_status(settings, include_release_notes=False)
        self.assertFalse(result.has_updates)
        self.assertIsNone(result.release)

    def test_deployment_uses_fast_restart_and_health_gate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        backend_unit = (root / "deploy" / "systemd" / "serverredus-backend.service").read_text(encoding="utf-8")
        agent_unit = (root / "deploy" / "systemd" / "serverredus-agent.service").read_text(encoding="utf-8")
        update_script = (root / "deploy" / "update.sh").read_text(encoding="utf-8")
        self.assertIn("RestartSec=1s", backend_unit)
        self.assertIn("TimeoutStopSec=10s", backend_unit)
        self.assertIn("After=network.target serverredus-backend.service", agent_unit)
        self.assertIn("bootstrap_server_dependencies.py", update_script)
        self.assertIn("/health", update_script)


if __name__ == "__main__":
    unittest.main()
