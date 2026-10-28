from __future__ import annotations

import sys
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

CLIENT_ROOT = Path(__file__).resolve().parents[1] / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import desktop_app
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


if __name__ == "__main__":
    unittest.main()
