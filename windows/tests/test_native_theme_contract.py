"""Portable source contracts for native theme parity; no .NET SDK is required."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
PC_THEMES = ROOT / "pc_client" / "themes.py"
NATIVE = ROOT / "windows" / "Xass.Native"
CATALOG = NATIVE / "Services" / "AppearanceThemeCatalog.cs"
PREFERENCES = NATIVE / "Services" / "AppearancePreferences.cs"
APPEARANCE = NATIVE / "MainWindow.Appearance.cs"
MAIN_XAML = NATIVE / "MainWindow.xaml"
APP_XAML = NATIVE / "App.xaml"
APPEARANCE_TEST_PROJECT = ROOT / "windows" / "Appearance.Tests" / "Appearance.Tests.csproj"

XAML_NS = "{http://schemas.microsoft.com/winfx/2006/xaml}"
CANONICAL_THEME_IDS = ("classic", "cyber", "tokyo", "nordic", "matrix")
THEME_TAGS = ("system", "light", "dark", *CANONICAL_THEME_IDS)

CATALOG_TO_PC_FIELD = {
    "Background": "BG",
    "Card": "CARD",
    "Hover": "HOVER",
    "Line": "LINE",
    "Sidebar": "SIDEBAR",
    "Field": "FIELD",
    "Well": "WELL",
    "Note": "NOTE",
    "Text": "TEXT",
    "Muted": "MUTED",
    "Accent": "ACCENT",
    "AccentSoft": "ACCENT_SOFT",
    "Lilac": "LILAC",
    "Green": "GREEN",
    "Amber": "AMBER",
    "Red": "RED",
    "SidebarCard": "SIDEBAR_CARD",
    "SidebarBorder": "SIDEBAR_BORDER",
    "NavigationForeground": "NAV_FG",
    "NavigationActiveBackground": "NAV_ACTIVE_BG",
    "NavigationSelectedBackground": "NAV_SELECTED_BG",
    "DwmCaption": "DWM_CAPTION",
    "DwmBorder": "DWM_BORDER",
    "DwmText": "DWM_TEXT",
    "Backdrop": "BACKDROP",
}

PALETTE_BRUSH_KEYS = {
    "XassPageBackgroundBrush",
    "XassCardBackgroundBrush",
    "XassHoverBrush",
    "XassBorderBrush",
    "XassSidebarBrush",
    "XassFieldBrush",
    "XassWellBrush",
    "XassTextBrush",
    "XassMutedTextBrush",
    "XassNavForegroundBrush",
    "XassNavActiveBrush",
    "XassNavSelectedBrush",
}

ACCENT_BRUSH_KEYS = {
    "XassAccentFillBrush",
    "XassAccentForegroundBrush",
    "XassAccentHoverBrush",
    "XassAccentPressedBrush",
    "XassAccentTextBrush",
    "AccentButtonBackground",
    "AccentButtonBackgroundPointerOver",
    "AccentButtonBackgroundPressed",
    "AccentButtonForeground",
    "AccentButtonForegroundPointerOver",
    "AccentButtonForegroundPressed",
    "AccentButtonBorderBrush",
    "AccentButtonBorderBrushPointerOver",
    "AccentButtonBorderBrushPressed",
}


def _literal(node: ast.AST, names: dict[str, object]) -> object:
    """Evaluate the data-only subset used by pc_client/themes.py."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return names[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        values = [_literal(item, names) for item in node.elts]
        return values if isinstance(node, ast.List) else tuple(values)
    if isinstance(node, ast.Dict):
        return {
            _literal(key, names): _literal(value, names)
            for key, value in zip(node.keys, node.values, strict=True)
        }
    raise AssertionError(f"unsupported data expression in themes.py: {ast.dump(node)}")


def _load_pc_contract() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    tree = ast.parse(PC_THEMES.read_text(encoding="utf-8"), filename=str(PC_THEMES))
    values: dict[str, object] = {}
    for statement in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign):
            target, value = statement.target, statement.value
        if isinstance(target, ast.Name) and value is not None:
            try:
                values[target.id] = _literal(value, values)
            except (AssertionError, KeyError):
                # Executable state/functions are intentionally outside this source contract.
                continue
    themes = values.get("THEMES")
    aliases = values.get("THEME_ALIASES")
    if not isinstance(themes, dict) or not isinstance(aliases, dict):
        raise AssertionError("THEMES and THEME_ALIASES must remain statically inspectable")
    return themes, aliases


def _balanced_contents(source: str, opening_paren: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    for position in range(opening_paren, len(source)):
        character = source[position]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return source[opening_paren + 1 : position]
    raise AssertionError("unbalanced C# argument list")


def _split_csharp_arguments(arguments: str) -> list[str]:
    result: list[str] = []
    start = 0
    depth = 0
    in_string = False
    escaped = False
    for position, character in enumerate(arguments):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "," and depth == 0:
            result.append(arguments[start:position].strip())
            start = position + 1
    result.append(arguments[start:].strip())
    return result


def _csharp_value(token: str) -> str | int:
    token = token.strip()
    if token.startswith('"') and token.endswith('"'):
        return json.loads(token)
    integer = re.fullmatch(r"0[xX]([0-9a-fA-F]+)(?:[uUlL]+)?", token)
    if integer:
        return int(integer.group(1), 16)
    raise AssertionError(f"catalog value must be a literal, got {token!r}")


def _load_native_catalog() -> tuple[dict[str, dict[str, str | int]], dict[str, str], str]:
    source = CATALOG.read_text(encoding="utf-8")
    record = re.search(
        r"record\s+AppearanceThemePalette\s*\((?P<fields>.*?)\)\s*;",
        source,
        flags=re.DOTALL,
    )
    if record is None:
        raise AssertionError("AppearanceThemePalette positional record is missing")
    fields = re.findall(r"\b(?:string|uint)\s+(\w+)\b", record.group("fields"))
    constants = {
        match.group("symbol"): match.group("value")
        for match in re.finditer(
            r"public\s+const\s+string\s+(?P<symbol>\w+)\s*=\s*\"(?P<value>[^\"]+)\"\s*;",
            source,
        )
    }

    palettes: dict[str, dict[str, str | int]] = {}
    for symbol, theme_id in constants.items():
        if theme_id not in CANONICAL_THEME_IDS:
            continue
        entry = re.search(rf"\[\s*{re.escape(symbol)}\s*\]\s*=\s*new\s*\(", source)
        if entry is None:
            raise AssertionError(f"catalog entry for {theme_id!r} is missing")
        opening = source.find("(", entry.start())
        arguments = [_csharp_value(item) for item in _split_csharp_arguments(_balanced_contents(source, opening))]
        if len(arguments) != len(fields):
            raise AssertionError(
                f"catalog entry {theme_id!r} has {len(arguments)} values for {len(fields)} fields"
            )
        palettes[theme_id] = dict(zip(fields, arguments, strict=True))

    aliases: dict[str, str] = {}
    for match in re.finditer(
        r"\[\s*\"(?P<alias>[^\"]+)\"\s*\]\s*=\s*(?P<target>\w+)\s*,",
        source,
    ):
        target = match.group("target")
        if target in constants:
            aliases[match.group("alias").lower()] = constants[target]
    return palettes, aliases, source


def _xaml_keys(path: Path) -> set[str]:
    root = ET.parse(path).getroot()
    return {
        element.get(XAML_NS + "Key", "")
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "SolidColorBrush"
    }


class NativeThemeContractTests(unittest.TestCase):
    def test_all_native_palette_values_match_pc_client_canonical_source(self):
        pc_themes, _ = _load_pc_contract()
        native, _, _ = _load_native_catalog()
        expected_names = {name.lower(): name for name in pc_themes}
        self.assertEqual(set(native), set(CANONICAL_THEME_IDS))
        self.assertEqual(set(expected_names), set(CANONICAL_THEME_IDS))
        for theme_id in CANONICAL_THEME_IDS:
            with self.subTest(theme=theme_id):
                palette = native[theme_id]
                canonical_name = expected_names[theme_id]
                pc_palette = pc_themes[canonical_name]
                self.assertEqual(palette.get("Name"), canonical_name)
                self.assertEqual(set(palette), {"Name", *CATALOG_TO_PC_FIELD})
                self.assertEqual(set(pc_palette), set(CATALOG_TO_PC_FIELD.values()))
                for native_field, pc_field in CATALOG_TO_PC_FIELD.items():
                    actual = palette[native_field]
                    expected = pc_palette[pc_field]
                    if native_field.startswith("Dwm"):
                        actual = f"0x{actual:08x}"
                        expected = expected.lower()
                    self.assertEqual(actual, expected, f"{theme_id}.{native_field}")

    def test_all_legacy_aliases_match_except_reserved_dark(self):
        _, pc_aliases = _load_pc_contract()
        _, native_aliases, source = _load_native_catalog()
        expected = {
            alias.lower(): target.lower()
            for alias, target in pc_aliases.items()
            if alias.lower() != "dark"
        }
        self.assertEqual(native_aliases, expected)
        self.assertEqual(pc_aliases["dark"], "Classic")
        self.assertNotIn("dark", native_aliases)
        normalize = source[source.index("NormalizeThemeName"):source.index("IsPreset")]
        self.assertRegex(
            normalize,
            r'raw\s+is\s+"system"\s+or\s+"light"\s+or\s+"dark"\s*\)\s*return\s+raw\s*;',
        )

    def test_backdrop_remains_visible_and_exact_dwm_border_is_applied(self):
        source = APPEARANCE.read_text(encoding="utf-8")
        self.assertIn("palette.Backdrop", source)
        self.assertIn("new DesktopAcrylicBackdrop()", source)
        self.assertIn("new MicaBackdrop { Kind = MicaKind.BaseAlt }", source)
        self.assertRegex(
            source,
            r"RootGrid\.Background\s*=\s*highContrast\s*\|\|\s*SystemBackdrop\s+is\s+null",
        )
        self.assertIn('DllImport("dwmapi.dll", EntryPoint = "DwmSetWindowAttribute")', source)
        self.assertIn("palette.DwmBorder", source)
        self.assertIn("DwmColorDefault", source)

    def test_theme_selector_has_eight_stable_tags_and_accessible_names(self):
        source = APPEARANCE.read_text(encoding="utf-8")
        choices = source[
            source.index("InitializeAppearanceChoices") : source.index("private static string SelectedTag")
        ]
        constants = {
            match.group("symbol"): match.group("value")
            for match in re.finditer(
                r"public\s+const\s+string\s+(?P<symbol>\w+)\s*=\s*\"(?P<value>[^\"]+)\"\s*;",
                CATALOG.read_text(encoding="utf-8"),
            )
        }
        tags: list[str] = []
        for match in re.finditer(r"\(\s*id\s*:\s*(?P<id>[^,]+),\s*label\s*:", choices):
            expression = match.group("id").strip()
            if expression.startswith('"'):
                tags.append(json.loads(expression))
            else:
                symbol = expression.rsplit(".", 1)[-1]
                self.assertIn(symbol, constants)
                tags.append(constants[symbol])
        self.assertEqual(tuple(tags), THEME_TAGS)
        self.assertRegex(
            choices,
            r"new\s+ComboBoxItem\s*\{[^}]*\bTag\s*=\s*choice\s*\.\s*id\b[^}]*\}",
        )
        self.assertRegex(
            choices,
            r"AutomationProperties\s*\.\s*SetName\s*\(\s*item\s*,\s*choice\s*\.\s*accessible\s*\)",
        )
        self.assertRegex(
            choices,
            r"AutomationProperties\s*\.\s*SetName\s*\(\s*AppearanceTheme\s*,\s*\"[^\"]+\"\s*\)",
        )
        self.assertRegex(
            choices,
            r"AutomationProperties\s*\.\s*SetHelpText\s*\(\s*AppearanceTheme\s*,",
        )

    def test_portable_appearance_tests_compile_the_catalog(self):
        project = ET.parse(APPEARANCE_TEST_PROJECT).getroot()
        links = {
            (node.get("Include", "").replace("\\", "/"), node.get("Link", ""))
            for node in project.findall(".//Compile")
        }
        self.assertIn(
            ("../Xass.Native/Services/AppearanceThemeCatalog.cs", "AppearanceThemeCatalog.cs"),
            links,
        )

    def test_palette_and_existing_accent_brushes_are_declared(self):
        keys = _xaml_keys(APP_XAML)
        self.assertTrue(PALETTE_BRUSH_KEYS <= keys, PALETTE_BRUSH_KEYS - keys)
        self.assertTrue(ACCENT_BRUSH_KEYS <= keys, ACCENT_BRUSH_KEYS - keys)

    def test_preset_custom_accent_and_high_contrast_paths_remain_explicit(self):
        source = APPEARANCE.read_text(encoding="utf-8")
        self.assertRegex(source, r"AppearanceThemeCatalog\s*\.\s*TryGetPalette\s*\(")
        self.assertRegex(source, r"AccentPalette\s*\.\s*TryParse\s*\(\s*appearance\s*\.\s*Accent")
        self.assertRegex(source, r"\bpalette\s*\.\s*Accent\b")
        self.assertRegex(source, r"appearanceAccessibility\s*\.\s*HighContrast")
        self.assertRegex(source, r"UIElementType\s*\.\s*Highlight\b")
        self.assertRegex(source, r"UIElementType\s*\.\s*HighlightText\b")
        for key in PALETTE_BRUSH_KEYS:
            with self.subTest(brush=key):
                self.assertIn(key, source)

    def test_normalized_theme_and_custom_accent_are_persisted(self):
        preferences = PREFERENCES.read_text(encoding="utf-8")
        appearance = APPEARANCE.read_text(encoding="utf-8")
        self.assertRegex(
            preferences,
            r"AppearanceThemeCatalog\s*\.\s*NormalizeThemeName\s*\(\s*Theme\s*\)",
        )
        self.assertRegex(preferences, r"theme\s*=\s*clean\s*\.\s*Theme")
        self.assertRegex(preferences, r"accent\s*=\s*clean\s*\.\s*Accent")
        self.assertRegex(preferences, r"schema\s*!=\s*AppearancePreferences\s*\.\s*SchemaVersion")
        self.assertRegex(appearance, r"appearanceStore\s*\.\s*Save\s*\(\s*appearance\s*\)")


if __name__ == "__main__":
    unittest.main()
