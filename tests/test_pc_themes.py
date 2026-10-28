from __future__ import annotations

import sys
import unittest
from pathlib import Path

CLIENT_ROOT = Path(__file__).resolve().parents[1] / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import themes
import desktop_widgets


class PcThemesTests(unittest.TestCase):
    def test_standard_themes_defined(self) -> None:
        expected = ["Cyber", "Tokyo", "Nordic", "Matrix", "Classic"]
        self.assertEqual(themes.ALL_THEMES, expected)
        for name in expected:
            palette = themes.get_theme(name)
            self.assertIn("BG", palette)
            self.assertIn("CARD", palette)
            self.assertIn("HOVER", palette)
            self.assertIn("LINE", palette)
            self.assertIn("SIDEBAR", palette)
            self.assertIn("TEXT", palette)
            self.assertIn("ACCENT", palette)
            self.assertIn("GREEN", palette)
            self.assertIn("RED", palette)
            self.assertTrue(palette["BG"].startswith("#"))
            self.assertTrue(palette["ACCENT"].startswith("#"))

    def test_resolve_theme_name_aliases(self) -> None:
        self.assertEqual(themes.resolve_theme_name("cyber"), themes.THEME_CYBER)
        self.assertEqual(themes.resolve_theme_name("CYBERPUNK"), themes.THEME_CYBER)
        self.assertEqual(themes.resolve_theme_name("tokyo_night"), themes.THEME_TOKYO)
        self.assertEqual(themes.resolve_theme_name("nord"), themes.THEME_NORDIC)
        self.assertEqual(themes.resolve_theme_name("matrix"), themes.THEME_MATRIX)
        self.assertEqual(themes.resolve_theme_name("classic"), themes.THEME_CLASSIC)
        self.assertEqual(themes.resolve_theme_name("unknown_theme"), themes.THEME_CLASSIC)

    def test_apply_theme_colors_updates_modules(self) -> None:
        themes.apply_theme_colors("Matrix")
        self.assertEqual(themes.current_theme_name(), themes.THEME_MATRIX)
        self.assertEqual(desktop_widgets.BG, themes.THEMES[themes.THEME_MATRIX]["BG"])
        self.assertEqual(desktop_widgets.ACCENT, themes.THEMES[themes.THEME_MATRIX]["ACCENT"])

        themes.apply_theme_colors("Tokyo")
        self.assertEqual(themes.current_theme_name(), themes.THEME_TOKYO)
        self.assertEqual(desktop_widgets.BG, themes.THEMES[themes.THEME_TOKYO]["BG"])
        self.assertEqual(desktop_widgets.ACCENT, themes.THEMES[themes.THEME_TOKYO]["ACCENT"])

        themes.apply_theme_colors("Cyber")
        self.assertEqual(themes.current_theme_name(), themes.THEME_CYBER)
        self.assertEqual(desktop_widgets.BG, themes.THEMES[themes.THEME_CYBER]["BG"])
        self.assertEqual(desktop_widgets.ACCENT, themes.THEMES[themes.THEME_CYBER]["ACCENT"])


if __name__ == "__main__":
    unittest.main()
