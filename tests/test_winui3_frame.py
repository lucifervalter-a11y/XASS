from __future__ import annotations

import sys
import tkinter as tk
import unittest
from pathlib import Path

CLIENT_ROOT = Path(__file__).resolve().parents[1] / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import desktop_app
import desktop_widgets
import themes
import winui_frame


class WinUI3FrameTests(unittest.TestCase):
    def test_hex_to_colorref(self) -> None:
        self.assertEqual(winui_frame.hex_to_colorref("#141418"), 0x00181414)
        self.assertEqual(winui_frame.hex_to_colorref("#ffffff"), 0x00FFFFFF)
        self.assertEqual(winui_frame.hex_to_colorref("0x00120c09"), 0x00120c09)
        self.assertEqual(winui_frame.hex_to_colorref(0x00123456), 0x00123456)
        self.assertEqual(winui_frame.hex_to_colorref("invalid"), 0)

    def test_winui_font_system(self) -> None:
        fonts = desktop_widgets._detect_winui_fonts()
        self.assertIn("display", fonts)
        self.assertIn("text", fonts)
        self.assertIn("small", fonts)
        self.assertIn("code", fonts)

        display_font = desktop_widgets.winui_font("display", 24)
        self.assertEqual(display_font[0], fonts["display"])
        self.assertEqual(display_font[1], 24)
        self.assertEqual(display_font[2], "bold")

        body_font = desktop_widgets.winui_font("body", 11)
        self.assertEqual(body_font[0], fonts["text"])
        self.assertEqual(body_font[1], 11)

        caption_font = desktop_widgets.winui_font("caption", 8)
        self.assertEqual(caption_font[0], fonts["small"])
        self.assertEqual(caption_font[1], 8)

    def test_winui_card_and_badge_creation(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        root.withdraw()

        card = desktop_widgets.WinUICard(root, bg=desktop_widgets.CARD, radius=8)
        card.pack()
        root.update_idletasks()
        self.assertEqual(card._radius, 8)

        badge = desktop_widgets.winui_badge(card, "Active", tone="green")
        badge.pack()
        root.update_idletasks()
        self.assertEqual(badge._radius, 12)

    def test_winui_window_styling_application(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        root.withdraw()
        root.update_idletasks()

        res_window = winui_frame.apply_winui_window_styling(root, theme_name="Tokyo", backdrop="mica")
        if winui_frame.is_windows():
            self.assertTrue(res_window)

        dialog = tk.Toplevel(root)
        self.addCleanup(dialog.destroy)
        dialog.withdraw()
        dialog.update_idletasks()

        res_dialog = winui_frame.apply_winui_dialog_styling(dialog, theme_name="Cyber", backdrop="acrylic")
        if winui_frame.is_windows():
            self.assertTrue(res_dialog)

    def test_hermes_view_in_desktop_app(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        root.withdraw()

        app = desktop_app.XassDesktop(root, preview=True)
        self.assertIn("hermes", app.nav_buttons)

        # Switch to hermes view
        app.show_view("hermes")
        root.update_idletasks()
        self.assertEqual(app.current_view, "hermes")
        self.assertTrue(hasattr(app, "hermes_view_input"))
        self.assertTrue(hasattr(app, "hermes_view_output"))
        self.assertTrue(hasattr(app, "hermes_view_run_btn"))

        # Test prompt insertion
        app._set_hermes_view_prompt("Тестовый запрос WinUI 3")
        self.assertEqual(app.hermes_view_input.get(), "Тестовый запрос WinUI 3")

        # Test clearing output
        app._clear_hermes_view_output()
        out = app.hermes_view_output.text.get("1.0", "end").strip()
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
