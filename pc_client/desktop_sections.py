"""Connection, maintenance and local actions for the native XASS desktop shell.

Builders only compose widgets. Pairing, updates and PC actions remain owned by
the application callbacks, including their preview and confirmation guards.
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk
from typing import Any

from client_update import UPDATE_RESULT, current_revision, current_version, update_in_progress
from desktop_widgets import icon_image

try:
    from tkinterdnd2 import DND_FILES
except ImportError:
    DND_FILES = None


BG, CARD, FIELD = "#202022", "#2b2b2f", "#232326"
TEXT, MUTED, ACCENT, LINE = "#f5f5f7", "#b1b1bb", "#829cff", "#3b3b42"
LILAC = "#c495f4"


class _Columns(tk.Frame):
    """Two readable panels on wide windows; one column at compact widths."""

    def __init__(self, parent: tk.Misc, *, breakpoint: int = 840, bg: str = BG) -> None:
        super().__init__(parent, bg=bg)
        self._breakpoint = breakpoint
        self._panels: list[tk.Widget] = []
        self._wide: bool | None = None
        self.bind("<Configure>", self._arrange)

    def add(self, panel: tk.Widget) -> None:
        self._panels.append(panel)
        self._wide = None
        self._arrange()

    def _arrange(self, _event: Any = None) -> None:
        wide = self.winfo_width() >= self._breakpoint
        if wide == self._wide:
            return
        self._wide = wide
        self.columnconfigure(0, weight=1, uniform="sections" if wide else "")
        self.columnconfigure(1, weight=1 if wide else 0, uniform="sections" if wide else "")
        for index, panel in enumerate(self._panels):
            panel.grid(row=index // 2 if wide else index, column=index % 2 if wide else 0,
                       sticky="nsew", padx=(0, 8) if wide and index % 2 == 0 else (8, 0) if wide else 0,
                       pady=(0, 16))


def _label(parent: tk.Misc, text: str = "", *, variable: tk.Variable | None = None,
           size: int = 10, color: str = MUTED, bold: bool = False, bg: str = CARD) -> tk.Label:
    options: dict[str, Any] = {"textvariable": variable} if variable is not None else {"text": text}
    return tk.Label(parent, **options, bg=bg, fg=color, font=("Segoe UI Semibold" if bold else "Segoe UI", size),
                    justify="left", anchor="w", wraplength=760)


def _icon(parent: tk.Misc, name: str, *, color: str = ACCENT, size: int = 28, bg: str = CARD) -> tk.Label:
    image = icon_image(parent, name, size=size, color=color)
    label = tk.Label(parent, image=image, bg=bg)
    label._xass_icon = image  # type: ignore[attr-defined]  # Tk needs a strong PhotoImage reference.
    return label


def _header(app: Any, title: str, subtitle: str) -> None:
    header = tk.Frame(app.content, bg=BG)
    header.pack(fill="x", pady=(0, 20))
    _label(header, title, size=22, color=TEXT, bold=True, bg=BG).pack(fill="x")
    _label(header, subtitle, size=10, bg=BG).pack(fill="x", pady=(6, 0))


def _card_heading(parent: tk.Misc, title: str, description: str, icon: str, *, color: str = ACCENT) -> None:
    header = tk.Frame(parent, bg=CARD)
    header.pack(fill="x", pady=(0, 15))
    _icon(header, icon, color=color).pack(side="left", anchor="n", padx=(0, 13), pady=(2, 0))
    copy = tk.Frame(header, bg=CARD)
    copy.pack(side="left", fill="x", expand=True)
    _label(copy, title, size=16, color=TEXT, bold=True).pack(fill="x")
    _label(copy, description).pack(fill="x", pady=(5, 0))


def _register_dropzone(zone: tk.Widget, callback: Any) -> bool:
    if DND_FILES is None:
        return False
    registered = False
    for child in [zone, *zone.winfo_children()]:
        if not hasattr(child, "drop_target_register"):
            continue
        try:
            child.drop_target_register(DND_FILES)
            child.dnd_bind("<<Drop>>", callback)
            registered = True
        except tk.TclError:
            continue  # File picker and clipboard import are still available.
    return registered


def build_connection(app: Any) -> None:
    """Import/drop a connection file, or pair with the original manual fields."""
    _header(app, "Подключение", "Свяжите этот компьютер с Telegram Mini App и веб-приложением XASS.")
    columns = _Columns(app.content)
    columns.pack(fill="x")

    quick = app._card(columns, padding=22)
    columns.add(quick)
    _card_heading(quick, "Подключить файлом", "Рекомендуемый способ · без ручного ввода адреса", "link")
    _label(quick, "В Mini App откройте «Инструменты» → «Агенты» → «Подключить ПК» и скачайте файл подключения.").pack(fill="x", pady=(0, 16))

    zone = tk.Frame(quick, bg=FIELD, padx=18, pady=20, highlightbackground="#525a76", highlightthickness=1)
    zone.pack(fill="x")
    _icon(zone, "folder", size=34, color=ACCENT, bg=FIELD).pack(anchor="w", pady=(0, 12))
    drop_title = _label(zone, "Перетащите файл сюда", size=14, color=TEXT, bold=True, bg=FIELD)
    drop_title.pack(fill="x")
    _label(zone, "xass-connect.xass или конфигурация .json", bg=FIELD).pack(fill="x", pady=(6, 15))
    app._button(zone, "Выбрать файл подключения", app.import_connection_file, kind="primary").pack(fill="x")
    if not _register_dropzone(zone, app._drop_connection_file):
        drop_title.configure(text="Выберите файл подключения")

    app._button(quick, "Вставить конфигурацию", app.paste_connection, kind="secondary").pack(fill="x", pady=(12, 0))
    status = tk.Frame(quick, bg=CARD)
    status.pack(fill="x", pady=(18, 0))
    _label(status, "СОСТОЯНИЕ ИМПОРТА", size=9, bold=True).pack(fill="x")
    _label(status, variable=app.import_status_var, color=TEXT).pack(fill="x", pady=(6, 0))

    manual = app._card(columns, padding=22)
    columns.add(manual)
    _card_heading(manual, "Ввести вручную", "Если адрес и одноразовый ключ уже под рукой", "terminal", color=LILAC)
    for label, variable, secret in (
        ("Адрес сервера или IP", app.server_var, False),
        ("Имя компьютера", app.name_var, False),
        ("Одноразовый ключ", app.pair_var, True),
    ):
        before = set(manual.winfo_children())
        app._field(manual, label, variable, secret=secret)
        for child in set(manual.winfo_children()) - before:
            if isinstance(child, tk.Label):
                child.configure(font=("Segoe UI Semibold", 10))
    _label(manual, "Ключ действует ограниченное время и выдаётся в разделе «Подключить ПК». Повторная настройка после обновления не нужна.").pack(fill="x", pady=(16, 18))
    actions = tk.Frame(manual, bg=CARD)
    actions.pack(fill="x")
    app.pair_button = app._button(actions, "Подключить", app.pair, kind="primary")
    app.pair_button.pack(fill="x")
    if app._pairing:
        app.pair_button.configure(state="disabled", text="Подключение…")

    _label(app.content, "Файл подключения и ключ дают доступ к вашему серверу. Не публикуйте их и не отправляйте посторонним.", bg=BG).pack(fill="x", pady=(0, 4))


def build_updates(app: Any) -> None:
    """Keep live updater variables/progress and original check/restart callbacks."""
    _header(app, "Обновления", "Новая версия приложения с сохранением привязки и настроек.")
    card = app._card(app.content, padding=22)
    card.pack(fill="x", pady=(0, 16))
    _card_heading(card, "XASS для Windows", "Канал stable · обновления с вашего сервера", "update")
    _label(card, variable=app.update_state_var, size=20, color=TEXT, bold=True).pack(fill="x", pady=(2, 6))
    _label(card, variable=app.update_detail_var).pack(fill="x")
    app.update_progress = ttk.Progressbar(card, mode="determinate", style="XASS.Horizontal.TProgressbar")
    app.update_progress.pack(fill="x", pady=(20, 18))
    app._progress_running = False

    facts = tk.Frame(card, bg=CARD)
    facts.pack(fill="x")
    app._connection_row(facts, "Версия на этом ПК", current_version())
    app._connection_row(facts, "Ревизия", current_revision()[:16] or "локальная")
    app._connection_row(facts, "Автообновления", "Включены" if app.auto_update_var.get() else "Выключены")
    try:
        loaded = json.loads(UPDATE_RESULT.read_text(encoding="utf-8"))
        result = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError, TypeError):
        result = {}
    app._connection_row(facts, "Последняя установка", "Успешно" if result.get("ok") else "Ошибка / откат" if result else "Ещё не выполнялась")
    actions = tk.Frame(card, bg=CARD)
    actions.pack(fill="x", pady=(20, 0))
    app.update_button = app._button(actions, "Проверить обновление", app.check_update, kind="primary")
    app.update_button.pack(side="left")
    app._button(actions, "Перезапустить агент", app.restart_agent, kind="secondary").pack(side="left")
    app._flow_actions(actions)
    if app._update_checking or update_in_progress():
        app.update_button.configure(state="disabled")

    safety = app._card(app.content, padding=22)
    safety.pack(fill="x")
    _card_heading(safety, "Проверка перед установкой", "Привязка, настройки и локальный архив сохраняются", "shield", color=LILAC)
    _label(safety, "Пакет проходит проверку подлинности и целостности. Затем XASS устанавливает новую версию и запускается снова. Если локальная проверка не пройдёт, updater попытается вернуть предыдущую версию.").pack(fill="x")


def build_commands(app: Any) -> None:
    """Organize all six existing local actions without replacing their guards."""
    _header(app, "Команды", "Действия на этом компьютере. Важные операции требуют подтверждения.")
    columns = _Columns(app.content, breakpoint=780)
    columns.pack(fill="x")

    connection = app._card(columns, padding=22)
    columns.add(connection)
    _card_heading(connection, "Связь и обслуживание", "Проверьте сервер или перезапустите фоновый агент", "link")
    _label(connection, "Перезапускается только агент XASS, не Windows. Проверка обновления не запускает установку без подтверждения.").pack(fill="x", pady=(0, 18))
    actions = tk.Frame(connection, bg=CARD)
    actions.pack(fill="x")
    for label, callback, kind in (
        ("Проверить связь", app.check_connection, "primary"),
        ("Перезапустить агент", app.restart_agent, "secondary"),
        ("Проверить обновление", app.check_update, "ghost"),
    ):
        app._button(actions, label, callback, kind=kind).pack(side="left")
    app._flow_actions(actions)

    capture = app._card(columns, padding=22)
    columns.add(capture)
    _card_heading(capture, "Экран и буфер", "Быстрые локальные инструменты", "monitor", color=LILAC)
    _label(capture, "Снимки сохраняются в «Изображения\\XASS». Буфер обмена открывается локально — проверяйте содержимое перед отправкой.").pack(fill="x", pady=(0, 18))
    actions = tk.Frame(capture, bg=CARD)
    actions.pack(fill="x")
    app._button(actions, "Снимок экрана", app.take_local_screenshot, kind="secondary").pack(side="left")
    app._button(actions, "Буфер обмена", app.show_clipboard, kind="secondary").pack(side="left")
    app._flow_actions(actions)

    security = app._card(app.content, padding=22)
    security.pack(fill="x")
    _card_heading(security, "Безопасность компьютера", "Блокировка Windows завершает доступ к рабочему столу", "shield")
    _label(security, "После блокировки войти снова можно только на этом ПК: через Windows Hello, PIN или пароль. XASS не обходит экран входа.").pack(fill="x", pady=(0, 16))
    app._button(security, "Заблокировать экран", app.lock_workstation, kind="danger").pack(anchor="w")
    _label(app.content, "Управление с телефона доступно в Telegram Mini App и веб-приложении, пока фоновый агент подключён к серверу.", bg=BG).pack(fill="x", pady=(16, 0))
