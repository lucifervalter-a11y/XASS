"""WinUI 3 native styling, DWM materials (Mica, Mica Alt, Acrylic), and window framing for Windows."""
from __future__ import annotations

import ctypes
import os
import platform
import sys
import tkinter as tk
from typing import Any

try:
    import pywinstyles
except ImportError:
    pywinstyles = None

try:
    from themes import get_theme, resolve_theme_name
except ModuleNotFoundError:
    try:
        from pc_client.themes import get_theme, resolve_theme_name
    except ModuleNotFoundError:
        def resolve_theme_name(x):
            return "Classic"
        def get_theme(x=None):
            return {"BG": "#202022", "CARD": "#2b2b2f", "SIDEBAR": "#141418", "TEXT": "#f5f5f7", "LINE": "#43434f", "DWM_CAPTION": "0x00181414"}

# DWM Window Attributes (Win32 & Windows 11 App SDK / WinUI 3)
DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWA_MICA_EFFECT = 1029

# Corner preferences (DWM_WINDOW_CORNER_PREFERENCE)
DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3

# Backdrop types (DWM_SYSTEMBACKDROP_TYPE)
DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2       # Mica
DWMSBT_TRANSIENTWINDOW = 3  # Acrylic
DWMSBT_TABBEDWINDOW = 4     # Mica Alt (Tabbed window - standard for NavigationView apps)

BACKDROP_MAP = {
    "auto": DWMSBT_AUTO,
    "none": DWMSBT_NONE,
    "mica": DWMSBT_MAINWINDOW,
    "mica_alt": DWMSBT_TABBEDWINDOW,
    "tabbed": DWMSBT_TABBEDWINDOW,
    "acrylic": DWMSBT_TRANSIENTWINDOW,
    "dark": DWMSBT_NONE,
}


def is_windows() -> bool:
    return os.name == "nt"


def windows_build_number() -> int:
    if not is_windows():
        return 0
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0


def is_windows_11() -> bool:
    return is_windows() and windows_build_number() >= 22000


def has_winui3_backdrop_support() -> bool:
    """Windows 11 Build 22621 (22H2) and later support DWMWA_SYSTEMBACKDROP_TYPE = 38."""
    return is_windows() and windows_build_number() >= 22621


def hex_to_colorref(hex_color: str | int) -> int:
    """Convert #RRGGBB or 0x00BBGGRR or color name to Win32 COLORREF (0x00BBGGRR)."""
    if isinstance(hex_color, int):
        return hex_color
    raw = str(hex_color).strip()
    if raw.startswith("0x") or raw.startswith("0X"):
        try:
            return int(raw, 16)
        except ValueError:
            return 0
    clean = raw.lstrip("#")
    if len(clean) == 6:
        try:
            r = int(clean[0:2], 16)
            g = int(clean[2:4], 16)
            b = int(clean[4:6], 16)
            return (b << 16) | (g << 8) | r
        except ValueError:
            return 0
    elif len(clean) == 8:
        # #AARRGGBB or #RRGGBBAA
        try:
            r = int(clean[0:2], 16)
            g = int(clean[2:4], 16)
            b = int(clean[4:6], 16)
            return (b << 16) | (g << 8) | r
        except ValueError:
            return 0
    return 0


def get_window_hwnd(window: Any) -> int | None:
    """Resolve true outer top-level HWND for a Tk or toplevel window."""
    if not is_windows():
        return None
    try:
        if isinstance(window, int):
            return window
        if hasattr(window, "winfo_id"):
            window.update_idletasks()
            inner_id = window.winfo_id()
            if not inner_id:
                return None
            user32 = ctypes.windll.user32
            # GA_ROOT = 2: retrieves root window by walking parent chain
            hwnd = user32.GetAncestor(ctypes.c_void_p(inner_id), ctypes.c_uint(2))
            if not hwnd:
                hwnd = user32.GetParent(ctypes.c_void_p(inner_id))
            if not hwnd:
                hwnd = inner_id
            if user32.IsWindow(ctypes.c_void_p(hwnd)):
                return int(hwnd)
    except Exception:
        pass
    return None


def apply_dwm_attribute(hwnd: int, attribute: int, value: int) -> bool:
    """Set DWM attribute using DwmSetWindowAttribute safely."""
    if not is_windows() or not hwnd:
        return False
    try:
        dwmapi = ctypes.windll.dwmapi
        setter = dwmapi.DwmSetWindowAttribute
        setter.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        setting = ctypes.c_int(value)
        hr = setter(ctypes.c_void_p(hwnd), ctypes.c_uint(attribute), ctypes.byref(setting), ctypes.sizeof(setting))
        return hr == 0
    except Exception:
        return False


def set_window_backdrop(window: Any, backdrop: str = "mica") -> bool:
    """Apply native WinUI 3 backdrop (Mica, Mica Alt, Acrylic) to window."""
    if not is_windows():
        return False
    hwnd = get_window_hwnd(window)
    if not hwnd:
        return False

    success = False
    build = windows_build_number()

    # Try pywinstyles if available for acrylic / mica composition
    if pywinstyles is not None:
        try:
            target_style = backdrop.lower()
            if target_style in ("mica_alt", "tabbed"):
                target_style = "mica"
            if target_style in ("mica", "acrylic", "dark"):
                pywinstyles.apply_style(window, target_style)
                success = True
        except Exception:
            pass

    # Windows 11 Build >= 22621: DWMWA_SYSTEMBACKDROP_TYPE (Attribute 38)
    if build >= 22621:
        sbt = BACKDROP_MAP.get(backdrop.lower(), DWMSBT_MAINWINDOW)
        if apply_dwm_attribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, sbt):
            success = True
    elif build >= 22000:
        # Windows 11 21H2 Mica effect (Attribute 1029)
        if backdrop.lower() in ("mica", "mica_alt", "tabbed"):
            if apply_dwm_attribute(hwnd, DWMWA_MICA_EFFECT, 1):
                success = True

    return success


def apply_winui_window_styling(
    window: Any,
    *,
    theme_name: str | None = None,
    backdrop: str | None = None,
    round_corners: bool = True,
) -> bool:
    """
    Apply full WinUI 3 styling to a top-level window or dialog:
    - Rounded corners (DWMWCP_ROUND = 2)
    - Immersive dark mode title bar
    - Caption, text, and border colors matching the active theme
    - Mica / Mica Alt / Acrylic backdrop
    """
    if not is_windows():
        return False
    hwnd = get_window_hwnd(window)
    if not hwnd:
        return False

    palette = get_theme(theme_name)
    build = windows_build_number()

    # 1. Immersive dark mode (Attribute 20 on >= 20H1, Attribute 19 on older)
    if build >= 19041:
        apply_dwm_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
    elif build >= 17763:
        apply_dwm_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1, 1)

    # 2. Window corner preference (Windows 11: Attribute 33)
    if build >= 22000 and round_corners:
        apply_dwm_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)

    # 3. Caption Color (Attribute 35) & Text Color (Attribute 36)
    if build >= 22000:
        caption_ref = hex_to_colorref(palette.get("DWM_CAPTION", palette.get("SIDEBAR", "#141418")))
        apply_dwm_attribute(hwnd, DWMWA_CAPTION_COLOR, caption_ref)

        text_ref = hex_to_colorref(palette.get("TEXT", "#f5f5f7"))
        apply_dwm_attribute(hwnd, DWMWA_TEXT_COLOR, text_ref)

        # 4. Subtle WinUI 3 Border Color (Attribute 34)
        border_hex = palette.get("SIDEBAR_BORDER", palette.get("LINE", "#2e2e3a"))
        border_ref = hex_to_colorref(border_hex)
        apply_dwm_attribute(hwnd, DWMWA_BORDER_COLOR, border_ref)

        if pywinstyles is not None:
            try:
                sidebar_color = palette.get("SIDEBAR", "#141418")
                pywinstyles.change_header_color(window, sidebar_color)
                pywinstyles.change_border_color(window, border_hex)
                pywinstyles.change_title_color(window, palette.get("TEXT", "#f5f5f7"))
            except Exception:
                pass

    # 5. Backdrop effect (Mica Alt for tabbed/sidebar navigation, Mica or Acrylic)
    resolved_backdrop = backdrop
    if not resolved_backdrop:
        resolved_backdrop = palette.get("BACKDROP", "mica_alt" if build >= 22621 else "mica")

    set_window_backdrop(window, resolved_backdrop)
    return True


def apply_winui_dialog_styling(
    dialog: Any,
    *,
    theme_name: str | None = None,
    backdrop: str = "acrylic",
) -> bool:
    """Style child dialog or modal with WinUI 3 Acrylic and rounded corners."""
    return apply_winui_window_styling(
        dialog,
        theme_name=theme_name,
        backdrop=backdrop,
        round_corners=True,
    )


def configure_winui_dpi_awareness(window: Any | None = None) -> None:
    """Enable Per-Monitor V2 DPI Awareness and synchronize Tk scaling."""
    if not is_windows():
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    if window is not None and hasattr(window, "tk"):
        try:
            hwnd = get_window_hwnd(window)
            if hwnd:
                dpi = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
                if dpi > 0:
                    scaling = dpi / 72.0
                    window.tk.call("tk", "scaling", scaling)
        except Exception:
            pass
