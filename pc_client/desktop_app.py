from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable

import httpx
import psutil

from client_agent import (
    build_payload,
    claim_pair_code,
    discover_backend_url,
    ensure_minimal_defaults,
    load_config,
    normalize_server_url,
    save_config,
)
from client_update import current_revision, current_version, download_update, launch_update_helper
from connection_file import ConnectionProfile, load_connection_file, parse_connection_text

ROOT = Path(__file__).resolve().parent
BG = "#050a12"
SIDEBAR = "#08131f"
CARD = "#0a1623"
CARD_HOVER = "#0d1c2b"
FIELD = "#07111c"
LINE = "#1b2d3f"
TEXT = "#f4f7fb"
MUTED = "#8494a8"
ACCENT = "#348dfb"
ACCENT_HOVER = "#247ce7"
GREEN = "#42d979"
AMBER = "#f0b35a"
RED = "#f2656f"


class XassDesktop:
    def __init__(self, root: tk.Tk, *, minimized: bool = False) -> None:
        self.root = root
        self.root.title("XASS — Desktop Agent")
        self.root.geometry("1080x720")
        self.root.minsize(940, 620)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.option_add("*Font", ("Segoe UI", 10))

        self.config = ensure_minimal_defaults(load_config())
        self.config["desktop_managed"] = True
        self.process: subprocess.Popen[str] | None = None
        self._start_after_id: str | None = None
        self._closing = False
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
        self.connection_var = tk.StringVar(value="Остановлен")
        self.last_seen_var = tk.StringVar(value="Heartbeat ещё не получен")
        self.server_state_var = tk.StringVar(value="Не проверен")
        self.import_status_var = tk.StringVar(value="Выберите файл, скачанный в Telegram Mini App")
        self.status_color = AMBER

        self._build_shell()
        self.show_view("overview")
        self.root.after(120, self._drain_logs)
        self._schedule_start(250)
        if minimized:
            self.root.after(900, self.root.iconify)

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

    def _build_shell(self) -> None:
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(shell, bg=SIDEBAR, width=230)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=24, pady=(28, 34))
        tk.Label(brand, text="XASS", bg=SIDEBAR, fg=TEXT, font=("Segoe UI Black", 25)).pack(anchor="w")
        tk.Label(
            brand,
            text="DESKTOP AGENT",
            bg=SIDEBAR,
            fg=ACCENT,
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w", pady=(2, 0))

        for key, label in (
            ("overview", "  Обзор"),
            ("connection", "  Подключение"),
            ("logs", "  Журнал событий"),
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
                padx=22,
                pady=13,
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
        self.content = tk.Frame(body, bg=BG)
        self.content.pack(fill="both", expand=True, padx=34, pady=28)

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
        elif name == "logs":
            self._build_logs()
        else:
            self._build_overview()

    def _header(self, title: str, subtitle: str) -> None:
        top = tk.Frame(self.content, bg=BG)
        top.pack(fill="x", pady=(0, 22))
        copy = tk.Frame(top, bg=BG)
        copy.pack(side="left")
        tk.Label(copy, text=title, bg=BG, fg=TEXT, font=("Segoe UI Semibold", 25)).pack(anchor="w")
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
        self._header("Обзор", "Состояние этого компьютера и связь с XASS")

        hero = self._card(self.content, padding=22)
        hero.pack(fill="x")
        accent = tk.Frame(hero, bg=self.status_color, width=4)
        accent.pack(side="left", fill="y", padx=(0, 18))
        accent.pack_propagate(False)
        hero_copy = tk.Frame(hero, bg=CARD)
        hero_copy.pack(side="left", fill="both", expand=True)
        status_line = tk.Frame(hero_copy, bg=CARD)
        status_line.pack(anchor="w")
        self.hero_dot = tk.Label(status_line, text="●", bg=CARD, fg=self.status_color, font=("Segoe UI", 13))
        self.hero_dot.pack(side="left")
        tk.Label(
            status_line,
            textvariable=self.connection_var,
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI Semibold", 18),
        ).pack(side="left", padx=(9, 0))
        tk.Label(
            hero_copy,
            textvariable=self.last_seen_var,
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(5, 0))
        tk.Label(
            hero_copy,
            text=f"{self.name_var.get()}  ·  {self.server_var.get()}",
            bg=CARD,
            fg="#b9c6d5",
            font=("Cascadia Mono", 9),
        ).pack(anchor="w", pady=(12, 0))

        hero_actions = tk.Frame(hero, bg=CARD)
        hero_actions.pack(side="right", padx=(20, 0))
        if self.config.get("api_key"):
            self._button(hero_actions, "Перезапустить", self.restart_agent).pack(anchor="e")
            self._button(hero_actions, "Проверить обновление", self.check_update, kind="ghost").pack(anchor="e", pady=(8, 0))
        else:
            tk.Label(hero_actions, text="Компьютер ещё не привязан", bg=CARD, fg=AMBER, font=("Segoe UI Semibold", 9)).pack(anchor="e", pady=(0, 9))
            self._button(hero_actions, "Подключить компьютер", lambda: self.show_view("connection"), kind="primary").pack(anchor="e")

        summaries = tk.Frame(self.content, bg=BG)
        summaries.pack(fill="x", pady=14)
        for column in range(3):
            summaries.columnconfigure(column, weight=1, uniform="summary")
        cards = (
            self._summary_card(summaries, "Сервер", self.server_state_var, self.server_var.get()),
            self._summary_card(summaries, "Версия клиента", f"v{current_version()}", current_revision()[:12] or "ревизия появится после обновления"),
            self._summary_card(summaries, "Обновления", "Автоматически" if self.auto_update_var.get() else "Вручную", "SHA-256 · подпись · резервная копия"),
        )
        for index, card in enumerate(cards):
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0 if index == 2 else 6))

        events_head = tk.Frame(self.content, bg=BG)
        events_head.pack(fill="x", pady=(8, 10))
        tk.Label(events_head, text="Последние события", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 13)).pack(side="left")
        tk.Label(events_head, text="обновляется автоматически", bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(side="right")
        self.overview_log = ScrolledText(
            self.content,
            height=10,
            bg=CARD,
            fg="#9cadbf",
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightbackground=LINE,
            highlightthickness=1,
            font=("Cascadia Mono", 9),
            padx=16,
            pady=13,
        )
        self.overview_log.pack(fill="both", expand=True)
        for line in self.history[-120:]:
            self.overview_log.insert("end", line + "\n")
        self.overview_log.see("end")

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

    def _build_logs(self) -> None:
        self._header("Журнал событий", "Heartbeat, команды сервера и установка обновлений")
        toolbar = tk.Frame(self.content, bg=BG)
        toolbar.pack(fill="x", pady=(0, 10))
        self._button(toolbar, "Очистить экран", self._clear_log_view, kind="ghost").pack(side="right")
        self.full_log = ScrolledText(
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
        for line in self.history[-500:]:
            self.full_log.insert("end", line + "\n")
        self.full_log.configure(state="disabled")

    def _clear_log_view(self) -> None:
        if hasattr(self, "full_log") and self.full_log.winfo_exists():
            self.full_log.configure(state="normal")
            self.full_log.delete("1.0", "end")
            self.full_log.configure(state="disabled")

    def save_settings(self) -> None:
        try:
            interval = max(5, int(self.interval_var.get().strip()))
        except ValueError:
            messagebox.showerror("XASS", "Интервал должен быть числом")
            return
        self.config.update(
            {
                "server_url": normalize_server_url(self.server_var.get()),
                "source_name": self.name_var.get().strip() or socket.gethostname(),
                "source_type": "PC_AGENT",
                "interval_sec": interval,
                "auto_update": self.auto_update_var.get(),
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
            filetypes=(("XASS connection", "*.json"), ("Все файлы", "*.*")),
        )
        if not selected:
            return
        try:
            profile = load_connection_file(Path(selected))
        except ValueError as exc:
            messagebox.showerror("XASS", str(exc))
            return
        self._apply_connection_profile(profile, Path(selected).name)

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
        update_marker = ROOT / ".updates" / ".in-progress"
        if update_marker.exists():
            if self._update_is_running(update_marker):
                self._set_status("Устанавливается обновление…", AMBER)
                self._schedule_start(250)
                return
            update_marker.unlink(missing_ok=True)
        if not self.config.get("server_url") or not self.config.get("api_key"):
            self._set_status("Требуется подключение", AMBER)
            return
        args = [sys.executable, "-u", str(ROOT / "client_agent.py"), "--desktop-managed"]
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
        )
        self.process = process
        self._set_status("Подключение…", AMBER)
        threading.Thread(target=self._read_process, args=(process,), daemon=True).start()

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
        self._set_status("Перезапуск…" if code == 75 else "Остановлен", AMBER if code == 75 else RED)
        if code == 75:
            self._schedule_start(120)

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

    def stop_agent(self) -> None:
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
        self._set_status("Остановлен", RED)

    def restart_agent(self) -> None:
        self.stop_agent()
        self._schedule_start(150)

    def check_update(self) -> None:
        if not self.config.get("api_key"):
            messagebox.showwarning("XASS", "Сначала подключите компьютер")
            return

        def worker() -> None:
            try:
                server = discover_backend_url(str(self.config.get("server_url")))
                payload = build_payload(self.config)
                with httpx.Client(timeout=25, trust_env=bool(self.config.get("trust_env_proxy", False))) as client:
                    response = client.post(
                        f"{server}/agent/heartbeat",
                        headers={"X-Api-Key": self.config["api_key"]},
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                manifest = data.get("update") if isinstance(data, dict) else None
                if not isinstance(manifest, dict) or not manifest.get("available"):
                    self.root.after(0, lambda: messagebox.showinfo("XASS", "Установлена актуальная версия"))
                    return
                self.root.after(0, lambda: self._confirm_update(manifest))
            except Exception as exc:
                self.root.after(0, lambda error=str(exc): messagebox.showerror("XASS", f"Не удалось проверить обновление:\n{error}"))

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_update(self, manifest: dict[str, Any]) -> None:
        version = str(manifest.get("version") or "")
        if not messagebox.askyesno("XASS", f"Доступна версия {version}. Установить сейчас?"):
            return
        # Do not let the background agent download the same package in parallel
        # with a manual update from the desktop UI.
        self.stop_agent()
        self._set_status("Скачивание обновления…", AMBER)

        def worker() -> None:
            try:
                stage = download_update(
                    manifest,
                    api_key=str(self.config.get("api_key")),
                    trust_env=bool(self.config.get("trust_env_proxy", False)),
                    progress=lambda message: self.log_queue.put(message),
                )
                launch_update_helper(stage, manifest, command_id=None, restart_target="desktop", minimized=False)
                self.root.after(0, self.root.destroy)
            except Exception as exc:
                self.root.after(0, lambda error=str(exc): self._update_failed(error))

        threading.Thread(target=worker, daemon=True).start()

    def _update_failed(self, error: str) -> None:
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
        self.history.append(stamped)
        self.history = self.history[-1000:]
        if "[pc-client] ok" in line:
            self._set_status("В сети", GREEN)
            self.last_seen_var.set("Последний heartbeat только что")
            self.server_state_var.set("Доступен")
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

    def close(self) -> None:
        self._closing = True
        self.stop_agent()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="XASS desktop PC agent")
    parser.add_argument("--minimized", action="store_true")
    args = parser.parse_args()
    root = tk.Tk()
    XassDesktop(root, minimized=args.minimized)
    root.mainloop()


if __name__ == "__main__":
    main()
