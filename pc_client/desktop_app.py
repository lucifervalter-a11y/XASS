from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from tkinter import ttk
from typing import Any, Callable

import httpx
import psutil

try:
    import pystray
    from PIL import Image
except ImportError:  # Source checkout can still run before optional UI deps are installed.
    pystray = None
    Image = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

from client_agent import (
    build_payload,
    claim_pair_code,
    discover_backend_url,
    ensure_minimal_defaults,
    load_config,
    normalize_server_url,
    save_config,
)
from client_update import (
    DATA_ROOT,
    UPDATE_MARKER,
    UPDATE_RESULT,
    current_revision,
    current_version,
    download_installer_update,
    download_update,
    is_installer_build,
    launch_installer_update,
    launch_update_helper,
    load_agent_status,
    load_update_state,
    update_in_progress,
    update_operation,
    write_agent_status,
)
from connection_file import ConnectionProfile, load_connection_file, parse_connection_text
from archive_store import archive_root, archive_status, cleanup_archive, conversation_rows
from network_client import create_http_client
try:
    from runtime_state import acquire_single_instance, append_log, configure_utf8_logging, read_log_tail
except ModuleNotFoundError:
    from pc_client.runtime_state import acquire_single_instance, append_log, configure_utf8_logging, read_log_tail

ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
BG = "#070708"
SIDEBAR = "#0b0b0c"
CARD = "#111214"
CARD_HOVER = "#17191d"
FIELD = "#101114"
LINE = "#2b2d31"
TEXT = "#f4f4f5"
MUTED = "#9c9ca3"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#2f73df"
VIOLET = "#3b82f6"
GREEN = "#61c554"
AMBER = "#efb65c"
RED = "#f36b76"


class DarkScrolledText(tk.Frame):
    """Text widget with a themeable, dark scrollbar."""

    def __init__(self, parent: tk.Misc, **options: Any) -> None:
        background = str(options.get("bg") or CARD)
        super().__init__(parent, bg=background, highlightthickness=0, borderwidth=0)
        self.text = tk.Text(self, **options)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview, style="XASS.Vertical.TScrollbar")
        self.text.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

    def insert(self, *args: Any, **kwargs: Any) -> Any:
        return self.text.insert(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        return self.text.delete(*args, **kwargs)

    def see(self, *args: Any, **kwargs: Any) -> Any:
        return self.text.see(*args, **kwargs)

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if not hasattr(self, "text"):
            return super().configure(cnf, **kwargs)
        return self.text.configure(cnf, **kwargs)

    config = configure


def _configure_windows_process() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("XASS.Desktop.Agent")
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def _resource_path(relative: str) -> Path:
    return RESOURCE_ROOT / relative


class XassDesktop:
    def __init__(self, root: tk.Tk, *, minimized: bool = False, preview: bool = False) -> None:
        self.root = root
        self.root.title("XASS — предпросмотр" if preview else "XASS")
        screen_width = max(960, self.root.winfo_screenwidth())
        screen_height = max(700, self.root.winfo_screenheight())
        width = min(1360, max(980, screen_width - 140))
        height = min(840, max(660, screen_height - 120))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(900, 620)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.option_add("*Font", ("Segoe UI", 10))
        self.root.option_add("*Scrollbar.background", CARD_HOVER)
        self.root.option_add("*Scrollbar.troughColor", BG)
        self.root.option_add("*Scrollbar.activeBackground", ACCENT)
        self.root.option_add("*Scrollbar.borderWidth", 0)
        self.root.option_add("*Scrollbar.width", 11)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "XASS.Vertical.TScrollbar",
            gripcount=0,
            background=CARD_HOVER,
            darkcolor=CARD_HOVER,
            lightcolor=CARD_HOVER,
            troughcolor=BG,
            bordercolor=BG,
            arrowcolor=MUTED,
            relief="flat",
            width=11,
        )
        style.map("XASS.Vertical.TScrollbar", background=[("active", ACCENT)])
        icon_path = _resource_path("assets/xass.ico")
        if icon_path.is_file():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.brand_image: tk.PhotoImage | None = None
        brand_path = _resource_path("assets/xass-icon.png")
        if brand_path.is_file():
            try:
                self.brand_image = tk.PhotoImage(file=str(brand_path)).subsample(8, 8)
            except tk.TclError:
                self.brand_image = None

        self.config = ensure_minimal_defaults(load_config())
        self.config["desktop_managed"] = True
        self.process: subprocess.Popen[str] | None = None
        self.external_agent_pid = 0
        self._agent_started_at = 0.0
        self._start_after_id: str | None = None
        self._closing = False
        self._hidden_to_tray = False
        self._restart_failures: list[float] = []
        self._update_checking = False
        self._archive_cancel = threading.Event()
        self._archive_moving = False
        self.tray_icon: Any | None = None
        self._expected_stop_pids: set[int] = set()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.history: list[str] = []
        self.current_view = "overview"
        self.nav_buttons: dict[str, tk.Button] = {}

        self.server_var = tk.StringVar(value=str(self.config.get("server_url") or "http://127.0.0.1:8001"))
        self.name_var = tk.StringVar(value=str(self.config.get("source_name") or socket.gethostname()))
        self.pair_var = tk.StringVar()
        self.interval_var = tk.StringVar(value=str(self.config.get("interval_sec") or 30))
        self.auto_update_var = tk.BooleanVar(value=bool(self.config.get("auto_update", True)))
        self.archive_folder_var = tk.StringVar(value=str(self.config.get("archive_folder") or archive_root(self.config)))
        self.archive_max_gb_var = tk.StringVar(value=str(self.config.get("archive_max_gb") or ""))
        self.archive_retention_days_var = tk.StringVar(value=str(self.config.get("archive_retention_days") or ""))
        self.archive_state_var = tk.StringVar(value="Локальный архив ещё не синхронизирован")
        self.connection_var = tk.StringVar(value="Остановлен")
        self.last_seen_var = tk.StringVar(value="Heartbeat ещё не получен")
        self.latency_var = tk.StringVar(value="—")
        self.agent_version_var = tk.StringVar(value=current_version())
        self.agent_pid_var = tk.StringVar(value="—")
        self.update_state_var = tk.StringVar(value="Готово к проверке")
        self.last_error_var = tk.StringVar(value="Ошибок нет")
        self.server_state_var = tk.StringVar(value="Не проверен")
        self.import_status_var = tk.StringVar(value="Выберите файл .xass, скачанный в Telegram Mini App")
        self.status_color = AMBER
        self.cpu_var = tk.StringVar(value="—")
        self.memory_var = tk.StringVar(value="—")
        self.disk_var = tk.StringVar(value="—")
        self.memory_detail_var = tk.StringVar(value="—")
        self.disk_detail_var = tk.StringVar(value="—")
        self.uptime_var = tk.StringVar(value="—")
        self.local_time_var = tk.StringVar(value="—")
        self.metric_bars: dict[str, tuple[tk.Canvas, int]] = {}

        self._build_shell()
        self._setup_tray()
        self.show_view("overview")
        self.root.after(120, self._drain_logs)
        self.root.after(450, self._refresh_agent_status)
        self.root.after(700, self._refresh_update_status)
        self.root.after(220, self._refresh_local_metrics)
        if preview:
            self._set_status("Предпросмотр", ACCENT)
            self.last_seen_var.set("Агент не запущен в режиме предпросмотра")
            self.server_state_var.set("Без подключения")
        else:
            self._schedule_start(250)
        if minimized:
            self.root.after(900, self.close)

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        kind: str = "secondary",
        width: int | None = None,
    ) -> tk.Button:
        palette = {
            "primary": (ACCENT, "#ffffff", ACCENT_HOVER),
            "secondary": (CARD_HOVER, TEXT, "#14283b"),
            "ghost": (CARD, MUTED, CARD_HOVER),
            "danger": ("#2b1720", "#ffadb7", "#3a1b26"),
        }
        background, foreground, active = palette[kind]
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=background,
            fg=foreground,
            activebackground=active,
            activeforeground="#ffffff" if kind == "primary" else foreground,
            relief="flat",
            borderwidth=0,
            padx=18,
            pady=10,
            cursor="hand2",
            font=("Segoe UI Semibold", 10),
            width=width or 0,
        )
        button.bind("<Enter>", lambda _event: button.configure(bg=active))
        button.bind("<Leave>", lambda _event: button.configure(bg=background))
        return button

    def _card(self, parent: tk.Misc, *, padding: int = 20) -> tk.Frame:
        return tk.Frame(
            parent,
            bg=CARD,
            padx=padding,
            pady=padding,
            highlightbackground=LINE,
            highlightthickness=1,
        )

    @staticmethod
    def _style_scrolled_text(widget: DarkScrolledText) -> None:
        try:
            widget.vbar.configure(style="XASS.Vertical.TScrollbar")
        except (AttributeError, tk.TclError):
            pass

    def _build_shell(self) -> None:
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(shell, bg=SIDEBAR, width=244, highlightbackground=LINE, highlightthickness=1)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=24, pady=(27, 38))
        if self.brand_image is not None:
            tk.Label(brand, image=self.brand_image, bg=SIDEBAR, borderwidth=0).pack(side="left", padx=(0, 12))
        brand_copy = tk.Frame(brand, bg=SIDEBAR)
        brand_copy.pack(side="left", anchor="center")
        tk.Label(brand_copy, text="XASS", bg=SIDEBAR, fg=TEXT, font=("Segoe UI Semibold", 25)).pack(anchor="w")
        tk.Label(
            brand_copy,
            text="DESKTOP AGENT",
            bg=SIDEBAR,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w", pady=(2, 0))

        for key, label in (
            ("overview", "Обзор"),
            ("connection", "Подключение"),
            ("archive", "Архив"),
            ("updates", "Обновления"),
            ("settings", "Настройки"),
            ("diagnostics", "Диагностика"),
        ):
            button = tk.Button(
                self.sidebar,
                text=label,
                command=lambda item=key: self.show_view(item),
                anchor="w",
                bg=SIDEBAR,
                fg=MUTED,
                activebackground=CARD_HOVER,
                activeforeground=TEXT,
                relief="flat",
                borderwidth=0,
                padx=18,
                pady=14,
                cursor="hand2",
                font=("Segoe UI Semibold", 10),
            )
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[key] = button

        footer = self._card(self.sidebar, padding=14)
        footer.pack(side="bottom", fill="x", padx=14, pady=14)
        status_row = tk.Frame(footer, bg=CARD)
        status_row.pack(fill="x")
        self.side_dot = tk.Label(status_row, text="●", bg=CARD, fg=self.status_color, font=("Segoe UI", 12))
        self.side_dot.pack(side="left")
        self.side_status = tk.Label(
            status_row,
            textvariable=self.connection_var,
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
        )
        self.side_status.pack(side="left", padx=(7, 0))
        tk.Label(
            footer,
            text=f"Клиент {current_version()}  ·  stable",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(7, 0))

        body = tk.Frame(shell, bg=BG)
        body.pack(side="left", fill="both", expand=True)
        self.body_canvas = tk.Canvas(body, bg=BG, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(
            body,
            orient="vertical",
            command=self.body_canvas.yview,
            style="XASS.Vertical.TScrollbar",
        )
        self.body_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.body_canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.body_canvas, bg=BG, padx=28, pady=24)
        self.content_window = self.body_canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._on_content_configure)
        self.body_canvas.bind("<Configure>", self._on_canvas_configure)
        self.body_canvas.bind("<MouseWheel>", self._on_mousewheel)

    def _on_content_configure(self, _event: tk.Event[Any]) -> None:
        self.body_canvas.configure(scrollregion=self.body_canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event[Any]) -> None:
        self.body_canvas.itemconfigure(self.content_window, width=max(1, event.width))

    def _on_mousewheel(self, event: tk.Event[Any]) -> None:
        self.body_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _clear_content(self) -> None:
        for widget in self.content.winfo_children():
            widget.destroy()

    def show_view(self, name: str) -> None:
        self.current_view = name
        for key, button in self.nav_buttons.items():
            button.configure(bg=CARD_HOVER if key == name else SIDEBAR, fg=TEXT if key == name else MUTED)
        self._clear_content()
        if name == "connection":
            self._build_connection()
        elif name == "archive":
            self._build_archive()
        elif name == "updates":
            self._build_updates()
        elif name == "settings":
            self._build_settings()
        elif name == "diagnostics":
            self._build_diagnostics()
        else:
            self._build_overview()
        self.body_canvas.yview_moveto(0)

    def _header(self, title: str, subtitle: str) -> None:
        top = tk.Frame(self.content, bg=BG)
        top.pack(fill="x", pady=(0, 18))
        copy = tk.Frame(top, bg=BG)
        copy.pack(side="left")
        tk.Label(copy, text=title, bg=BG, fg=TEXT, font=("Segoe UI", 25)).pack(anchor="w")
        tk.Label(copy, text=subtitle, bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 0))
        version = tk.Label(
            top,
            text=f"v{current_version()}",
            bg=CARD,
            fg=MUTED,
            padx=12,
            pady=6,
            font=("Cascadia Mono", 9),
        )
        version.pack(side="right", anchor="n")

    def _set_status(self, text: str, color: str) -> None:
        self.connection_var.set(text)
        self.status_color = color
        if hasattr(self, "side_dot") and self.side_dot.winfo_exists():
            self.side_dot.configure(fg=color)
        for attr in ("hero_dot", "header_dot"):
            widget = getattr(self, attr, None)
            if widget and widget.winfo_exists():
                widget.configure(fg=color)

    def _summary_card(self, parent: tk.Misc, label: str, value: str | tk.StringVar, hint: str) -> tk.Frame:
        card = self._card(parent, padding=17)
        tk.Label(card, text=label.upper(), bg=CARD, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        options: dict[str, Any] = {"bg": CARD, "fg": TEXT, "font": ("Segoe UI Semibold", 14)}
        if isinstance(value, tk.StringVar):
            options["textvariable"] = value
        else:
            options["text"] = value
        tk.Label(card, **options).pack(anchor="w", pady=(8, 3))
        tk.Label(card, text=hint, bg=CARD, fg=MUTED, font=("Segoe UI", 8), wraplength=190, justify="left").pack(anchor="w")
        return card

    def _build_overview(self) -> None:
        self._header("Этот компьютер", "Агент XASS, локальные ресурсы и соединение")
        self.metric_bars = {}

        identity = tk.Frame(self.content, bg=BG)
        identity.pack(fill="x", pady=(3, 18))
        identity.columnconfigure(0, weight=3)
        identity.columnconfigure(1, weight=2)

        primary = tk.Frame(identity, bg=BG)
        primary.grid(row=0, column=0, sticky="nsew")
        computer = tk.Canvas(primary, width=150, height=118, bg=BG, highlightthickness=0)
        computer.pack(side="left", padx=(8, 30))
        computer.create_rectangle(25, 15, 125, 82, outline=TEXT, width=3)
        computer.create_line(75, 82, 75, 101, fill=TEXT, width=3)
        computer.create_line(49, 102, 101, 102, fill=TEXT, width=3)
        computer.create_oval(72, 46, 78, 52, fill=ACCENT, outline=ACCENT)
        copy = tk.Frame(primary, bg=BG)
        copy.pack(side="left", fill="y", pady=(10, 0))
        tk.Label(copy, text=self.name_var.get(), bg=BG, fg=TEXT, font=("Segoe UI Semibold", 24)).pack(anchor="w")
        status_line = tk.Frame(copy, bg=BG)
        status_line.pack(anchor="w", pady=(12, 0))
        self.hero_dot = tk.Label(status_line, text="●", bg=BG, fg=self.status_color, font=("Segoe UI", 12))
        self.hero_dot.pack(side="left")
        tk.Label(status_line, textvariable=self.connection_var, bg=BG, fg=GREEN, font=("Segoe UI Semibold", 13)).pack(side="left", padx=(8, 0))
        channel = "TLS-соединение" if self.server_var.get().lower().startswith("https://") else "Канал с персональным API-ключом"
        tk.Label(copy, text=channel, bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w", pady=(8, 0))

        system_info = tk.Frame(identity, bg=BG)
        system_info.grid(row=0, column=1, sticky="nsew", padx=(28, 0))
        self._connection_row(system_info, "Имя компьютера", socket.gethostname())
        self._connection_row(system_info, "Пользователь", os.environ.get("USERNAME") or "—")
        self._connection_row(system_info, "ОС", f"Windows {platform.release()}" if os.name == "nt" else platform.system())
        self._connection_row(system_info, "Время работы", self.uptime_var)
        self._connection_row(system_info, "Локальное время", self.local_time_var)

        tk.Frame(self.content, bg=LINE, height=1).pack(fill="x")
        middle = tk.Frame(self.content, bg=BG)
        middle.pack(fill="x", pady=22)
        middle.columnconfigure(0, weight=1, uniform="middle")
        middle.columnconfigure(1, weight=1, uniform="middle")

        resources = tk.Frame(middle, bg=BG)
        resources.grid(row=0, column=0, sticky="nsew", padx=(18, 40))
        tk.Label(resources, text="Локальные ресурсы", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 12))
        self._metric_row(resources, "CPU", self.cpu_var, ACCENT, "cpu")
        self._metric_row(resources, "Память", self.memory_var, ACCENT, "memory", self.memory_detail_var)
        self._metric_row(resources, "Диск", self.disk_var, ACCENT, "disk", self.disk_detail_var)

        connection = tk.Frame(middle, bg=BG, highlightbackground=LINE, highlightthickness=0)
        connection.grid(row=0, column=1, sticky="nsew", padx=(40, 18))
        tk.Label(connection, text="Подключение", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 12))
        server = normalize_server_url(self.server_var.get())
        protocol = "TLS" if server.lower().startswith("https://") else "HTTP"
        self._connection_row(connection, "Сервер", server)
        self._connection_row(connection, "Протокол", protocol)
        self._connection_row(connection, "Статус", self.server_state_var, GREEN)
        self._connection_row(connection, "Версия приложения", current_version())
        self._connection_row(connection, "Версия агента", self.agent_version_var)
        self._connection_row(connection, "Задержка", self.latency_var)
        self._connection_row(connection, "Последний heartbeat", self.last_seen_var)
        self._connection_row(connection, "Обновление", self.update_state_var)
        self._connection_row(connection, "Автообновления", "Включены (подписанные)" if self.auto_update_var.get() else "Выключены", GREEN if self.auto_update_var.get() else AMBER)

        tk.Frame(self.content, bg=LINE, height=1).pack(fill="x")
        events_head = tk.Frame(self.content, bg=BG)
        events_head.pack(fill="x", pady=(18, 10))
        tk.Label(events_head, text="Последние события", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 14)).pack(side="left")
        self._button(events_head, "Открыть диагностику", lambda: self.show_view("diagnostics"), kind="ghost").pack(side="right")
        self.overview_log = DarkScrolledText(
            self.content,
            height=8,
            bg=BG,
            fg="#c7c9ce",
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightbackground=LINE,
            highlightthickness=1,
            font=("Cascadia Mono", 9),
            padx=18,
            pady=13,
        )
        self.overview_log.pack(fill="both", expand=True)
        self._style_scrolled_text(self.overview_log)
        for line in self.history[-120:]:
            self.overview_log.insert("end", line + "\n")
        self.overview_log.see("end")

    def _metric_row(
        self,
        parent: tk.Misc,
        label: str,
        value: tk.StringVar,
        color: str,
        key: str,
        detail: tk.StringVar | None = None,
    ) -> None:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=10)
        head = tk.Frame(row, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text=label, bg=BG, fg=TEXT, font=("Segoe UI Semibold", 11)).pack(side="left")
        tk.Label(head, textvariable=value, bg=BG, fg=TEXT, font=("Segoe UI Semibold", 13)).pack(side="right")
        track = tk.Canvas(row, height=5, bg="#2b2d31", highlightthickness=0)
        track.pack(fill="x", pady=(8, 3))
        bar = track.create_rectangle(0, 0, 0, 5, fill=color, outline=color)
        self.metric_bars[key] = (track, bar)
        if detail is not None:
            tk.Label(row, textvariable=detail, bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="e")

    def _connection_row(self, parent: tk.Misc, label: str, value: str | tk.StringVar, color: str = TEXT) -> None:
        parent_bg = str(parent.cget("bg"))
        row = tk.Frame(parent, bg=parent_bg)
        row.pack(fill="x", pady=6)
        tk.Label(row, text=label, bg=parent_bg, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        options: dict[str, Any] = {"textvariable": value} if isinstance(value, tk.StringVar) else {"text": value}
        tk.Label(row, bg=parent_bg, fg=color, font=("Segoe UI Semibold", 9), wraplength=260, **options).pack(side="right")

    def _refresh_local_metrics(self) -> None:
        try:
            values = {
                "cpu": float(psutil.cpu_percent(interval=None)),
                "memory": float(psutil.virtual_memory().percent),
                "disk": float(psutil.disk_usage("C:\\" if os.name == "nt" else "/").percent),
            }
            self.cpu_var.set(f"{values['cpu']:.0f}%")
            self.memory_var.set(f"{values['memory']:.0f}%")
            self.disk_var.set(f"{values['disk']:.0f}%")
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage("C:\\" if os.name == "nt" else "/")
            self.memory_detail_var.set(f"{memory.used / (1024**3):.1f} / {memory.total / (1024**3):.1f} ГБ")
            self.disk_detail_var.set(f"{disk.used / (1024**3):.0f} / {disk.total / (1024**3):.0f} ГБ")
            uptime = max(0, int(time.time() - psutil.boot_time()))
            days, remainder = divmod(uptime, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes = remainder // 60
            self.uptime_var.set(f"{days} д. {hours} ч. {minutes} мин.")
            self.local_time_var.set(datetime.now().strftime("%d.%m.%Y %H:%M:%S"))
            for key, percent in values.items():
                item = self.metric_bars.get(key)
                if not item:
                    continue
                canvas, bar = item
                if not canvas.winfo_exists():
                    continue
                width = max(1, canvas.winfo_width())
                canvas.coords(bar, 0, 0, width * min(100.0, max(0.0, percent)) / 100.0, 6)
        except (OSError, psutil.Error, tk.TclError):
            pass
        if not self._closing:
            self.root.after(1800, self._refresh_local_metrics)

    def _field(self, parent: tk.Misc, label: str, variable: tk.StringVar, *, secret: bool = False) -> tk.Entry:
        tk.Label(parent, text=label.upper(), bg=CARD, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(13, 6))
        border = tk.Frame(parent, bg=LINE, padx=1, pady=1)
        border.pack(fill="x")
        entry = tk.Entry(
            border,
            textvariable=variable,
            show="•" if secret else "",
            bg=FIELD,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=ACCENT,
            relief="flat",
            borderwidth=0,
            font=("Cascadia Mono", 10) if not secret else ("Segoe UI", 10),
        )
        entry.pack(fill="x", ipady=10, padx=11)
        return entry

    def _build_connection(self) -> None:
        self._header("Подключение", "Самый быстрый способ — импортировать конфиг из Telegram Mini App")
        columns = tk.Frame(self.content, bg=BG)
        columns.pack(fill="both", expand=True)
        columns.columnconfigure(0, weight=1, uniform="connect")
        columns.columnconfigure(1, weight=1, uniform="connect")
        columns.rowconfigure(0, weight=1)

        quick = self._card(columns, padding=23)
        quick.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(quick, text="БЫСТРОЕ ПОДКЛЮЧЕНИЕ", bg=CARD, fg=ACCENT, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(quick, text="Импортировать конфиг", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 20)).pack(anchor="w", pady=(12, 5))
        tk.Label(
            quick,
            text="В Mini App откройте «Агенты» → «Подключить ПК» и скачайте xass-connect.json. Адрес сервера и одноразовый ключ уже будут внутри.",
            bg=CARD,
            fg=MUTED,
            justify="left",
            wraplength=330,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(0, 20))
        self._button(quick, "Выбрать xass-connect.json", self.import_connection_file, kind="primary").pack(fill="x")
        self._button(quick, "Вставить JSON из буфера", self.paste_connection, kind="secondary").pack(fill="x", pady=(9, 0))
        status_box = tk.Frame(quick, bg=FIELD, padx=14, pady=12, highlightbackground=LINE, highlightthickness=1)
        status_box.pack(side="bottom", fill="x", pady=(22, 0))
        tk.Label(status_box, text="●", bg=FIELD, fg=GREEN, font=("Segoe UI", 10)).pack(side="left", anchor="n")
        tk.Label(
            status_box,
            textvariable=self.import_status_var,
            bg=FIELD,
            fg="#aab9ca",
            justify="left",
            wraplength=285,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(8, 0), fill="x", expand=True)
        if DND_FILES is not None and hasattr(quick, "drop_target_register"):
            quick.drop_target_register(DND_FILES)
            quick.dnd_bind("<<Drop>>", self._drop_connection_file)

        manual = self._card(columns, padding=23)
        manual.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        tk.Label(manual, text="РУЧНАЯ НАСТРОЙКА", bg=CARD, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(manual, text="Адрес и одноразовый ключ", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 17)).pack(anchor="w", pady=(10, 2))
        self._field(manual, "Адрес сервера или IP", self.server_var)
        self._field(manual, "Имя компьютера", self.name_var)
        self._field(manual, "Одноразовый ключ", self.pair_var)
        self._field(manual, "Интервал heartbeat, сек", self.interval_var)
        check = tk.Checkbutton(
            manual,
            text="Устанавливать обновления автоматически",
            variable=self.auto_update_var,
            bg=CARD,
            fg=TEXT,
            activebackground=CARD,
            activeforeground=TEXT,
            selectcolor=FIELD,
            relief="flat",
            borderwidth=0,
            font=("Segoe UI", 9),
        )
        check.pack(anchor="w", pady=(17, 15))
        actions = tk.Frame(manual, bg=CARD)
        actions.pack(fill="x", side="bottom")
        self._button(actions, "Сохранить", self.save_settings, kind="secondary").pack(side="left")
        self.pair_button = self._button(actions, "Подключить", self.pair, kind="primary")
        self.pair_button.pack(side="right")

    def _build_settings(self) -> None:
        self._header("Настройки", "Поведение агента и обслуживание приложения")
        columns = tk.Frame(self.content, bg=BG)
        columns.pack(fill="both", expand=True)
        columns.columnconfigure(0, weight=1, uniform="settings")
        columns.columnconfigure(1, weight=1, uniform="settings")

        runtime = self._card(columns, padding=23)
        runtime.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(runtime, text="АГЕНТ", bg=CARD, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(runtime, text="Фоновая работа", bg=CARD, fg=TEXT, font=("Segoe UI", 20)).pack(anchor="w", pady=(11, 4))
        tk.Label(runtime, text="Окно можно свернуть — heartbeat и обновления продолжат работать.", bg=CARD, fg=MUTED, justify="left", wraplength=360, font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 18))
        check = tk.Checkbutton(runtime, text="Устанавливать подписанные обновления автоматически", variable=self.auto_update_var, bg=CARD, fg=TEXT, activebackground=CARD, activeforeground=TEXT, selectcolor=FIELD, relief="flat", borderwidth=0, font=("Segoe UI", 9))
        check.pack(anchor="w", pady=(6, 14))
        self._field(runtime, "Интервал heartbeat, секунд", self.interval_var)
        self._field(runtime, "Папка локального архива", self.archive_folder_var)
        self._button(runtime, "Выбрать папку архива", self.choose_archive_folder, kind="ghost").pack(fill="x", pady=(9, 0))
        self._field(runtime, "Лимит архива, ГБ (пусто — без лимита)", self.archive_max_gb_var)
        self._field(runtime, "Хранить медиа, дней (пусто — бессрочно)", self.archive_retention_days_var)
        self._button(runtime, "Сохранить настройки", self.save_settings, kind="primary").pack(fill="x", pady=(20, 0))

        maintenance = self._card(columns, padding=23)
        maintenance.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        tk.Label(maintenance, text="ОБСЛУЖИВАНИЕ", bg=CARD, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(maintenance, text="Клиент XASS", bg=CARD, fg=TEXT, font=("Segoe UI", 20)).pack(anchor="w", pady=(11, 18))
        self._connection_row(maintenance, "Версия", current_version())
        self._connection_row(maintenance, "Ревизия", current_revision()[:16] or "локальная")
        self._connection_row(maintenance, "Python", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
        self._connection_row(maintenance, "Автозапуск", "Windows Startup")
        self._button(maintenance, "Проверить обновление", self.check_update, kind="ghost").pack(fill="x", pady=(22, 9))
        self._button(maintenance, "Перезапустить агент", self.restart_agent).pack(fill="x")

    def _build_updates(self) -> None:
        self._header("Обновления", "Проверка, загрузка, установка и автоматический откат")
        state = load_update_state()
        result: dict[str, Any] = {}
        try:
            loaded = json.loads(UPDATE_RESULT.read_text(encoding="utf-8"))
            result = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError, TypeError):
            pass
        card = self._card(self.content, padding=22)
        card.pack(fill="x", pady=(0, 16))
        tk.Label(card, text="СОСТОЯНИЕ ОБНОВЛЕНИЯ", bg=CARD, fg=ACCENT, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(card, textvariable=self.update_state_var, bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 18)).pack(anchor="w", pady=(10, 4))
        message = str(state.get("message") or result.get("message") or "Обновление ещё не запускалось")
        tk.Label(card, text=message, bg=CARD, fg=MUTED, justify="left", wraplength=760, font=("Segoe UI", 10)).pack(anchor="w")
        details = tk.Frame(card, bg=CARD)
        details.pack(fill="x", pady=(18, 0))
        self._connection_row(details, "Текущая версия", current_version())
        self._connection_row(details, "Ревизия", current_revision()[:16] or "локальная")
        self._connection_row(details, "Канал", "stable")
        self._connection_row(details, "Автообновления", "включены" if self.auto_update_var.get() else "выключены")
        self._connection_row(details, "Последний итог", "успешно" if result.get("ok") else ("ошибка / откат" if result else "—"))
        actions = tk.Frame(card, bg=CARD)
        actions.pack(fill="x", pady=(20, 0))
        self.update_button = self._button(actions, "Проверить обновление", self.check_update, kind="primary")
        self.update_button.pack(side="left")
        self._button(actions, "Перезапустить только агент", self.restart_agent, kind="secondary").pack(side="left", padx=(10, 0))

        safety = self._card(self.content, padding=20)
        safety.pack(fill="x")
        tk.Label(safety, text="КАК XASS ОБНОВЛЯЕТСЯ", bg=CARD, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(
            safety,
            text=(
                "Manifest подписывается персональным ключом агента. Клиент проверяет revision, размер и SHA-256 "
                "скачанного файла, запускает внешний helper, сохраняет config.json и архив. Если новая версия "
                "не проходит локальный health-check или завершается сразу после запуска, предыдущая версия возвращается автоматически."
            ),
            bg=CARD,
            fg=MUTED,
            justify="left",
            wraplength=850,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(10, 0))

    def _build_diagnostics(self) -> None:
        self._header("Диагностика", "Безопасный статус GUI, агента, сети и updater")
        card = self._card(self.content, padding=21)
        card.pack(fill="x", pady=(0, 14))
        columns = tk.Frame(card, bg=CARD)
        columns.pack(fill="x")
        left = tk.Frame(columns, bg=CARD)
        right = tk.Frame(columns, bg=CARD)
        left.pack(side="left", fill="both", expand=True, padx=(0, 24))
        right.pack(side="left", fill="both", expand=True)
        self._connection_row(left, "GUI", "работает")
        self._connection_row(left, "Агент", self.connection_var)
        self._connection_row(left, "PID агента", self.agent_pid_var)
        self._connection_row(left, "Версия", current_version())
        self._connection_row(left, "Путь установки", str(Path(sys.executable).resolve()))
        self._connection_row(right, "Конфигурация", str(DATA_ROOT / "config.json"))
        self._connection_row(right, "Архив", str(archive_root(self.config)))
        self._connection_row(right, "Endpoint", f"{str(self.config.get('server_url') or '').rstrip('/')}/agent/heartbeat")
        self._connection_row(right, "Heartbeat", self.last_seen_var)
        self._connection_row(right, "Updater", self.update_state_var)
        self._connection_row(right, "Последняя ошибка", self.last_error_var, RED if self.last_error_var.get() != "Ошибок нет" else GREEN)
        actions = tk.Frame(card, bg=CARD)
        actions.pack(fill="x", pady=(20, 0))
        self._button(actions, "Проверить соединение", self.check_connection, kind="primary").pack(side="left")
        self._button(actions, "Перезапустить агент", self.restart_agent, kind="secondary").pack(side="left", padx=(9, 0))
        self._button(actions, "Экспорт отчёта", self.export_diagnostics, kind="ghost").pack(side="right")

        self.full_log = DarkScrolledText(
            self.content,
            height=15,
            bg=CARD,
            fg="#9cadbf",
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightbackground=LINE,
            highlightthickness=1,
            font=("Cascadia Mono", 9),
            padx=16,
            pady=14,
        )
        self.full_log.pack(fill="both", expand=True)
        self._style_scrolled_text(self.full_log)
        rows = self.history[-300:] or read_log_tail(300)
        for line in rows:
            self.full_log.insert("end", line + "\n")
        self.full_log.configure(state="disabled")

    def check_connection(self) -> None:
        self._set_status("Проверка связи…", AMBER)

        def worker() -> None:
            try:
                server = discover_backend_url(str(self.config.get("server_url") or self.server_var.get()))
                with httpx.Client(timeout=10, trust_env=bool(self.config.get("trust_env_proxy", False))) as client:
                    response = client.get(f"{server.rstrip('/')}/health")
                    response.raise_for_status()
                self.root.after(0, lambda: self._connection_checked(True, f"Сервер доступен: {server}"))
            except Exception as exc:
                self.root.after(0, lambda error=str(exc): self._connection_checked(False, error))

        threading.Thread(target=worker, daemon=True).start()

    def _connection_checked(self, ok: bool, detail: str) -> None:
        if ok:
            self.server_state_var.set("Доступен")
            self._set_status("В сети", GREEN)
            self._log(detail)
        else:
            self.server_state_var.set("Ошибка")
            self.last_error_var.set(detail[-240:])
            self._set_status("Нет связи", RED)
            messagebox.showerror("XASS", f"Проверка соединения не пройдена:\n{detail}")

    @staticmethod
    def _redact_log(line: str) -> str:
        value = re.sub(r"(?i)(x-api-key|api[_-]?key|token|password)(\s*[:=]\s*)\S+", r"\1\2[скрыто]", str(line))
        return re.sub(r"\b(?:iph_|agt_|pair_)[A-Za-z0-9_-]{12,}\b", "[скрыто]", value)

    def export_diagnostics(self) -> None:
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить отчёт XASS",
            defaultextension=".json",
            filetypes=(("JSON", "*.json"),),
            initialfile=f"xass-diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json",
        )
        if not selected:
            return
        status = load_agent_status() or {}
        report = {
            "created_at": datetime.now().astimezone().isoformat(),
            "app_version": current_version(),
            "agent_version": status.get("agent_version") or current_version(),
            "revision": current_revision(),
            "gui_pid": os.getpid(),
            "agent_pid": status.get("process_id"),
            "install_path": str(Path(sys.executable).resolve()),
            "config_path": str(DATA_ROOT / "config.json"),
            "archive_path": str(archive_root(self.config)),
            "server_endpoint": f"{str(self.config.get('server_url') or '').rstrip('/')}/agent/heartbeat",
            "heartbeat_state": status.get("state"),
            "heartbeat_updated_at": status.get("updated_at"),
            "latency_ms": status.get("latency_ms"),
            "last_error": status.get("last_error") or "",
            "updater": load_update_state(),
            "logs": [self._redact_log(line) for line in (self.history[-100:] or read_log_tail(100))],
        }
        try:
            Path(selected).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("XASS", f"Не удалось сохранить отчёт:\n{exc}")
            return
        messagebox.showinfo("XASS", "Диагностический отчёт сохранён без ключей и приватных сообщений.")

    def _build_logs(self) -> None:
        self._header("Журнал событий", "Heartbeat, команды сервера и установка обновлений")
        toolbar = tk.Frame(self.content, bg=BG)
        toolbar.pack(fill="x", pady=(0, 10))
        self._button(toolbar, "Очистить экран", self._clear_log_view, kind="ghost").pack(side="right")
        self.full_log = DarkScrolledText(
            self.content,
            bg=CARD,
            fg="#9cadbf",
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightbackground=LINE,
            highlightthickness=1,
            font=("Cascadia Mono", 9),
            padx=16,
            pady=14,
        )
        self.full_log.pack(fill="both", expand=True)
        self._style_scrolled_text(self.full_log)
        for line in self.history[-500:]:
            self.full_log.insert("end", line + "\n")
        self.full_log.configure(state="disabled")

    def _build_archive(self) -> None:
        self._header("Архив сообщений", "Тексты и медиа хранятся локально на этом компьютере")
        status = archive_status(self.config)
        summary = self._card(self.content, padding=18)
        summary.pack(fill="x", pady=(0, 14))
        tk.Label(summary, text="ЛОКАЛЬНОЕ ХРАНИЛИЩЕ", bg=CARD, fg=ACCENT, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        sync_state = "ожидает повторной доставки" if status.get("pending_retry") else "синхронизирован"
        last_sync = str(status.get("last_sync_at") or "никогда").replace("T", " ")[:19]
        error_line = f"\nПоследняя ошибка: {status.get('last_error')}" if status.get("last_error") else ""
        self.archive_state_var.set(
            f"Папка: {status.get('folder')}\nСинхронизировано событий: {status.get('cursor', 0)} · "
            f"Индекс: {int(status.get('database_size', 0)) / 1024:.1f} КБ\n"
            f"Состояние: {sync_state} · последняя синхронизация: {last_sync} · "
            f"свободно {int(status.get('free_bytes', 0)) / (1024 ** 3):.1f} ГБ{error_line}"
        )
        tk.Label(summary, textvariable=self.archive_state_var, bg=CARD, fg=MUTED, justify="left", font=("Segoe UI", 10)).pack(anchor="w", pady=(9, 12))
        actions = tk.Frame(summary, bg=CARD)
        actions.pack(fill="x")
        self._button(actions, "Открыть папку", self.open_archive_folder, kind="primary").pack(side="left")
        self._button(actions, "Выбрать другую", self.choose_archive_folder, kind="secondary").pack(side="left", padx=(9, 0))
        self._button(actions, "Тест записи", self.test_archive_folder, kind="ghost").pack(side="left", padx=(9, 0))
        self._button(actions, "Очистить медиа", self.cleanup_archive_files, kind="ghost").pack(side="left", padx=(9, 0))
        if self._archive_moving:
            self._button(actions, "Отменить перенос", self._archive_cancel.set, kind="danger").pack(side="right")

        self.archive_messages = DarkScrolledText(
            self.content,
            bg=CARD,
            fg="#d8dce4",
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightbackground=LINE,
            highlightthickness=1,
            font=("Segoe UI", 10),
            padx=18,
            pady=14,
        )
        self.archive_messages.pack(fill="both", expand=True)
        self._style_scrolled_text(self.archive_messages)
        rows = conversation_rows(self.config, 500)
        for row in reversed(rows):
            timestamp = str(row.get("message_date") or row.get("updated_at") or "").replace("T", " ")[:16]
            title = row.get("chat_title") or row.get("from_username") or row.get("chat_id")
            marker = "  [УДАЛЕНО]" if row.get("deleted") else ""
            if row.get("forwarded_from"):
                marker += f"  [ПЕРЕСЛАНО ОТ {row.get('forwarded_from')}]"
            if row.get("reply_to_message_id"):
                marker += f"  [ОТВЕТ НА #{row.get('reply_to_message_id')}]"
            direction = "→" if row.get("direction") == "outgoing" else "←"
            text = str(row.get("text_content") or "Медиа / сообщение без текста").replace("\n", " ")
            media = f"  · файлов: {row.get('media_count')}" if row.get("media_count") else ""
            self.archive_messages.insert("end", f"{timestamp}  {direction} {title}{marker}{media}\n{text}\n\n")
        if not rows:
            self.archive_messages.insert("end", "Архив пуст. Включите этот компьютер как цель хранения в XASS Mini App.")
        self.archive_messages.configure(state="disabled")

    def choose_archive_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="Папка локального архива XASS", initialdir=str(archive_root(self.config)))
        if not selected:
            return
        destination = Path(selected).expanduser().resolve()
        try:
            free = self._validate_archive_folder(destination)
        except OSError as exc:
            messagebox.showerror("XASS", f"В выбранную папку нельзя записывать:\n{exc}")
            return
        source = archive_root(self.config).resolve()
        has_archive = source != destination and source.is_dir() and any(source.iterdir())
        if has_archive and messagebox.askyesno(
            "XASS",
            "Скопировать существующий архив в новую папку? Исходные данные останутся на месте до успешной проверки.",
            parent=self.root,
        ):
            self._start_archive_transfer(source, destination)
            return
        self._finish_archive_folder_change(destination, free)

    def _validate_archive_folder(self, folder: Path) -> int:
        folder.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".xass-write-test-", dir=folder)
        try:
            os.write(descriptor, "Проверка XASS".encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            Path(temporary).unlink(missing_ok=True)
        return shutil.disk_usage(folder).free

    def test_archive_folder(self) -> None:
        try:
            free = self._validate_archive_folder(archive_root(self.config))
        except OSError as exc:
            messagebox.showerror("XASS", f"Ошибка тестовой записи:\n{exc}")
            return
        messagebox.showinfo("XASS", f"Запись работает. Свободно {free / (1024 ** 3):.1f} ГБ.")

    def _start_archive_transfer(self, source: Path, destination: Path) -> None:
        if self._archive_moving:
            return
        self._archive_moving = True
        self._archive_cancel.clear()
        self.archive_state_var.set("Подготовка безопасного переноса…")

        def worker() -> None:
            try:
                files = [path for path in source.rglob("*") if path.is_file()]
                total = sum(path.stat().st_size for path in files)
                copied = 0
                for item in files:
                    if self._archive_cancel.is_set():
                        raise InterruptedError("перенос отменён")
                    relative = item.relative_to(source)
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + ".xass-copying")
                    try:
                        shutil.copy2(item, temporary)
                        if temporary.stat().st_size != item.stat().st_size:
                            raise OSError(f"не удалось проверить {relative}")
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    copied += item.stat().st_size
                    percent = int(copied * 100 / total) if total else 100
                    self.root.after(0, lambda p=percent: self.archive_state_var.set(f"Перенос архива: {p}%"))
                free = self._validate_archive_folder(destination)
                self.root.after(0, lambda: self._finish_archive_folder_change(destination, free))
            except InterruptedError:
                self.root.after(0, lambda: self.archive_state_var.set("Перенос отменён. Исходный архив не изменён."))
            except Exception as exc:
                self.root.after(0, lambda error=str(exc): messagebox.showerror("XASS", f"Не удалось перенести архив:\n{error}"))
            finally:
                self._archive_moving = False

        threading.Thread(target=worker, daemon=True).start()

    def _finish_archive_folder_change(self, destination: Path, free: int) -> None:
        self.archive_folder_var.set(str(destination))
        self.config["archive_folder"] = str(destination)
        save_config(self.config)
        self._log(f"Папка архива: {destination} · свободно {free / (1024 ** 3):.1f} ГБ")
        self.restart_agent()
        if self.current_view == "archive":
            self.show_view("archive")

    def open_archive_folder(self) -> None:
        folder = archive_root(self.config)
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(folder))
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def cleanup_archive_files(self) -> None:
        if not messagebox.askyesno(
            "XASS",
            "Удалить локальные медиафайлы архива? Тексты сообщений останутся. Это действие нельзя отменить.",
            parent=self.root,
        ):
            return
        result = cleanup_archive(self.config, force=True)
        self._log(f"Очищено локальных файлов: {result['removed_files']}, освобождено: {result['freed_bytes']} байт")
        self.show_view("archive")

    def _clear_log_view(self) -> None:
        if hasattr(self, "full_log") and self.full_log.winfo_exists():
            self.full_log.configure(state="normal")
            self.full_log.delete("1.0", "end")
            self.full_log.configure(state="disabled")

    def save_settings(self) -> None:
        try:
            interval = max(5, int(self.interval_var.get().strip()))
            max_gb = max(0.0, float(self.archive_max_gb_var.get().strip() or 0))
            retention_days = max(0, int(self.archive_retention_days_var.get().strip() or 0))
        except ValueError:
            messagebox.showerror("XASS", "Интервал, лимит и срок хранения должны быть числами")
            return
        self.config.update(
            {
                "server_url": normalize_server_url(self.server_var.get()),
                "source_name": self.name_var.get().strip() or socket.gethostname(),
                "source_type": "PC_AGENT",
                "interval_sec": interval,
                "auto_update": self.auto_update_var.get(),
                "archive_folder": self.archive_folder_var.get().strip(),
                "archive_max_gb": max_gb,
                "archive_retention_days": retention_days,
                "desktop_managed": True,
            }
        )
        save_config(self.config)
        self._log("Настройки сохранены")
        self.restart_agent()

    def import_connection_file(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Выберите файл подключения XASS",
            filetypes=(("Подключение XASS", "*.xass *.xass-connect *.json"), ("Все файлы", "*.*")),
        )
        if not selected:
            return
        self.import_connection_path(Path(selected))

    def _drop_connection_file(self, event: Any) -> None:
        try:
            paths = self.root.tk.splitlist(str(event.data))
        except (tk.TclError, AttributeError):
            paths = (str(getattr(event, "data", "")),)
        selected = next((Path(item) for item in paths if str(item).strip()), None)
        if selected is None:
            return
        self.import_connection_path(selected.expanduser().resolve())

    def import_connection_path(self, selected: Path) -> None:
        try:
            profile = load_connection_file(selected)
        except ValueError as exc:
            messagebox.showerror("XASS", str(exc))
            return
        self._apply_connection_profile(profile, selected.name)

    def paste_connection(self) -> None:
        try:
            profile = parse_connection_text(self.root.clipboard_get())
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror("XASS", f"Не удалось прочитать конфиг из буфера:\n{exc}")
            return
        self._apply_connection_profile(profile, "буфер обмена")

    def _apply_connection_profile(self, profile: ConnectionProfile, source: str) -> None:
        self.server_var.set(profile.server_url)
        self.pair_var.set(profile.pair_code)
        if profile.source_name:
            self.name_var.set(profile.source_name)
        self.auto_update_var.set(profile.auto_update)
        expires = profile.expires_at.astimezone().strftime("%H:%M")
        self.import_status_var.set(f"Конфиг из {source} принят · ключ действует до {expires}. Подключаю…")
        self._log(f"Импортирован файл подключения: {source}")
        self.root.after(180, self.pair)

    def pair(self) -> None:
        code = self.pair_var.get().strip()
        if not code:
            messagebox.showwarning("XASS", "Введите одноразовый ключ или импортируйте xass-connect.json")
            return
        if hasattr(self, "pair_button") and self.pair_button.winfo_exists():
            self.pair_button.configure(state="disabled", text="Подключение…")
        self._set_status("Подключение…", AMBER)
        server_input = self.server_var.get()
        source_name = self.name_var.get().strip() or socket.gethostname()
        try:
            interval = max(5, int(self.interval_var.get() or 30))
        except ValueError:
            self._pair_failed("Интервал heartbeat должен быть числом")
            return
        auto_update = self.auto_update_var.get()

        def worker() -> None:
            try:
                server = discover_backend_url(normalize_server_url(server_input))
                result = claim_pair_code(
                    server_url=server,
                    pair_code=code,
                    source_name=source_name,
                    source_type="PC_AGENT",
                )
                self.config.update(
                    {
                        "server_url": server,
                        "source_name": str(result.get("source_name") or source_name),
                        "source_type": "PC_AGENT",
                        "api_key": str(result.get("agent_api_key") or ""),
                        "interval_sec": interval,
                        "auto_update": auto_update,
                        "desktop_managed": True,
                    }
                )
                save_config(self.config)
                self.root.after(0, lambda: self._paired_ok(server))
            except Exception as exc:
                self.root.after(0, lambda error=str(exc): self._pair_failed(error))

        threading.Thread(target=worker, daemon=True).start()

    def _pair_failed(self, error: str) -> None:
        self._set_status("Ошибка подключения", RED)
        if hasattr(self, "pair_button") and self.pair_button.winfo_exists():
            self.pair_button.configure(state="normal", text="Подключить")
        messagebox.showerror("XASS", f"Не удалось подключить агент:\n{error}")

    def _paired_ok(self, server: str) -> None:
        self.server_var.set(server)
        self.name_var.set(str(self.config.get("source_name") or ""))
        self.pair_var.set("")
        self.import_status_var.set("Компьютер успешно привязан. Персональный API-ключ сохранён локально.")
        self._log("Pairing выполнен, персональный ключ сохранён локально")
        self._set_status("Подключён", GREEN)
        self.restart_agent()
        self.show_view("overview")
        messagebox.showinfo("XASS", "Компьютер подключён к серверу")

    def start_agent(self) -> None:
        if self._closing:
            return
        if self.process and self.process.poll() is None:
            return
        # Always launch from the durable config. This also makes a fast restart
        # pick up a config imported or repaired outside the current GUI object.
        self.config = ensure_minimal_defaults(load_config())
        self.server_var.set(str(self.config.get("server_url") or self.server_var.get()))
        existing_status = load_agent_status() or {}
        try:
            existing_pid = int(existing_status.get("process_id") or 0)
            existing = psutil.Process(existing_pid) if existing_pid > 0 else None
            command = " ".join(existing.cmdline()).casefold() if existing is not None else ""
            if existing is not None and existing.is_running() and "--agent" in command:
                self.external_agent_pid = existing_pid
                self.agent_pid_var.set(str(existing_pid))
                self._set_status("В сети" if existing_status.get("state") == "online" else "Подключение…", GREEN if existing_status.get("state") == "online" else AMBER)
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
            self.external_agent_pid = 0
        update_marker = UPDATE_MARKER
        if update_marker.exists():
            if self._update_is_running(update_marker):
                self._set_status("Устанавливается обновление…", AMBER)
                self._schedule_start(250)
                return
            update_marker.unlink(missing_ok=True)
        if not self.config.get("server_url") or not self.config.get("api_key"):
            self._set_status("Требуется подключение", AMBER)
            return
        args = (
            [sys.executable, "--agent", "--desktop-managed"]
            if is_installer_build()
            else [sys.executable, "-u", str(ROOT / "client_agent.py"), "--desktop-managed"]
        )
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        try:
            process = subprocess.Popen(
                args,
                cwd=str(DATA_ROOT if is_installer_build() else ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            self.last_error_var.set(str(exc))
            self._set_status("Агент не запущен", RED)
            self._log(f"[desktop] не удалось запустить агент: {exc}")
            return
        self.process = process
        self._agent_started_at = time.time()
        write_agent_status(
            "connecting",
            detail="Ожидание первого heartbeat",
            process_id=process.pid,
        )
        self._set_status("Подключение…", AMBER)
        threading.Thread(target=self._read_process, args=(process,), daemon=True).start()

    def _refresh_agent_status(self) -> None:
        if self._closing:
            return
        process = self.process
        active_pid = process.pid if process is not None and process.poll() is None else self.external_agent_pid
        if active_pid:
            payload = load_agent_status()
            try:
                status_pid = int((payload or {}).get("process_id") or 0)
                updated_at = float((payload or {}).get("updated_at") or 0)
            except (TypeError, ValueError):
                status_pid = 0
                updated_at = 0
            if status_pid == active_pid and (self.external_agent_pid or updated_at >= self._agent_started_at):
                state = str((payload or {}).get("state") or "").lower()
                self.agent_pid_var.set(str(status_pid))
                self.agent_version_var.set(str((payload or {}).get("agent_version") or current_version()))
                latency = float((payload or {}).get("latency_ms") or 0)
                self.latency_var.set(f"{latency:.0f} мс" if latency > 0 else "—")
                age = max(0, int(time.time() - updated_at))
                self.last_seen_var.set("только что" if age < 3 else f"{age} сек. назад")
                if state == "online":
                    self._set_status("В сети", GREEN)
                    self._restart_failures.clear()
                    self.server_state_var.set("Доступен")
                    self.last_error_var.set("Ошибок нет")
                elif state == "offline":
                    self._set_status("Нет связи", RED)
                    detail = str((payload or {}).get("detail") or "Сервер не отвечает")
                    self.last_error_var.set(str((payload or {}).get("last_error") or detail)[-240:])
                    self.server_state_var.set("Ошибка")
        self.root.after(750, self._refresh_agent_status)

    def _refresh_update_status(self) -> None:
        if self._closing:
            return
        state = load_update_state()
        phase = str(state.get("phase") or "")
        labels = {
            "checking": "Проверка обновления",
            "downloading": "Скачивание",
            "verifying": "Проверка пакета",
            "installing": "Установка",
            "restarting": "Перезапуск",
            "health-check": "Проверка запуска",
        }
        if state and update_in_progress():
            message = str(state.get("message") or labels.get(phase) or "Обновление выполняется")
            self.update_state_var.set(message)
        else:
            try:
                result = json.loads(UPDATE_RESULT.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                result = {}
            if isinstance(result, dict) and result:
                result_version = str(result.get("version") or "").strip()
                if result.get("ok") and result_version:
                    self.update_state_var.set(f"Последняя установка: {result_version} · проверка не выполнялась")
                else:
                    self.update_state_var.set(str(result.get("message") or ("Готово" if result.get("ok") else "Ошибка")))
            elif not self._update_checking:
                self.update_state_var.set("Готово к проверке")
        self.root.after(700, self._refresh_update_status)

    def _update_is_running(self, marker: Path) -> bool:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            pid = int(payload.get("pid") or 0)
            started_at = float(payload.get("started_at") or marker.stat().st_mtime)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            try:
                return time.time() - marker.stat().st_mtime < 30
            except OSError:
                return False
        if pid <= 0:
            return time.time() - started_at < 30
        try:
            process = psutil.Process(pid)
            return process.is_running() and abs(process.create_time() - started_at) < 30
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return time.time() - started_at < 600

    def _read_process(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            self.log_queue.put(line.rstrip())
        code = process.wait()
        if process.pid in self._expected_stop_pids:
            self.log_queue.put("[desktop] агент остановлен для быстрого перезапуска")
        else:
            self.log_queue.put(f"[desktop] agent stopped with code {code}")
        self.root.after(0, lambda: self._agent_exited(process, code))

    def _agent_exited(self, process: subprocess.Popen[str], code: int) -> None:
        self._expected_stop_pids.discard(process.pid)
        if self.process is not process:
            return
        self.process = None
        if code == 76:
            self._set_status("Установка обновления…", AMBER)
            self._closing = True
            self.root.after(120, self._destroy_for_update)
            return
        self._set_status("Перезапуск…" if code == 75 else "Остановлен", AMBER if code == 75 else RED)
        if code == 75:
            self._schedule_start(120)
            return
        now = time.time()
        self._restart_failures = [stamp for stamp in self._restart_failures if now - stamp < 300]
        self._restart_failures.append(now)
        if len(self._restart_failures) <= 4 and not self._closing:
            delay = min(15_000, 750 * (2 ** (len(self._restart_failures) - 1)))
            self._set_status(f"Восстановление через {delay // 1000 + 1} сек.", AMBER)
            self._schedule_start(delay)
        else:
            self.last_error_var.set(f"Агент завершился с кодом {code}; автоперезапуск остановлен")

    def _schedule_start(self, delay_ms: int) -> None:
        if self._closing:
            return
        if self._start_after_id is not None:
            try:
                self.root.after_cancel(self._start_after_id)
            except tk.TclError:
                pass
        self._start_after_id = self.root.after(delay_ms, self._run_scheduled_start)

    def _run_scheduled_start(self) -> None:
        self._start_after_id = None
        self.start_agent()

    def stop_agent(self) -> subprocess.Popen[str] | None:
        if self._start_after_id is not None:
            try:
                self.root.after_cancel(self._start_after_id)
            except tk.TclError:
                pass
            self._start_after_id = None
        process = self.process
        self.process = None
        if process and process.poll() is None:
            self._expected_stop_pids.add(process.pid)
            process.terminate()
        if self.external_agent_pid:
            try:
                psutil.Process(self.external_agent_pid).terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass
            self.external_agent_pid = 0
        self._set_status("Остановлен", RED)
        return process

    def restart_agent(self) -> None:
        external_pid = self.external_agent_pid
        process = self.stop_agent()
        self._set_status("Быстрый перезапуск…", AMBER)

        def worker() -> None:
            if process and process.poll() is None:
                try:
                    process.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=0.8)
                    except subprocess.TimeoutExpired:
                        pass
            if external_pid:
                try:
                    psutil.Process(external_pid).wait(timeout=1.5)
                except (psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.AccessDenied):
                    pass
            self.root.after(0, lambda: self._schedule_start(80))

        threading.Thread(target=worker, daemon=True).start()

    def check_update(self) -> None:
        if not self.config.get("api_key"):
            messagebox.showwarning("XASS", "Сначала подключите компьютер")
            return
        if self._update_checking or update_in_progress():
            messagebox.showinfo("XASS", "Проверка или установка обновления уже выполняется")
            return
        self._update_checking = True
        self.update_state_var.set("Проверка обновления…")
        button = getattr(self, "update_button", None)
        if button and button.winfo_exists():
            button.configure(state="disabled", text="Проверка…")

        def worker() -> None:
            try:
                server = discover_backend_url(str(self.config.get("server_url")))
                payload = build_payload(self.config)
                with create_http_client(
                    server,
                    timeout=25,
                    trust_env=bool(self.config.get("trust_env_proxy", False)),
                ) as client:
                    response = client.post(
                        f"{server}/agent/heartbeat",
                        headers={"X-Api-Key": self.config["api_key"]},
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                manifest_key = "installer_update" if is_installer_build() else "update"
                manifest = data.get(manifest_key) if isinstance(data, dict) else None
                if not isinstance(manifest, dict) or not manifest.get("available"):
                    self.root.after(0, lambda: self._update_checked(None, ""))
                    return
                self.root.after(0, lambda: self._update_checked(manifest, ""))
            except Exception as exc:
                self.root.after(0, lambda error=str(exc): self._update_checked(None, error))

        threading.Thread(target=worker, daemon=True).start()

    def _update_checked(self, manifest: dict[str, Any] | None, error: str) -> None:
        self._update_checking = False
        button = getattr(self, "update_button", None)
        if button and button.winfo_exists():
            button.configure(state="normal", text="Проверить обновление")
        if error:
            self.update_state_var.set("Ошибка проверки")
            self.last_error_var.set(error[-240:])
            messagebox.showerror("XASS", f"Не удалось проверить обновление:\n{error}")
            return
        if manifest is None:
            self.update_state_var.set("Установлена актуальная версия")
            messagebox.showinfo("XASS", "Установлена актуальная версия")
            return
        self._confirm_update(manifest)

    def _confirm_update(self, manifest: dict[str, Any]) -> None:
        version = str(manifest.get("version") or "")
        if not messagebox.askyesno("XASS", f"Доступна версия {version}. Установить сейчас?"):
            return
        # Do not let the background agent download the same package in parallel
        # with a manual update from the desktop UI.
        self.stop_agent()
        self._set_status("Скачивание обновления…", AMBER)
        self.update_state_var.set("Скачивание обновления…")

        def worker() -> None:
            try:
                version = str(manifest.get("version") or "")
                revision = str(manifest.get("revision") or "")
                with update_operation(version, revision) as operation:
                    def report(message: str) -> None:
                        self.log_queue.put(message)
                        operation.phase("downloading", message)

                    if is_installer_build():
                        installer = download_installer_update(
                            manifest,
                            api_key=str(self.config.get("api_key")),
                            trust_env=bool(self.config.get("trust_env_proxy", False)),
                            progress=report,
                        )
                        operation.phase("verifying", "Установщик проверен")
                        launch_installer_update(
                            installer,
                            wait_pid=os.getpid(),
                            expected_version=version,
                            expected_revision=revision,
                        )
                    else:
                        stage = download_update(
                            manifest,
                            api_key=str(self.config.get("api_key")),
                            trust_env=bool(self.config.get("trust_env_proxy", False)),
                            progress=report,
                        )
                        operation.phase("verifying", "Пакет проверен")
                        launch_update_helper(stage, manifest, command_id=None, restart_target="desktop", minimized=False)
                self.root.after(0, self._destroy_for_update)
            except Exception as exc:
                self.root.after(0, lambda error=str(exc): self._update_failed(error))

        threading.Thread(target=worker, daemon=True).start()

    def _update_failed(self, error: str) -> None:
        self.update_state_var.set("Ошибка обновления")
        self.last_error_var.set(error[-240:])
        messagebox.showerror("XASS", f"Обновление не установлено:\n{error}")
        self._schedule_start(120)

    def _drain_logs(self) -> None:
        try:
            while True:
                self._log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(120, self._drain_logs)

    def _log(self, line: str) -> None:
        stamped = f"{datetime.now().strftime('%H:%M:%S')}  {line}"
        append_log(stamped)
        self.history.append(stamped)
        self.history = self.history[-1000:]
        if "[pc-client] ok" in line:
            self._set_status("В сети", GREEN)
            self.last_seen_var.set("Последний heartbeat только что")
            self.server_state_var.set("Доступен")
        elif "Скачивание" in line or "Повторная загрузка" in line:
            self._set_status(line.strip(), AMBER)
            self.update_state_var.set(line.strip())
        elif "heartbeat failed" in line:
            self._set_status("Нет связи", RED)
            self.last_seen_var.set(line[-120:])
            self.server_state_var.set("Ошибка")
        for attr in ("overview_log", "full_log"):
            widget = getattr(self, attr, None)
            if widget and widget.winfo_exists():
                widget.configure(state="normal")
                widget.insert("end", stamped + "\n")
                widget.see("end")
                if attr == "full_log":
                    widget.configure(state="disabled")

    def _setup_tray(self) -> None:
        if pystray is None or Image is None:
            return
        icon_path = _resource_path("assets/xass-icon.png")
        if not icon_path.is_file():
            return
        try:
            image = Image.open(icon_path).convert("RGBA")
            menu = pystray.Menu(
                pystray.MenuItem("Открыть XASS", lambda _icon, _item: self.root.after(0, self._show_from_tray), default=True),
                pystray.MenuItem("Проверить соединение", lambda _icon, _item: self.root.after(0, self.check_connection)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выйти", lambda _icon, _item: self.root.after(0, self.exit_application)),
            )
            self.tray_icon = pystray.Icon("XASS", image, "XASS", menu)
            self.tray_icon.run_detached()
        except Exception as exc:
            self.tray_icon = None
            self._log(f"[desktop] системный трей недоступен: {exc}")

    def _show_from_tray(self) -> None:
        self._hidden_to_tray = False
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def close(self) -> None:
        # Closing the window keeps the background agent alive. A deliberate exit
        # remains available from the tray menu.
        self._hidden_to_tray = True
        if self.tray_icon is not None:
            self.root.withdraw()
            self._log("[desktop] окно скрыто в трей; агент продолжает работу")
        else:
            self.root.iconify()

    def _destroy_for_update(self) -> None:
        self._closing = True
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()

    def exit_application(self) -> None:
        self._closing = True
        self.stop_agent()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    if "--health-check" in sys.argv:
        try:
            version = current_version()
            if not version or version == "0.0.0":
                raise ValueError("version metadata is missing")
            config_path = DATA_ROOT / "config.json"
            if config_path.exists():
                payload = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("config.json is invalid")
            if "--expected-version" in sys.argv:
                index = sys.argv.index("--expected-version")
                expected = sys.argv[index + 1]
                if version != expected:
                    raise ValueError(f"expected {expected}, got {version}")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise SystemExit(1)
        raise SystemExit(0)
    if "--agent" in sys.argv:
        sys.argv = [sys.argv[0], *[arg for arg in sys.argv[1:] if arg != "--agent"]]
        from client_agent import main as agent_main

        agent_main()
        return
    configure_utf8_logging()
    parser = argparse.ArgumentParser(description="XASS desktop PC agent")
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--preview", action="store_true", help="show the interface without starting the agent")
    parser.add_argument("connection_file", nargs="?", help="XASS connection profile to import")
    args = parser.parse_args()
    _configure_windows_process()
    instance = acquire_single_instance("XASS-desktop-GUI")
    if instance is None:
        return
    try:
        root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
        app = XassDesktop(root, minimized=args.minimized, preview=args.preview)
        if args.connection_file:
            profile_path = Path(args.connection_file).expanduser().resolve()
            root.after(350, lambda: app.import_connection_path(profile_path))
        root.mainloop()
    finally:
        instance.close()


if __name__ == "__main__":
    main()
