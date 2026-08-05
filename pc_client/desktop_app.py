from __future__ import annotations

import argparse
import json
import queue
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

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

ROOT = Path(__file__).resolve().parent
BG = "#050a12"
PANEL = "#0a1421"
PANEL_2 = "#0e1a28"
LINE = "#1b2a39"
TEXT = "#f3f6fa"
MUTED = "#8795a8"
ACCENT = "#348dfb"
GREEN = "#42d979"
AMBER = "#f0b35a"
RED = "#f2656f"


class XassDesktop:
    def __init__(self, root: Tk, *, minimized: bool = False) -> None:
        self.root = root
        self.root.title("XASS — ПК агент")
        self.root.geometry("980x650")
        self.root.minsize(820, 560)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.config = ensure_minimal_defaults(load_config())
        self.config["desktop_managed"] = True
        self.process: subprocess.Popen[str] | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.current_view = "overview"

        self.server_var = StringVar(value=str(self.config.get("server_url") or "http://127.0.0.1:8001"))
        self.name_var = StringVar(value=str(self.config.get("source_name") or socket.gethostname()))
        self.pair_var = StringVar()
        self.interval_var = StringVar(value=str(self.config.get("interval_sec") or 30))
        self.auto_update_var = BooleanVar(value=bool(self.config.get("auto_update", True)))
        self.connection_var = StringVar(value="Остановлен")
        self.last_seen_var = StringVar(value="Нет соединения")
        self.server_state_var = StringVar(value="Не проверен")

        self._configure_styles()
        self._build_shell()
        self.show_view("overview")
        self.root.after(120, self._drain_logs)
        self.root.after(600, self.start_agent)
        if minimized:
            self.root.after(900, self.root.iconify)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Root.TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Body.TFrame", background=BG)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 24))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("Section.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 14))
        style.configure("PanelTitle.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI Semibold", 9))
        style.configure("PanelValue.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI Semibold", 14))
        style.configure("PanelHint.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Nav.TButton", background=PANEL, foreground=MUTED, borderwidth=0, padding=(18, 13), anchor="w", font=("Segoe UI Semibold", 10))
        style.map("Nav.TButton", background=[("active", PANEL_2)], foreground=[("active", TEXT)])
        style.configure("Primary.TButton", background=ACCENT, foreground="white", borderwidth=0, padding=(18, 11), font=("Segoe UI Semibold", 10))
        style.map("Primary.TButton", background=[("active", "#277be1")])
        style.configure("Secondary.TButton", background=PANEL_2, foreground=TEXT, bordercolor=LINE, padding=(16, 10), font=("Segoe UI Semibold", 10))
        style.map("Secondary.TButton", background=[("active", "#142438")])
        style.configure("Danger.TButton", background="#321720", foreground="#ffb5bf", padding=(16, 10), font=("Segoe UI Semibold", 10))
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=10)
        style.configure("TCheckbutton", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", BG)])

    def _build_shell(self) -> None:
        shell = ttk.Frame(self.root, style="Root.TFrame")
        shell.pack(fill="both", expand=True)
        self.sidebar = ttk.Frame(shell, style="Panel.TFrame", width=210)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        body = ttk.Frame(shell, style="Body.TFrame")
        body.pack(side="left", fill="both", expand=True)

        brand = ttk.Frame(self.sidebar, style="Panel.TFrame")
        brand.pack(fill="x", padx=20, pady=(24, 30))
        ttk.Label(brand, text="XASS", background=PANEL, foreground=TEXT, font=("Segoe UI Black", 24)).pack(anchor="w")
        ttk.Label(brand, text="ПК агент", background=PANEL, foreground=MUTED, font=("Segoe UI", 9)).pack(anchor="w")

        for key, label in (("overview", "Обзор"), ("connection", "Подключение"), ("logs", "Журнал")):
            ttk.Button(self.sidebar, text=label, style="Nav.TButton", command=lambda item=key: self.show_view(item)).pack(fill="x", padx=10, pady=2)
        ttk.Separator(self.sidebar, orient="horizontal").pack(fill="x", padx=20, pady=18)
        self.side_status = ttk.Label(self.sidebar, textvariable=self.connection_var, background=PANEL, foreground=AMBER, font=("Segoe UI Semibold", 10))
        self.side_status.pack(anchor="w", padx=22)
        ttk.Label(self.sidebar, text=f"Версия {current_version()}", background=PANEL, foreground=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=22, pady=(4, 0))

        self.content = ttk.Frame(body, style="Body.TFrame")
        self.content.pack(fill="both", expand=True, padx=32, pady=26)

    def _clear_content(self) -> None:
        for widget in self.content.winfo_children():
            widget.destroy()

    def show_view(self, name: str) -> None:
        self.current_view = name
        self._clear_content()
        if name == "connection":
            self._build_connection()
        elif name == "logs":
            self._build_logs()
        else:
            self._build_overview()

    def _header(self, title: str, subtitle: str) -> None:
        ttk.Label(self.content, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.content, text=subtitle, style="Subtitle.TLabel").pack(anchor="w", pady=(3, 24))

    def _panel(self, parent: ttk.Frame, title: str, value_var: StringVar | None = None, value: str = "", hint: str = "") -> ttk.Frame:
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=18)
        ttk.Label(panel, text=title.upper(), style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(panel, textvariable=value_var, text=value, style="PanelValue.TLabel").pack(anchor="w", pady=(7, 2))
        if hint:
            ttk.Label(panel, text=hint, style="PanelHint.TLabel").pack(anchor="w")
        return panel

    def _build_overview(self) -> None:
        self._header("Состояние агента", "Связь с XASS, версия клиента и автоматические обновления")
        status = ttk.Frame(self.content, style="Panel.TFrame", padding=20)
        status.pack(fill="x")
        top = ttk.Frame(status, style="Panel.TFrame")
        top.pack(fill="x")
        self.status_dot = ttk.Label(top, text="●", background=PANEL, foreground=GREEN, font=("Segoe UI", 16))
        self.status_dot.pack(side="left")
        ttk.Label(top, textvariable=self.connection_var, background=PANEL, foreground=TEXT, font=("Segoe UI Semibold", 16)).pack(side="left", padx=(10, 0))
        ttk.Button(top, text="Перезапустить", style="Secondary.TButton", command=self.restart_agent).pack(side="right")
        ttk.Label(status, textvariable=self.last_seen_var, background=PANEL, foreground=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=(28, 0), pady=(4, 0))

        grid = ttk.Frame(self.content, style="Body.TFrame")
        grid.pack(fill="x", pady=16)
        for column in range(3):
            grid.columnconfigure(column, weight=1, uniform="summary")
        panels = [
            self._panel(grid, "Сервер", self.server_state_var, hint=self.server_var.get()),
            self._panel(grid, "Клиент", value=f"v{current_version()}", hint=(current_revision()[:12] or "локальная версия")),
            self._panel(grid, "Обновления", value="Автоматически" if self.auto_update_var.get() else "Вручную", hint="Подписанный пакет + откат"),
        ]
        for index, panel in enumerate(panels):
            panel.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 7, 0 if index == 2 else 7))

        actions = ttk.Frame(self.content, style="Body.TFrame")
        actions.pack(fill="x", pady=(2, 0))
        ttk.Button(actions, text="Проверить обновление", style="Primary.TButton", command=self.check_update).pack(side="left")
        ttk.Button(actions, text="Остановить агент", style="Danger.TButton", command=self.stop_agent).pack(side="left", padx=10)

        ttk.Label(self.content, text="Последние события", style="Section.TLabel").pack(anchor="w", pady=(28, 10))
        self.overview_log = ScrolledText(self.content, height=10, bg=PANEL, fg=MUTED, insertbackground=TEXT, relief="flat", borderwidth=0, font=("Cascadia Mono", 9), padx=14, pady=12)
        self.overview_log.pack(fill="both", expand=True)

    def _build_connection(self) -> None:
        self._header("Подключение", "Адрес сервера, имя устройства и одноразовый код из Telegram")
        form = ttk.Frame(self.content, style="Panel.TFrame", padding=22)
        form.pack(fill="x")
        fields = [
            ("Адрес сервера", self.server_var),
            ("Имя компьютера", self.name_var),
            ("Pair-code", self.pair_var),
            ("Интервал heartbeat, сек", self.interval_var),
        ]
        for row, (label, variable) in enumerate(fields):
            ttk.Label(form, text=label, background=PANEL, foreground=MUTED, font=("Segoe UI Semibold", 9)).grid(row=row * 2, column=0, sticky="w", pady=(0 if row == 0 else 14, 5))
            ttk.Entry(form, textvariable=variable).grid(row=row * 2 + 1, column=0, sticky="ew")
        form.columnconfigure(0, weight=1)
        ttk.Checkbutton(form, text="Устанавливать обновления автоматически", variable=self.auto_update_var).grid(row=8, column=0, sticky="w", pady=(18, 0))
        actions = ttk.Frame(self.content, style="Body.TFrame")
        actions.pack(fill="x", pady=16)
        ttk.Button(actions, text="Сохранить", style="Secondary.TButton", command=self.save_settings).pack(side="left")
        ttk.Button(actions, text="Подключить по коду", style="Primary.TButton", command=self.pair).pack(side="left", padx=10)

    def _build_logs(self) -> None:
        self._header("Журнал", "Heartbeat, команды сервера и установка обновлений")
        self.full_log = ScrolledText(self.content, bg=PANEL, fg=MUTED, insertbackground=TEXT, relief="flat", borderwidth=0, font=("Cascadia Mono", 9), padx=14, pady=12)
        self.full_log.pack(fill="both", expand=True)
        for line in getattr(self, "history", [])[-500:]:
            self.full_log.insert("end", line + "\n")
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

    def pair(self) -> None:
        code = self.pair_var.get().strip()
        if not code:
            messagebox.showwarning("XASS", "Введите pair-code из Mini App или команды /pairpc")
            return

        def worker() -> None:
            try:
                server = discover_backend_url(normalize_server_url(self.server_var.get()))
                result = claim_pair_code(server_url=server, pair_code=code, source_name=self.name_var.get(), source_type="PC_AGENT")
                self.config.update(
                    {
                        "server_url": server,
                        "source_name": str(result.get("source_name") or self.name_var.get()),
                        "source_type": "PC_AGENT",
                        "api_key": str(result.get("agent_api_key") or ""),
                        "interval_sec": max(5, int(self.interval_var.get() or 30)),
                        "auto_update": self.auto_update_var.get(),
                        "desktop_managed": True,
                    }
                )
                save_config(self.config)
                self.root.after(0, lambda: self._paired_ok(server))
            except Exception as exc:
                self.root.after(0, lambda error=str(exc): messagebox.showerror("XASS", f"Не удалось подключить агент:\n{error}"))

        threading.Thread(target=worker, daemon=True).start()

    def _paired_ok(self, server: str) -> None:
        self.server_var.set(server)
        self.name_var.set(str(self.config.get("source_name") or ""))
        self.pair_var.set("")
        self._log("Pairing выполнен, ключ сохранён локально")
        self.restart_agent()
        messagebox.showinfo("XASS", "Компьютер подключён к серверу")

    def start_agent(self) -> None:
        if self.process and self.process.poll() is None:
            return
        update_marker = ROOT / ".updates" / ".in-progress"
        if update_marker.exists():
            if self._update_is_running(update_marker):
                self.connection_var.set("Устанавливается обновление…")
                self.side_status.configure(foreground=AMBER)
                self.root.after(1000, self.start_agent)
                return
            update_marker.unlink(missing_ok=True)
        if not self.config.get("server_url") or not self.config.get("api_key"):
            self.connection_var.set("Требуется подключение")
            self.side_status.configure(foreground=AMBER)
            return
        args = [sys.executable, "-u", str(ROOT / "client_agent.py"), "--desktop-managed"]
        self.process = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.connection_var.set("Подключение…")
        self.side_status.configure(foreground=AMBER)
        threading.Thread(target=self._read_process, daemon=True).start()

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

    def _read_process(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self.log_queue.put(line.rstrip())
        code = process.wait()
        self.log_queue.put(f"[desktop] agent stopped with code {code}")
        self.root.after(0, lambda: self._agent_exited(code))

    def _agent_exited(self, code: int) -> None:
        self.process = None
        self.connection_var.set("Перезапуск…" if code == 75 else "Остановлен")
        self.side_status.configure(foreground=AMBER if code == 75 else RED)
        if code == 75:
            self.root.after(700, self.start_agent)

    def stop_agent(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.process = None
        self.connection_var.set("Остановлен")
        self.side_status.configure(foreground=RED)

    def restart_agent(self) -> None:
        self.stop_agent()
        self.root.after(900, self.start_agent)

    def check_update(self) -> None:
        if not self.config.get("api_key"):
            messagebox.showwarning("XASS", "Сначала подключите компьютер по pair-code")
            return

        def worker() -> None:
            try:
                server = discover_backend_url(str(self.config.get("server_url")))
                payload = build_payload(self.config)
                with httpx.Client(timeout=25, trust_env=bool(self.config.get("trust_env_proxy", False))) as client:
                    response = client.post(f"{server}/agent/heartbeat", headers={"X-Api-Key": self.config["api_key"]}, json=payload)
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

        def worker() -> None:
            try:
                stage = download_update(manifest, api_key=str(self.config.get("api_key")), trust_env=bool(self.config.get("trust_env_proxy", False)), progress=self._log)
                launch_update_helper(stage, manifest, command_id=None, restart_target="desktop", minimized=False)
                self.root.after(0, self.root.destroy)
            except Exception as exc:
                self.root.after(0, lambda error=str(exc): messagebox.showerror("XASS", f"Обновление не установлено:\n{error}"))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_logs(self) -> None:
        try:
            while True:
                self._log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(120, self._drain_logs)

    def _log(self, line: str) -> None:
        if not hasattr(self, "history"):
            self.history: list[str] = []
        stamped = f"{datetime.now().strftime('%H:%M:%S')}  {line}"
        self.history.append(stamped)
        self.history = self.history[-1000:]
        if "[pc-client] ok" in line:
            self.connection_var.set("В сети")
            self.last_seen_var.set("Последний heartbeat только что")
            self.server_state_var.set("Доступен")
            self.side_status.configure(foreground=GREEN)
        elif "heartbeat failed" in line:
            self.connection_var.set("Нет связи")
            self.last_seen_var.set(line[-120:])
            self.server_state_var.set("Ошибка")
            self.side_status.configure(foreground=RED)
        for attr in ("overview_log", "full_log"):
            widget = getattr(self, attr, None)
            if widget and widget.winfo_exists():
                widget.configure(state="normal")
                widget.insert("end", stamped + "\n")
                widget.see("end")
                if attr == "full_log":
                    widget.configure(state="disabled")

    def close(self) -> None:
        self.stop_agent()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="XASS desktop PC agent")
    parser.add_argument("--minimized", action="store_true")
    args = parser.parse_args()
    root = Tk()
    XassDesktop(root, minimized=args.minimized)
    root.mainloop()


if __name__ == "__main__":
    main()
