from __future__ import annotations

import inspect
import sys
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

CLIENT_ROOT = Path(__file__).resolve().parents[1] / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import desktop_app
import desktop_sections
import hermes_client
import themes


class DesktopHermesTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = desktop_app.XassDesktop(self.root, preview=True)

    def tearDown(self) -> None:
        themes.apply_theme_colors(themes.THEME_CLASSIC)

    def test_theme_switch(self) -> None:
        self.app.set_theme("Matrix", save=False)
        self.assertEqual(self.app.theme_var.get(), "Matrix")
        self.assertEqual(themes.current_theme_name(), "Matrix")

        self.app.set_theme("Nordic", save=False)
        self.assertEqual(self.app.theme_var.get(), "Nordic")
        self.assertEqual(themes.current_theme_name(), "Nordic")

    def test_tray_hermes_status_text(self) -> None:
        self.app._hermes_online = False
        self.assertEqual(self.app._tray_hermes_status_text(), "Статус: Не в сети")

        self.app._hermes_online = True
        self.assertEqual(self.app._tray_hermes_status_text(), "Статус: В сети")

    def test_hermes_prompt_dialog_creation(self) -> None:
        dialog = desktop_app.HermesPromptDialog(self.app)
        self.addCleanup(dialog.destroy)
        self.root.update_idletasks()

        self.assertEqual(dialog.title(), "XASS AI — Запрос")
        self.assertEqual(hermes_client.DEFAULT_HERMES_URL, "http://127.0.0.1:9119")
        self.assertIsNotNone(dialog.prompt_entry)
        self.assertIsNotNone(dialog.run_btn)
        self.assertIsNotNone(dialog.output_text)

        # Test applying a result
        mock_result = {
            "ok": True,
            "prompt": "Тестовая задача",
            "response": "Ответ получен успешно",
            "latency_ms": 42.0,
        }
        dialog._apply_result(mock_result)
        out = dialog.output_text.text.get("1.0", "end")
        self.assertIn("Ответ получен успешно", out)
        self.assertIn("42", out)

    def test_failed_prompt_is_not_displayed_as_success(self) -> None:
        dialog = desktop_app.HermesPromptDialog(self.app)
        self.addCleanup(dialog.destroy)
        self.root.update_idletasks()

        dialog._apply_result({
            "ok": False,
            "online": True,
            "error": "Безопасный канал команд недоступен",
            "url": "http://127.0.0.1:9119",
        })
        out = dialog.output_text.text.get("1.0", "end")
        self.assertIn("Безопасный канал команд недоступен", out)
        self.assertIn("http://127.0.0.1:9119", out)
        self.assertNotIn("Завершено за", out)

    def test_dialog_rejects_truthy_non_boolean_result_flags(self) -> None:
        dialog = desktop_app.HermesPromptDialog(self.app)
        self.addCleanup(dialog.destroy)
        self.root.update_idletasks()

        dialog._apply_result({
            "ok": "false",
            "response": "must not be accepted",
            "error": "Некорректный ответ",
            "url": "http://127.0.0.1:9119",
        })
        out = dialog.output_text.text.get("1.0", "end")
        self.assertIn("Некорректный ответ", out)
        self.assertNotIn("must not be accepted", out)

        dialog._apply_status({"online": "false", "url": "http://127.0.0.1:9119"})
        self.assertFalse(self.app._hermes_online)

    def test_open_webui_uses_current_validated_loopback_field(self) -> None:
        self.app.hermes_url_var.set("http://localhost:9123")
        with patch.object(desktop_app.webbrowser, "open") as browser_open:
            self.app.open_hermes_webui()
        browser_open.assert_called_once_with("http://localhost:9123")

    def test_external_webui_is_not_exposed_by_primary_ui_tray_or_dialog(self) -> None:
        exposed_builders = (
            desktop_app.XassDesktop._build_hermes,
            desktop_app.XassDesktop._build_settings,
            desktop_app.XassDesktop._setup_tray,
            desktop_sections.build_commands,
            desktop_app.HermesPromptDialog.__init__,
        )
        for builder in exposed_builders:
            with self.subTest(builder=builder.__qualname__):
                source = inspect.getsource(builder)
                self.assertNotIn("open_hermes_webui", source)
                self.assertNotIn("Web UI", source)
                self.assertNotIn("WebUI", source)

    def test_main_prompt_caller_uses_the_supported_keyword(self) -> None:
        source = Path(desktop_app.__file__).read_text(encoding="utf-8")
        self.assertIn("send_hermes_prompt(prompt, hermes_url=url)", source)
        self.assertNotIn("send_hermes_prompt(prompt, daemon_url=url)", source)


if __name__ == "__main__":
    unittest.main()
