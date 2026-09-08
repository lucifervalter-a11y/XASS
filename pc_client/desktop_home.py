"""Native Windows home surface. Live controls and metrics, never a screenshot UI."""
from __future__ import annotations

from pathlib import Path
import sys
import tkinter as tk
from tkinter import font as tkfont
from typing import Any

from PIL import Image, ImageChops, ImageOps, ImageTk

from desktop_widgets import ModernButton, RoundedPanel, icon_image, rounded_image

BG, CARD, LINE = "#202022", "#2b2b2f", "#3b3b42"
TEXT, MUTED, BLUE, LILAC = "#f5f5f7", "#b1b1bb", "#829cff", "#c495f4"


def _icon(parent: tk.Misc, name: str, *, size: int = 24, color: str = TEXT) -> tk.Label:
    graphic = icon_image(parent, name, size, color)
    label = tk.Label(parent, image=graphic, bg=parent.cget("bg"), borderwidth=0)
    label.image = graphic
    return label


def _watch(owner: tk.Widget, variable: tk.Variable, callback) -> None:
    token = variable.trace_add("write", lambda *_: callback())

    def release(event) -> None:
        if event.widget is owner:
            try:
                variable.trace_remove("write", token)
            except tk.TclError:
                pass

    owner.bind("<Destroy>", release, add="+")


class HomeHero(tk.Canvas):
    """An image-backed panel with real keyboard-focusable buttons and live text."""

    def __init__(self, parent: tk.Misc, app: Any) -> None:
        super().__init__(parent, bg=BG, height=340, highlightthickness=0, borderwidth=0)
        self.app = app
        self._pending = None
        self._photo = None
        self._render_size = None
        self._title_font = tkfont.Font(self, family="Segoe UI Semibold", size=27)
        self._title_font_size = 27
        self._source = None
        resource = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        path = resource / "assets" / "xass-desktop-hero.png"
        if path.is_file():
            with Image.open(path) as source:
                self._source = source.convert("RGB")
        self._art = self.create_image(0, 0, anchor="nw")
        self._heading = self.create_text(32, 110, anchor="w", fill=TEXT, font=("Segoe UI Semibold", 27))
        self._description = self.create_text(32, 151, anchor="nw", text="Управление через Telegram и iPhone", fill="#c6c6ce", font=("Segoe UI", 12))
        self.open_button = ModernButton(self, text="Открыть Mini App", command=app.open_miniapp,
                                       bg=BLUE, fg="#12131c", activebackground="#9aafff", parent_bg="#000000",
                                       padx=20, pady=11, font=("Segoe UI Semibold", 11))
        self.connect_button = ModernButton(self, text="Подключение", command=lambda: app.show_view("connection"),
                                          bg="#252529", fg=TEXT, activebackground="#33333a", parent_bg="#000000",
                                          border_color="#44444c", padx=19, pady=11, font=("Segoe UI Semibold", 11))
        self.bind("<Configure>", self._schedule_draw)
        self.bind("<Destroy>", self._destroyed, add="+")
        _watch(self, app.name_var, self._schedule_draw)

    def _destroyed(self, event) -> None:
        if event.widget is self and self._pending:
            self.after_cancel(self._pending)
            self._pending = None

    def _schedule_draw(self, _event=None) -> None:
        if _event is not None and (_event.width, _event.height) == self._render_size:
            return  # Scroll/move Configure events do not change any artwork.
        if not self._pending and self.winfo_exists():
            self._pending = self.after(35, self._draw)

    def _draw(self) -> None:
        self._pending = None
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        if width < 3 or height < 3:
            return
        if self._render_size != (width, height):
            self._photo = ImageTk.PhotoImage(self._render_backdrop(width, height), master=self)
            self.itemconfigure(self._art, image=self._photo)
            self._render_size = (width, height)
        size = 27 if width > 760 else 23
        if size != self._title_font_size:
            self._title_font.configure(size=size)
            self._title_font_size = size
        name = self.app.name_var.get().strip() or "Мой компьютер"
        available = width - 64 if width < 690 else max(190, int(width * .56) - 35)
        while len(name) > 2 and self._title_font.measure(name) > available:
            name = name[:-2].rstrip("…") + "…"
        self.itemconfigure(self._heading, text=name, font=self._title_font)
        self.itemconfigure(self._description, width=max(250, int(width * .53)))
        self.open_button.place(x=32, y=height - 92)
        self.connect_button.place(x=32 + self.open_button.winfo_reqwidth() + 12, y=height - 92)

    def _render_backdrop(self, width: int, height: int) -> Image.Image:
        # Asset fitting is rendering, not a baked UI: all text remains native.
        picture = ImageOps.fit(self._source, (width, height), Image.Resampling.LANCZOS) if self._source else Image.new("RGB", (width, height), "#000000")
        mask = rounded_image(width, height, fill="#ffffff", radius=16).getchannel("A")
        canvas = Image.new("RGB", (width, height), BG)
        canvas.paste(picture, (0, 0), mask)
        edge = rounded_image(width, height, fill="#000000", radius=16, border_color="#ffffff", border_width=1)
        edge_mask = ImageChops.multiply(edge.convert("L"), edge.getchannel("A"))
        gradient = Image.new("RGB", (width, 1))
        gradient.putdata([tuple(round(a + (b-a) * x / max(1, width-1))
                               for a, b in zip((130, 156, 255), (196, 149, 244))) for x in range(width)])
        canvas.paste(gradient.resize((width, height), Image.Resampling.NEAREST), (0, 0), edge_mask)
        return canvas


class HomeGrid(tk.Frame):
    def __init__(self, parent: tk.Misc, *, breakpoint: int = 900) -> None:
        super().__init__(parent, bg=BG)
        self.items: list[tk.Widget] = []
        self.breakpoint = breakpoint
        self._wide = None
        self.bind("<Configure>", self.arrange)

    def add(self, item: tk.Widget) -> None:
        self.items.append(item)
        self._wide = None

    def arrange(self, _event=None) -> None:
        wide = self.winfo_width() >= self.breakpoint
        if wide == self._wide:
            return
        self._wide = wide
        for index in range(3):
            self.columnconfigure(index, weight=1 if wide or index == 0 else 0, uniform="home" if wide else "")
        for index, item in enumerate(self.items):
            item.grid(row=0 if wide else index, column=index if wide else 0, sticky="nsew",
                      padx=(0 if not wide or index == 0 else 6, 0 if not wide or index == 2 else 6),
                      pady=(0, 0 if wide or index == 2 else 10))


def build_home(app: Any) -> None:
    app.metric_bars = {}
    status = RoundedPanel(app.content, bg=CARD, padx=22, pady=16)
    status.pack(fill="x", pady=(0, 14))
    _icon(status, "monitor", size=34).pack(side="left", padx=(0, 18))
    copy = tk.Frame(status, bg=CARD)
    copy.pack(side="left", fill="x", expand=True)
    title = tk.Frame(copy, bg=CARD)
    title.pack(fill="x")
    app.hero_dot = tk.Label(title, text="●", bg=CARD, fg=app.status_color, font=("Segoe UI", 11))
    app.hero_dot.pack(side="left", padx=(0, 8))
    app.home_connection_text = tk.StringVar()
    tk.Label(title, textvariable=app.home_connection_text, bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 13)).pack(side="left")
    detail = tk.StringVar()
    tk.Label(copy, textvariable=detail, bg=CARD, fg=MUTED, font=("Segoe UI", 11)).pack(anchor="w", pady=(3, 0))

    def status_changed() -> None:
        value = app.connection_var.get()
        app.home_connection_text.set("Компьютер подключён" if value == "В сети" else value)
        detail.set("Предпросмотр интерфейса · агент не запущен" if app.preview else "Последний ответ агента — " + app.last_seen_var.get())

    _watch(status, app.connection_var, status_changed)
    _watch(status, app.last_seen_var, status_changed)
    status_changed()

    app.home_hero = HomeHero(app.content, app)
    app.home_hero.pack(fill="x", pady=(0, 14))

    metrics = HomeGrid(app.content, breakpoint=760)
    metrics.pack(fill="x", pady=(0, 12))
    for key, label, icon, variable, color in (
        ("cpu", "Процессор", "cpu", app.cpu_var, BLUE),
        ("memory", "Память", "memory", app.memory_var, LILAC),
        ("disk", "Диск", "disk", app.disk_var, BLUE),
    ):
        card = RoundedPanel(metrics, bg=CARD, padx=18, pady=17)
        metrics.add(card)
        _icon(card, icon, size=34, color=color).pack(side="left", padx=(0, 14))
        content = tk.Frame(card, bg=CARD)
        content.pack(side="left", fill="both", expand=True)
        row = tk.Frame(content, bg=CARD)
        row.pack(fill="x", pady=(0, 10))
        tk.Label(row, text=label, bg=CARD, fg=MUTED, font=("Segoe UI", 11)).pack(side="left")
        tk.Label(row, textvariable=variable, bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 12)).pack(side="right")
        track = tk.Canvas(content, height=6, bg="#44444a", highlightthickness=0)
        track.pack(fill="x")
        bar = track.create_rectangle(0, 0, 0, 6, fill=color, outline=color)
        app.metric_bars[key] = (track, bar)
    metrics.after_idle(metrics.arrange)

    actions = HomeGrid(app.content)
    actions.pack(fill="x", pady=(0, 14))
    for heading, subtitle, icon, target, color in (
        ("Команды", "Действия на этом ПК", "terminal", "commands", BLUE),
        ("Файлы и архив", "Локальные данные", "folder", "archive", LILAC),
        ("Обновления", "Версия и установка", "update", "updates", BLUE),
    ):
        card = RoundedPanel(actions, bg=CARD, padx=18, pady=20)
        actions.add(card)
        tile_icon = _icon(card, icon, size=42, color=color)
        tile_icon.pack(side="left", padx=(0, 14))
        arrow = _icon(card, "chevron", size=19)
        arrow.pack(side="right", padx=(8, 0))
        copy = tk.Frame(card, bg=CARD)
        copy.pack(side="left", fill="x", expand=True)
        button = ModernButton(copy, text=heading, command=lambda view=target: app.show_view(view),
                              bg=CARD, fg=TEXT, activebackground="#36363d",
                              anchor="w", padx=4, pady=4, font=("Segoe UI Semibold", 12))
        button.pack(fill="x")
        caption = tk.Label(copy, text=subtitle, bg=CARD, fg=MUTED, font=("Segoe UI", 10))
        caption.pack(anchor="w", padx=4, pady=(4, 0))
        # The entire tile is a target; the heading keeps keyboard focus/activation.
        for surface in (card, card._background, copy, tile_icon, arrow, caption):
            surface.configure(cursor="hand2")
            surface.bind("<ButtonRelease-1>", lambda _event, view=target: app.show_view(view))
    actions.after_idle(actions.arrange)

    event_card = RoundedPanel(app.content, bg=CARD, padx=20, pady=14)
    event_card.pack(fill="x")
    heading = tk.Frame(event_card, bg=CARD)
    heading.pack(fill="x", pady=(0, 10))
    tk.Label(heading, text="Последние события", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 11)).pack(side="left")
    ModernButton(heading, text="Открыть журнал", command=lambda: app.show_view("journal"), bg=CARD,
                 fg=BLUE, activebackground="#36363d", padx=7, pady=3, font=("Segoe UI", 10)).pack(side="right")
    tk.Frame(event_card, bg=LINE, height=1).pack(fill="x")
    event_rows = tk.Frame(event_card, bg=CARD)
    event_rows.pack(fill="x")

    def render_events() -> None:
        if not event_rows.winfo_exists():
            return
        for child in event_rows.winfo_children():
            child.destroy()
        recent = app.history[-2:][::-1]
        if not recent:
            tk.Label(event_rows, text="События подключения и обновления появятся здесь.", bg=CARD, fg=MUTED,
                     font=("Segoe UI", 10)).pack(anchor="w", pady=(14, 3))
        for entry in recent:
            line = tk.Frame(event_rows, bg=CARD)
            line.pack(fill="x", pady=(11, 0))
            tk.Label(line, text="●", bg=CARD, fg=BLUE, font=("Segoe UI", 9)).pack(side="left", padx=(0, 12))
            safe = app._redact_log(entry)
            tk.Label(line, text=safe[:8], bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(0, 18))
            message = tk.Label(line, text=safe[10:][:180], bg=CARD, fg=MUTED, font=("Segoe UI", 9), anchor="w", justify="left")
            message.pack(side="left", fill="x", expand=True)
            line.bind("<Configure>", lambda event, label=message: label.configure(wraplength=max(120, event.width - 110)))
    app._render_home_events = render_events
    render_events()
