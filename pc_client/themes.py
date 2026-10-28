"""Theme definitions and palette switching for XASS PC Client."""
from __future__ import annotations

import sys
from typing import Any

# Standard theme names
THEME_CYBER = "Cyber"
THEME_TOKYO = "Tokyo"
THEME_NORDIC = "Nordic"
THEME_MATRIX = "Matrix"
THEME_CLASSIC = "Classic"

ALL_THEMES: list[str] = [
    THEME_CYBER,
    THEME_TOKYO,
    THEME_NORDIC,
    THEME_MATRIX,
    THEME_CLASSIC,
]

THEMES: dict[str, dict[str, str]] = {
    THEME_CYBER: {
        "BG": "#0d1017",
        "CARD": "#161b26",
        "HOVER": "#1f2738",
        "LINE": "#2d374d",
        "SIDEBAR": "#090c12",
        "FIELD": "#121620",
        "WELL": "#1c2333",
        "NOTE": "#121620",
        "TEXT": "#f0f6fc",
        "MUTED": "#8b949e",
        "ACCENT": "#00f0ff",
        "ACCENT_SOFT": "#5ce1e6",
        "LILAC": "#e056fd",
        "GREEN": "#00ff9f",
        "AMBER": "#fbc531",
        "RED": "#ff4757",
        "SIDEBAR_CARD": "#121622",
        "SIDEBAR_BORDER": "#232a3d",
        "NAV_FG": "#7d8590",
        "NAV_ACTIVE_BG": "#1f2738",
        "NAV_SELECTED_BG": "#1a2232",
        "DWM_CAPTION": "0x00120c09",
    },
    THEME_TOKYO: {
        "BG": "#1a1b26",
        "CARD": "#24283b",
        "HOVER": "#2f354f",
        "LINE": "#414868",
        "SIDEBAR": "#16161e",
        "FIELD": "#1f2335",
        "WELL": "#292e42",
        "NOTE": "#1f2335",
        "TEXT": "#c0caf5",
        "MUTED": "#7aa2f7",
        "ACCENT": "#bb9af7",
        "ACCENT_SOFT": "#cfbbf9",
        "LILAC": "#b4f9f8",
        "GREEN": "#9ece6a",
        "AMBER": "#e0af68",
        "RED": "#f7768e",
        "SIDEBAR_CARD": "#1c1e2b",
        "SIDEBAR_BORDER": "#343b59",
        "NAV_FG": "#7982a9",
        "NAV_ACTIVE_BG": "#2a2f47",
        "NAV_SELECTED_BG": "#23273c",
        "DWM_CAPTION": "0x001e1616",
    },
    THEME_NORDIC: {
        "BG": "#242933",
        "CARD": "#2e3440",
        "HOVER": "#3b4252",
        "LINE": "#434c5e",
        "SIDEBAR": "#1e222a",
        "FIELD": "#282d38",
        "WELL": "#3b4252",
        "NOTE": "#282d38",
        "TEXT": "#eceff4",
        "MUTED": "#d8dee9",
        "ACCENT": "#88c0d0",
        "ACCENT_SOFT": "#81a1c1",
        "LILAC": "#b48ead",
        "GREEN": "#a3be8c",
        "AMBER": "#ebcb8b",
        "RED": "#bf616a",
        "SIDEBAR_CARD": "#272c37",
        "SIDEBAR_BORDER": "#39404f",
        "NAV_FG": "#949ba8",
        "NAV_ACTIVE_BG": "#343b49",
        "NAV_SELECTED_BG": "#2c3340",
        "DWM_CAPTION": "0x002a221e",
    },
    THEME_MATRIX: {
        "BG": "#0a0f0d",
        "CARD": "#101814",
        "HOVER": "#18241e",
        "LINE": "#21332a",
        "SIDEBAR": "#060907",
        "FIELD": "#0d1410",
        "WELL": "#16221b",
        "NOTE": "#0d1410",
        "TEXT": "#e0f8eb",
        "MUTED": "#6bb286",
        "ACCENT": "#00ff66",
        "ACCENT_SOFT": "#33ff88",
        "LILAC": "#66ffcc",
        "GREEN": "#00e676",
        "AMBER": "#ffd54f",
        "RED": "#ff5252",
        "SIDEBAR_CARD": "#0e1511",
        "SIDEBAR_BORDER": "#1c2b23",
        "NAV_FG": "#5d8a6f",
        "NAV_ACTIVE_BG": "#16231b",
        "NAV_SELECTED_BG": "#121c16",
        "DWM_CAPTION": "0x00070906",
    },
    THEME_CLASSIC: {
        "BG": "#202022",
        "CARD": "#2b2b2f",
        "HOVER": "#34343a",
        "LINE": "#434f4f",
        "SIDEBAR": "#141418",
        "FIELD": "#1a1a1e",
        "WELL": "#35353f",
        "NOTE": "#1a1a1e",
        "TEXT": "#f5f5f7",
        "MUTED": "#b1b1bb",
        "ACCENT": "#829cff",
        "ACCENT_SOFT": "#9aafff",
        "LILAC": "#c495f4",
        "GREEN": "#61c554",
        "AMBER": "#efb65c",
        "RED": "#f36b76",
        "SIDEBAR_CARD": "#1c1c24",
        "SIDEBAR_BORDER": "#2e2e3a",
        "NAV_FG": "#a3a3b0",
        "NAV_ACTIVE_BG": "#2a2a34",
        "NAV_SELECTED_BG": "#24242e",
        "DWM_CAPTION": "0x00181414",
    },
}

THEME_ALIASES: dict[str, str] = {
    "classic": THEME_CLASSIC,
    "default": THEME_CLASSIC,
    "dark": THEME_CLASSIC,
    "xass": THEME_CLASSIC,
    "cyber": THEME_CYBER,
    "cyberpunk": THEME_CYBER,
    "neon": THEME_CYBER,
    "tokyo": THEME_TOKYO,
    "tokyo_night": THEME_TOKYO,
    "tokyonight": THEME_TOKYO,
    "nordic": THEME_NORDIC,
    "nord": THEME_NORDIC,
    "matrix": THEME_MATRIX,
    "hacker": THEME_MATRIX,
    "terminal": THEME_MATRIX,
}

_CURRENT_THEME = THEME_CLASSIC


def resolve_theme_name(candidate: str | None) -> str:
    """Normalize a theme name string (case-insensitive) to canonical theme name."""
    raw = str(candidate or "").strip().lower()
    if raw in THEME_ALIASES:
        return THEME_ALIASES[raw]
    for standard in ALL_THEMES:
        if standard.lower() == raw:
            return standard
    return THEME_CLASSIC


def get_theme(name: str | None = None) -> dict[str, str]:
    """Return dictionary of theme colors for given theme name."""
    canonical = resolve_theme_name(name)
    return dict(THEES_MAP := THEMES.get(canonical, THEMES[THEME_CLASSIC]))


def current_theme_name() -> str:
    """Return the active theme name."""
    return _CURRENT_THEME


def apply_theme_colors(theme_name: str | None) -> dict[str, str]:
    """Update global palette colors across desktop modules and clear caches."""
    global _CURRENT_THEME
    canonical = resolve_theme_name(theme_name)
    _CURRENT_THEME = canonical
    palette = get_theme(canonical)

    # Modules that import or use color constants
    module_names = (
        "desktop_widgets",
        "desktop_app",
        "desktop_home",
        "desktop_music",
        "desktop_sections",
        "pc_client.desktop_widgets",
        "pc_client.desktop_app",
        "pc_client.desktop_home",
        "pc_client.desktop_music",
        "pc_client.desktop_sections",
    )

    for mod_name in module_names:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for key, val in palette.items():
            if hasattr(mod, key):
                setattr(mod, key, val)
        # Also alias CARD_HOVER and ACCENT_HOVER if present
        if hasattr(mod, "CARD_HOVER") and "HOVER" in palette:
            setattr(mod, "CARD_HOVER", palette["HOVER"])
        if hasattr(mod, "ACCENT_HOVER") and "ACCENT_SOFT" in palette:
            setattr(mod, "ACCENT_HOVER", palette["ACCENT_SOFT"])

    # Clear raster caches in desktop_widgets
    for mod_name in ("desktop_widgets", "pc_client.desktop_widgets"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            for cache_fn in ("_icon_raster", "_rounded_corners"):
                fn = getattr(mod, cache_fn, None)
                if fn and hasattr(fn, "cache_clear"):
                    fn.cache_clear()

    return palette
