from __future__ import annotations

import tkinter as tk
import unittest
from types import SimpleNamespace

from pc_client.desktop_widgets import (
    ACCENT, BG, CARD, HOVER, ModernButton, NavButton, RoundedPanel, _icon_raster, rounded_image,
)


class DecorationTests(unittest.TestCase):
    def test_rounded_decoration_preserves_transparency_and_fill(self):
        image = rounded_image(120, 60, fill=CARD, radius=12, border_color=ACCENT)
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertEqual(image.getpixel((60, 30)), (43, 43, 47, 255))
        self.assertTrue(any(0 < alpha < 255 for alpha in image.getchannel("A").getdata()))

    def test_every_navigation_and_status_icon_has_visible_monoline_pixels(self):
        for name in ("home", "monitor", "link", "folder", "archive", "journal", "update", "settings",
                     "terminal", "cpu", "memory", "disk", "chevron", "globe", "shield", "play", "pause"):
            with self.subTest(name=name):
                icon = _icon_raster(name, 24, ACCENT)
                self.assertEqual(icon.size, (24, 24))
                self.assertIsNotNone(icon.getbbox())
                self.assertEqual(icon.getpixel((0, 0))[3], 0)


class NativeWidgetTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.root.geometry("650x420")
        self.root.configure(bg=BG)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.addCleanup(self.root.destroy)

    def render(self):
        self.root.update_idletasks()
        self.assertEqual(self.errors, [])

    def test_panel_supports_direct_pack_and_grid_without_hidden_content_wrappers(self):
        panel = RoundedPanel(self.root, padx=17, pady=15)
        panel.pack(fill="both", expand=True)
        label = tk.Label(panel, text="Panel content", bg=panel.cget("bg"))
        label.pack()
        nested = RoundedPanel(panel, padx=8, pady=8)
        nested.pack(fill="x")
        tk.Label(nested, text="Grid child", bg=nested.cget("bg")).grid(row=0, column=0)
        self.render()
        self.assertEqual(panel.cget("bg"), CARD)
        self.assertEqual(label.master, panel)
        self.assertGreater(panel.winfo_reqwidth(), label.winfo_reqwidth())
        panel.configure(bg=HOVER)
        self.render()
        self.assertEqual(panel["bg"], HOVER)

    def test_button_configure_invocation_disabled_and_cancelled_click(self):
        calls = []
        button = ModernButton(self.root, "Connect", lambda: calls.append("clicked"), icon="link")
        button.pack()
        self.render()

        button.invoke()
        button._key_press(None)
        button._key_release(None)
        self.assertEqual(len(calls), 2)
        button.configure(state="disabled", text="Connecting…", bg=ACCENT, pady=12)
        button.invoke()
        button._key_press(None)
        button._key_release(None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(button.cget("state"), "disabled")
        self.assertEqual(button.cget("text"), "Connecting…")
        button.configure(state="normal")
        button._press(None)
        button._release(SimpleNamespace(x=-1, y=-1))
        self.assertEqual(len(calls), 2)
        self.render()

    def test_button_explicit_parent_background_controls_transparent_corners(self):
        button = ModernButton(self.root, "Hero action", bg=ACCENT, parent_bg="#000000")
        button.pack()
        self.render()
        self.assertEqual(tk.Canvas.cget(button, "background"), "#000000")
        self.assertEqual(button.cget("bg"), ACCENT)
        button.configure(parent_bg=BG)
        self.assertEqual(tk.Canvas.cget(button, "background"), BG)

    def test_nav_selection_and_variable_trace_cleanup(self):
        variable = tk.StringVar(self.root, value="Главный экран")
        nav = NavButton(self.root, textvariable=variable, icon="home")
        nav.pack()
        nav.set_active(True)
        self.assertTrue(nav.cget("active"))
        self.assertEqual(nav.cget("bg"), HOVER)
        nav.configure(active=False)
        self.assertFalse(nav.cget("active"))
        variable.set("Подключение")
        self.render()
        self.assertEqual(nav._text(), "Подключение")
        nav.destroy()
        variable.set("No destroyed-widget callback")
        self.render()


if __name__ == "__main__":
    unittest.main()
