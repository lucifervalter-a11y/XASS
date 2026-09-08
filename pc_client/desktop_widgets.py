"""Native Tk panels/buttons and anti-aliased monoline XASS decoration."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable
from PIL import Image, ImageDraw, ImageTk

BG, CARD, HOVER, LINE = "#202022", "#2b2b2f", "#34343a", "#3b3b42"
TEXT, MUTED, ACCENT, LILAC = "#f5f5f7", "#b1b1bb", "#829cff", "#c495f4"


def _parent_background(parent):
    try:
        return str(parent.cget("bg"))
    except tk.TclError:
        return BG


def rounded_image(width, height, *, fill, radius=12, border_color=None, border_width=1):
    """Transparent anti-aliased corners; no screenshot/bitmap UI labels."""
    width, height = max(1, int(width)), max(1, int(height))
    scale = 3
    image = Image.new("RGBA", (width * scale, height * scale))
    draw = ImageDraw.Draw(image)
    border = max(0, int(border_width)) if border_color else 0
    radius = max(0, min(int(radius), width // 2, height // 2))
    draw.rounded_rectangle((0, 0, width * scale - 1, height * scale - 1), radius=radius * scale,
                           fill=fill, outline=border_color if border else None, width=border * scale)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _icon_raster(name, size=22, color=TEXT):
    aliases = {"overview": "home", "computer": "monitor", "pc": "monitor", "device": "monitor",
               "connection": "link", "files": "folder", "logs": "journal", "updates": "update",
               "ram": "memory", "storage": "disk", "arrow": "chevron"}
    name = aliases.get(name, name)
    size, scale = max(8, int(size)), 4
    factor = size * scale / 24
    image = Image.new("RGBA", (size * scale, size * scale))
    draw = ImageDraw.Draw(image)
    stroke = max(1, round(1.7 * factor))

    def line(values, closed=False):
        values = list(values)
        if closed:
            values.append(values[0])
        points = [(round(x * factor), round(y * factor)) for x, y in values]
        draw.line(points, fill=color, width=stroke, joint="curve")
        for x, y in (points[0], points[-1]):
            r = stroke / 2
            draw.ellipse((x-r, y-r, x+r, y+r), fill=color)

    def box(bounds, radius=0):
        draw.rounded_rectangle(tuple(round(v * factor) for v in bounds), radius=round(radius * factor), outline=color, width=stroke)

    def ellipse(bounds):
        draw.ellipse(tuple(round(v * factor) for v in bounds), outline=color, width=stroke)

    def arc(bounds, start, end):
        draw.arc(tuple(round(v * factor) for v in bounds), start=start, end=end, fill=color, width=stroke)

    if name == "home":
        line([(3, 10), (12, 3), (21, 10)])
        line([(5, 9), (5, 21), (10, 21), (10, 14), (14, 14), (14, 21), (19, 21), (19, 9)])
    elif name == "monitor":
        box((2.5, 3.5, 21.5, 17), 1.8)
        line([(12, 17), (12, 21)]); line([(8, 21), (16, 21)])
    elif name == "link":
        arc((2.5, 7, 13.5, 17), 70, 290); arc((10.5, 7, 21.5, 17), 250, 470)
        line([(8, 12), (16, 12)])
    elif name == "folder":
        line([(3, 8), (3, 5), (9, 5), (12, 8), (21, 8), (21, 21), (3, 21), (3, 8), (21, 8)])
    elif name == "archive":
        box((3, 3, 21, 8), 1); box((4.5, 8, 19.5, 21), 1); box((10, 11, 14, 14), .5)
    elif name == "journal":
        box((4, 2.5, 20, 8), 1); line([(8, 5), (16, 5)])
        for y in (12, 18):
            box((4, y, 7, y + 3), .5); line([(11, y + 1.5), (20, y + 1.5)])
    elif name == "update":
        arc((3, 3, 21, 21), 190, 345); arc((3, 3, 21, 21), 10, 165)
        line([(21, 3), (21, 9), (15, 9)]); line([(3, 21), (3, 15), (9, 15)])
    elif name == "settings":
        outline = []
        for index in range(32):
            angle = index / 32 * math.tau - math.pi / 2
            r = 9.5 if index % 4 in (1, 2) else 7.6
            outline.append((12 + math.cos(angle) * r, 12 + math.sin(angle) * r))
        line(outline, True); ellipse((8.5, 8.5, 15.5, 15.5))
    elif name == "terminal":
        box((2.5, 3.5, 21.5, 20.5), 2)
        line([(6, 8), (10, 12), (6, 16)]); line([(13, 16), (18, 16)])
    elif name == "cpu":
        box((5, 5, 19, 19), 1.5); box((9, 9, 15, 15), .6)
        for value in (7, 12, 17):
            line([(value, 2), (value, 3)]); line([(value, 21), (value, 22)])
            line([(2, value), (3, value)]); line([(21, value), (22, value)])
    elif name == "memory":
        box((2.5, 5, 21.5, 17), 1.3); line([(3, 20), (21, 20)])
        for x in (6, 10, 14, 18):
            line([(x, 8), (x, 12)]); line([(x, 17), (x, 20)])
    elif name == "disk":
        line([(6, 3), (18, 3), (21, 17), (21, 21), (3, 21), (3, 17), (6, 3)])
        line([(4, 17), (20, 17)]); line([(7, 19), (9, 19)]); line([(17, 19), (17.1, 19)])
    elif name == "chevron":
        line([(9, 5), (16, 12), (9, 19)])
    elif name == "globe":
        ellipse((2, 2, 22, 22)); ellipse((7, 2, 17, 22)); line([(2, 12), (22, 12)])
    elif name == "shield":
        line([(12, 2.5), (20, 6), (20, 12), (18, 17), (12, 22), (6, 17), (4, 12), (4, 6)], True)
        line([(8, 12), (11, 15), (16, 9)])
    elif name == "play":
        line([(7, 3), (21, 12), (7, 21)], True)
    elif name == "pause":
        box((6, 3, 9, 21), .8); box((15, 3, 18, 21), .8)
    else:
        raise ValueError(f"Unknown XASS icon: {name}")
    return image.resize((size, size), Image.Resampling.LANCZOS)


def icon_image(master, name, size=22, color=TEXT):
    """Retain this PhotoImage when using it in a Label, as with normal Tk images."""
    return ImageTk.PhotoImage(_icon_raster(name, size, color), master=master)


class RoundedPanel(tk.Frame):
    """A normal Frame whose children pack/grid directly over a rounded backdrop."""
    def __init__(self, parent, *, bg=CARD, parent_bg=None, radius=12,
                 border_color=LINE, border_width=1, padx=20, pady=20, **options):
        self._fill = bg
        self._parent_bg = parent_bg or _parent_background(parent)
        self._radius = radius
        self._border_color = options.pop("highlightbackground", border_color)
        self._border_width = options.pop("highlightthickness", border_width)
        options.pop("borderwidth", None); options.pop("bd", None)
        super().__init__(parent, bg=self._parent_bg, padx=padx, pady=pady,
                         borderwidth=0, highlightthickness=0, **options)
        self._paint_job = None
        self._background = tk.Canvas(self, bg=self._parent_bg, highlightthickness=0, borderwidth=0)
        self._background.place(x=0, y=0, relwidth=1, relheight=1, bordermode="outside")
        self._background_image = self._last_paint = None
        self.bind("<Configure>", self._schedule_paint, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._schedule_paint()

    def cget(self, key):
        return self._fill if key in {"bg", "background"} else super().cget(key)

    __getitem__ = cget

    def configure(self, cnf=None, **options):
        if isinstance(cnf, str):
            return super().configure(cnf)
        if cnf:
            options = dict(cnf, **options)
        if not options:
            return super().configure()
        for key in ("bg", "background"):
            if key in options:
                self._fill = options.pop(key)
        for key, attr in (("radius", "_radius"), ("border_color", "_border_color"),
                          ("border_width", "_border_width"), ("parent_bg", "_parent_bg"),
                          ("highlightbackground", "_border_color"), ("highlightthickness", "_border_width")):
            if key in options:
                setattr(self, attr, options.pop(key))
        result = super().configure(**options) if options else None
        if hasattr(self, "_background"):
            super().configure(bg=self._parent_bg)
            self._background.configure(bg=self._parent_bg)
            self._schedule_paint()
        return result

    config = configure

    def _schedule_paint(self, _event=None):
        if self._paint_job is None:
            self._paint_job = self.after_idle(self._paint)

    def _paint(self):
        self._paint_job = None
        width, height = self.winfo_width(), self.winfo_height()
        key = (width, height, self._fill, self._radius, self._border_color, self._border_width)
        if width < 2 or height < 2 or key == self._last_paint:
            return
        self._last_paint = key
        self._background_image = ImageTk.PhotoImage(rounded_image(width, height, fill=self._fill,
            radius=self._radius, border_color=self._border_color, border_width=self._border_width), master=self)
        self._background.delete("all")
        self._background.create_image(0, 0, anchor="nw", image=self._background_image)
        self.tk.call("lower", self._background._w)  # Canvas.lower is tag_lower, not widget stacking.

    def _on_destroy(self, event):
        if event.widget is self and self._paint_job is not None:
            self.after_cancel(self._paint_job)
            self._paint_job = None


class ModernButton(tk.Canvas):
    _ALIASES = {"background": "bg", "foreground": "fg", "bd": "borderwidth"}

    def __init__(self, parent, text="", command: Callable | None = None, *, bg=HOVER, fg=TEXT,
                 activebackground=None, activeforeground=None, disabledforeground="#74747e",
                 font=("Segoe UI Semibold", 10), padx=18, pady=10, width=0, height=0,
                 radius=9, border_color=None, icon=None, icon_size=20, anchor="center", state="normal",
                 parent_bg=None, **options):
        self._values = dict(text=text, command=command, bg=bg, fg=fg,
            activebackground=activebackground or HOVER, activeforeground=activeforeground or fg,
            disabledforeground=disabledforeground, font=font, padx=padx, pady=pady, width=width, height=height,
            radius=radius, border_color=border_color, border_width=1 if border_color else 0,
            icon=icon, icon_size=icon_size, anchor=anchor, state=state, cursor="hand2", takefocus=1,
            textvariable=None, image=None, compound="left", active=False, highlightcolor=ACCENT,
            highlightbackground=bg, highlightthickness=0, relief="flat", borderwidth=0,
            justify="center", underline=-1, wraplength=0)
        self._values.update({self._ALIASES.get(k, k): v for k, v in options.items()})
        self._parent_bg = parent_bg or _parent_background(parent)
        super().__init__(parent, bg=self._parent_bg, highlightthickness=0, borderwidth=0,
                         takefocus=self._values["takefocus"], cursor=self._values["cursor"])
        self._hover = self._pressed = self._focused = self._armed = False
        self._paint_job = self._background_image = self._icon_image = None
        self._variable_trace = self._variable = None
        self.bind("<Configure>", self._schedule_paint, add="+")
        self.bind("<Enter>", lambda _: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda _: self._set_hover(False), add="+")
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        self.bind("<FocusIn>", lambda _: self._set_focus(True), add="+")
        self.bind("<FocusOut>", lambda _: self._set_focus(False), add="+")
        for key in ("space", "Return", "KP_Enter"):
            self.bind(f"<KeyPress-{key}>", self._key_press, add="+")
            self.bind(f"<KeyRelease-{key}>", self._key_release, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._watch_variable()
        self._resize_request()

    def _text(self):
        variable = self._values["textvariable"]
        if variable:
            try:
                return str(variable.get() if hasattr(variable, "get") else self.getvar(str(variable)))
            except tk.TclError:
                return ""
        return str(self._values["text"])

    def _watch_variable(self):
        if self._variable is not None and self._variable_trace:
            self._variable.trace_remove("write", self._variable_trace)
        self._variable, self._variable_trace = self._values["textvariable"], None
        if self._variable is not None and hasattr(self._variable, "trace_add"):
            self._variable_trace = self._variable.trace_add("write", lambda *_: self._resize_request())

    def _resize_request(self):
        self._font = tkfont.Font(root=self, font=self._values["font"])
        line_height = self._font.metrics("linespace")
        image = self._values["image"]
        icon_width = int(self._values["icon_size"]) if self._values["icon"] else (image.width() if image else 0)
        text_width = max((self._font.measure(line) for line in self._text().splitlines()), default=0)
        width = self._font.measure("0") * int(self._values["width"]) if self._values["width"] else text_width
        width += 2 * int(self._values["padx"]) + (icon_width + 10 if icon_width else 0)
        height = max(line_height * max(1, int(self._values["height"])), icon_width) + 2 * int(self._values["pady"])
        super().configure(width=max(1, width), height=max(1, height),
            cursor="arrow" if self._values["state"] == "disabled" else self._values["cursor"],
            takefocus=0 if self._values["state"] == "disabled" else self._values["takefocus"])
        self._schedule_paint()

    def configure(self, cnf=None, **options):
        if isinstance(cnf, str):
            value = self.cget(cnf)
            return (cnf, cnf, cnf, value, value)
        if cnf:
            options = dict(cnf, **options)
        if not options:
            return {key: (key, key, key, value, value) for key, value in self._values.items()}
        native = {}
        for original, value in options.items():
            key = self._ALIASES.get(original, original)
            if key in self._values:
                self._values[key] = value
            elif key == "parent_bg":
                self._parent_bg = value
                native["bg"] = value
            else:
                native[original] = value
        if native:
            super().configure(**native)
        if self._values["state"] == "disabled":
            self._armed = self._pressed = False
        if "textvariable" in options:
            self._watch_variable()
        self._resize_request()

    config = configure

    def cget(self, key):
        key = self._ALIASES.get(key, key)
        return self._values[key] if key in self._values else super().cget(key)

    __getitem__ = cget

    def invoke(self):
        if self._values["state"] != "disabled" and callable(self._values["command"]):
            return self._values["command"]()
        return None

    def _set_hover(self, value):
        self._hover = value
        self._schedule_paint()

    def _set_focus(self, value):
        self._focused = value
        if not value:
            self._armed = self._pressed = False
        self._schedule_paint()

    def _press(self, _event):
        if self._values["state"] != "disabled":
            self.focus_set()
            self._armed = self._pressed = True
            self._schedule_paint()
        return "break"

    def _release(self, event):
        invoke = self._armed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._armed = self._pressed = False
        self._schedule_paint()
        if invoke:
            self.invoke()
        return "break"

    def _key_press(self, _event):
        if self._values["state"] != "disabled":
            self._armed = self._pressed = True
            self._schedule_paint()
        return "break"

    def _key_release(self, _event):
        invoke = self._armed
        self._armed = self._pressed = False
        self._schedule_paint()
        if invoke:
            self.invoke()
        return "break"

    def _schedule_paint(self, _event=None):
        if self._paint_job is None:
            self._paint_job = self.after_idle(self._paint)

    def _paint(self):
        self._paint_job = None
        width, height = self.winfo_width(), self.winfo_height()
        if width < 2 or height < 2:
            return
        disabled = self._values["state"] == "disabled"
        highlighted = (self._hover or self._pressed) and not disabled
        fill = self._values["activebackground"] if highlighted else self._values["bg"]
        fg = self._values["disabledforeground"] if disabled else (
            self._values["activeforeground"] if highlighted else self._values["fg"])
        border = self._values["highlightcolor"] if self._focused and not disabled else self._values["border_color"]
        self._background_image = ImageTk.PhotoImage(rounded_image(width, height, fill=fill,
            radius=self._values["radius"], border_color=border,
            border_width=1 if self._focused and not disabled else self._values["border_width"]), master=self)
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self._background_image)
        text, icon, supplied = self._text(), self._values["icon"], self._values["image"]
        icon_width = int(self._values["icon_size"]) if icon else (supplied.width() if supplied else 0)
        available = max(0, width - 2 * int(self._values["padx"]) - (icon_width + 10 if icon_width else 0))
        while text and self._font.measure(text) > available:
            text = text[:-2].rstrip("…") + "…" if len(text) > 1 else ""
        group_width = self._font.measure(text) + (icon_width + 10 if icon_width and text else icon_width)
        anchor = self._values["anchor"]
        start = int(self._values["padx"]) if anchor in {"w", "nw", "sw", "left"} else (
            width - int(self._values["padx"]) - group_width if anchor in {"e", "ne", "se", "right"} else (width - group_width) / 2)
        shift = 1 if self._pressed and not disabled else 0
        if icon or supplied:
            self._icon_image = icon_image(self, icon, icon_width, fg) if icon else supplied
            self.create_image(start + icon_width / 2, height / 2 + shift, image=self._icon_image)
            start += icon_width + (10 if text else 0)
        self.create_text(start, height / 2 + shift, text=text, anchor="w", fill=fg, font=self._font, tags="label")

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        if self._paint_job is not None:
            self.after_cancel(self._paint_job)
            self._paint_job = None
        if self._variable is not None and self._variable_trace:
            self._variable.trace_remove("write", self._variable_trace)


class NavButton(ModernButton):
    def __init__(self, parent, text="", command=None, icon="home", active=False, **options):
        self._nav_active = active
        self._nav_normal = options.pop("bg", BG)
        self._nav_selected = options.pop("selectedbackground", HOVER)
        defaults = dict(bg=self._nav_selected if active else self._nav_normal, fg=TEXT if active else MUTED,
            activebackground=HOVER, activeforeground=TEXT, anchor="w", padx=20, pady=12,
            font=("Segoe UI", 11), icon=icon, icon_size=26, radius=9)
        defaults.update(options)
        super().__init__(parent, text, command, **defaults)
        self._values["active"] = active

    def set_active(self, active):
        self._nav_active = bool(active)
        super().configure(active=self._nav_active, bg=self._nav_selected if active else self._nav_normal,
                          fg=TEXT if active else MUTED)

    def configure(self, cnf=None, **options):
        if cnf and not isinstance(cnf, str):
            options = dict(cnf, **options)
            cnf = None
        if "active" in options:
            active = bool(options.pop("active"))
            self._nav_active = active
            options.setdefault("bg", self._nav_selected if active else self._nav_normal)
            options.setdefault("fg", TEXT if active else MUTED)
            options["active"] = active
        return super().configure(cnf, **options)

    config = configure

    def _paint(self):
        super()._paint()
        if self._nav_active and self.winfo_height() > 4:
            center = self.winfo_height() / 2
            self.create_line(2, center - 10, 2, center + 10, fill=LILAC, width=3,
                             capstyle="round", tags="active-indicator")
