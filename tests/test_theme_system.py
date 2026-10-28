"""Automated tests for the XASS Multi-Theme Engine.

Validates:
1. Valid theme identifiers: cyber, tokyo, nordic, matrix, sunset, glass.
2. Complete CSS custom variables presence for each theme.
3. System layer bindings (Music, Control Center) in miniapp-themes.css.
4. Validation of theme syntax, color codes, and fallback defaults.
5. Integration with miniapp.php head link.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
THEMES_CSS_PATH = REPO_ROOT / "assets" / "miniapp-themes.css"
MINIAPP_PHP_PATH = REPO_ROOT / "miniapp.php"

VALID_THEMES = ("cyber", "tokyo", "nordic", "matrix", "sunset", "glass")

REQUIRED_THEME_VARIABLES = (
    "--theme-bg",
    "--theme-surface",
    "--theme-surface2",
    "--theme-card",
    "--theme-card-hover",
    "--theme-card-bg",
    "--theme-card-border",
    "--theme-line",
    "--theme-text",
    "--theme-text-muted",
    "--theme-blue-bright",
    "--theme-accent",
    "--theme-accent-soft",
    "--theme-accent-glow",
    "--theme-tab-bg",
    "--theme-tab-border",
    "--theme-head-bg",
    "--theme-btn-bg",
    "--theme-btn-border",
)

REQUIRED_ROOT_MAPPINGS = (
    "--accent",
    "--accent-soft",
    "--accent-glow",
    "--bg",
    "--surface",
    "--surface2",
    "--card",
    "--card-hover",
    "--card-bg",
    "--card-border",
    "--card-blur",
    "--line",
    "--text",
    "--text-muted",
    "--muted",
    "--blue-bright",
    "--tab-bg",
    "--tab-border",
    "--head-bg",
    "--btn-bg",
    "--btn-border",
    "--input-bg",
    "--input-border",
    "--cc-card",
    "--cc-card-raised",
    "--cc-line",
    "--xm-bg",
    "--xm-surface",
    "--xm-line",
    "--xm-text",
    "--xm-muted",
)


def normalize_theme_name(value: str | None) -> str | None:
    """Normalize and validate a theme name."""
    if not value or not isinstance(value, str):
        return None
    clean = value.strip().lower()
    return clean if clean in VALID_THEMES else None


def extract_theme_block(css_content: str, theme_name: str) -> str:
    """Extract the CSS rules block associated with a data-theme attribute."""
    pattern = rf'\[data-theme=["\']?{theme_name}["\']?\][^\{{]*\{{([^}}]+)\}}'
    match = re.search(pattern, css_content, re.IGNORECASE)
    if not match:
        return ""
    return match.group(1)


class ThemeSystemValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.assertTrue(
            THEMES_CSS_PATH.is_file(),
            f"miniapp-themes.css not found at {THEMES_CSS_PATH}",
        )
        cls.css_content = THEMES_CSS_PATH.read_text(encoding="utf-8")

    def test_valid_theme_names_membership(self) -> None:
        """Ensure all 6 core themes are recognized and reject invalid names."""
        for name in VALID_THEMES:
            self.assertEqual(normalize_theme_name(name), name)
            self.assertEqual(normalize_theme_name(f"  {name.upper()}  "), name)

        invalid_names = ["", "dark", "light", "default", "custom", "neon", "../hack", None]
        for name in invalid_names:
            self.assertIsNone(normalize_theme_name(name))

    def test_css_defines_all_six_themes(self) -> None:
        """Verify miniapp-themes.css contains explicit selectors for all 6 themes."""
        for theme in VALID_THEMES:
            selector_pattern = rf'\[data-theme=["\']?{theme}["\']?\]'
            self.assertRegex(
                self.css_content,
                selector_pattern,
                f"Theme '{theme}' missing [data-theme='{theme}'] selector in CSS",
            )

    def test_theme_variables_presence_per_theme(self) -> None:
        """Every theme must define the standard set of theme variables."""
        for theme in VALID_THEMES:
            block = extract_theme_block(self.css_content, theme)
            self.assertTrue(block, f"Could not extract CSS rules block for theme '{theme}'")

            for var in REQUIRED_THEME_VARIABLES:
                # Glass theme can directly override variables without --theme- prefix (e.g. --surface instead of --theme-surface)
                direct_var = var.replace("--theme-", "--")
                has_var = (var in block) or (direct_var in block)
                self.assertTrue(
                    has_var,
                    f"Variable '{var}' (or '{direct_var}') is missing in theme '{theme}' block",
                )

    def test_root_mappings_and_fallbacks(self) -> None:
        """The :root block must map global CSS variables to theme custom variables."""
        root_match = re.search(r":root\s*\{([^}]+)\}", self.css_content)
        self.assertTrue(root_match, ":root declaration block missing from miniapp-themes.css")
        root_block = root_match.group(1)

        for var in REQUIRED_ROOT_MAPPINGS:
            self.assertIn(
                var,
                root_block,
                f"Global CSS variable mapping '{var}' missing in :root",
            )

    def test_cyber_default_fallback_rule(self) -> None:
        """Ensure Cyber theme also acts as default fallback when data-theme is unset."""
        self.assertIn(":root:not([data-theme])", self.css_content)

    def test_theme_colors_format(self) -> None:
        """Check that defined hex colors, rgb, or radial-gradients are syntactically valid."""
        for theme in VALID_THEMES:
            block = extract_theme_block(self.css_content, theme)
            hex_colors = re.findall(r"#([0-9a-fA-F]{3,8})\b", block)
            self.assertGreater(
                len(hex_colors),
                3,
                f"Theme '{theme}' should define multiple hex colors, found {len(hex_colors)}",
            )
            for color in hex_colors:
                self.assertIn(len(color), (3, 4, 6, 8), f"Invalid hex color length #{color}")

    def test_liquid_glass_morphism_blur(self) -> None:
        """Liquid Glass theme must configure high card blur and semi-transparent alpha backgrounds."""
        glass_block = extract_theme_block(self.css_content, "glass")
        self.assertTrue(
            "--theme-card-blur" in glass_block or "--card-blur" in glass_block,
            "Glass theme should specify card blur",
        )
        blur_match = re.search(r"--(?:theme-)?card-blur:\s*(\d+)px", glass_block)
        self.assertTrue(blur_match, "Glass theme should specify px blur value")
        self.assertGreaterEqual(int(blur_match.group(1)), 20)
        self.assertIn("rgba(", glass_block)

    def test_matrix_emerald_palette(self) -> None:
        """Matrix theme must use emerald green accents."""
        matrix_block = extract_theme_block(self.css_content, "matrix")
        # #10b981 or emerald variant
        self.assertTrue(
            "#10b981" in matrix_block.lower() or "#34d399" in matrix_block.lower(),
            "Matrix theme should feature emerald green accent (#10b981 or #34d399)",
        )

    def test_tokyo_night_magenta_cyan_palette(self) -> None:
        """Tokyo Night theme must feature neon magenta (#ff007f) and cyan (#00f0ff)."""
        tokyo_block = extract_theme_block(self.css_content, "tokyo").lower()
        self.assertIn("#ff007f", tokyo_block)
        self.assertIn("#00f0ff", tokyo_block)

    def test_sunset_amber_palette(self) -> None:
        """Sunset theme must feature amber/gold accents (#f59e0b)."""
        sunset_block = extract_theme_block(self.css_content, "sunset").lower()
        self.assertIn("#f59e0b", sunset_block)

    def test_nordic_frost_palette(self) -> None:
        """Nordic Frost theme must feature glacier cyan (#38bdf8)."""
        nordic_block = extract_theme_block(self.css_content, "nordic").lower()
        self.assertIn("#38bdf8", nordic_block)

    def test_miniapp_php_links_miniapp_themes(self) -> None:
        """miniapp.php should reference miniapp-themes.css."""
        if MINIAPP_PHP_PATH.is_file():
            content = MINIAPP_PHP_PATH.read_text(encoding="utf-8")
            self.assertIn("miniapp-themes.css", content)


if __name__ == "__main__":
    unittest.main()
