from datetime import datetime, timedelta, timezone

from app.models import AppConfig, HeartbeatSource, MessageLog
from app.services.app_config import DEFAULT_AWAY_MESSAGE, format_time_range, get_away_bypass_user_ids, minute_to_hhmm


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    aware = _ensure_utc(value)
    return aware.strftime("%Y-%m-%d %H:%M UTC")


def main_panel_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🔗 Связь", "callback_data": "panel:status"},
                {"text": "🖥 Сервер", "callback_data": "panel:server"},
            ],
            [
                {"text": "💻 ПК", "callback_data": "panel:pc"},
                {"text": "🤖 Агенты", "callback_data": "panel:agents"},
            ],
            [
                {"text": "📝 Логи", "callback_data": "panel:logs"},
                {"text": "📤 Экспорт", "callback_data": "panel:export"},
            ],
            [
                {"text": "⚙️ Настройки", "callback_data": "panel:settings"},
                {"text": "🌐 Профиль сайта", "callback_data": "panel:profile"},
            ],
        ]
    }


def settings_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Режим сохранения", "callback_data": "settings:save_mode"},
                {"text": "Таймаут связи", "callback_data": "settings:timeout"},
            ],
            [
                {"text": "Тихие часы вкл/выкл", "callback_data": "settings:quiet"},
                {"text": "Тихие часы время", "callback_data": "settings:quiet_time"},
            ],
            [
                {"text": "Не в сети вкл/выкл", "callback_data": "settings:away_toggle"},
                {"text": "Не в сети на время", "callback_data": "settings:away_for"},
            ],
            [
                {"text": "Расписание не в сети", "callback_data": "settings:away_schedule"},
                {"text": "Кому можно писать", "callback_data": "settings:away_bypass"},
            ],
            [
                {"text": "Текст автоответа", "callback_data": "settings:away_help"},
                {"text": "Чат уведомлений", "callback_data": "settings:set_notify_chat"},
            ],
            [
                {"text": "URL сервера", "callback_data": "settings:set_url"},
                {"text": "URL Shortcut iPhone", "callback_data": "settings:set_iphone_shortcut_url"},
            ],
            [
                {"text": "Назад", "callback_data": "panel:home"},
            ],
        ]
    }


def panel_text() -> str:
    return (
        "Панель управления\n"
        "-----------------\n"
        "Выберите раздел:\n"
        "• Связь и heartbeat\n"
        "• Сервер и ПК\n"
        "• Управление агентами\n"
        "• Логи и экспорт\n"
        "• Настройки и профиль сайта"
    )


def format_status_text(sources: list[HeartbeatSource], timeout_minutes: int) -> str:
    lines = [
        "Статус связи",
        "------------",
        f"Таймаут: {timeout_minutes} мин.",
        "",
    ]
    if not sources:
        lines.append("Источников heartbeat пока нет.")
        return "\n".join(lines)

    now = datetime.now(timezone.utc)
    for index, source in enumerate(sources, start=1):
        last_seen_at = _ensure_utc(source.last_seen_at)
        age_sec = int((now - last_seen_at).total_seconds())
        status = "В СЕТИ" if source.is_online else "НЕ В СЕТИ"
        lines.append(f"{index}. {source.source_name} [{source.source_type}]")
        lines.append(f"   Состояние: {status}")
        lines.append(f"   Последний heartbeat: {last_seen_at.isoformat()} ({max(age_sec, 0)} сек. назад)")
    return "\n".join(lines)


def format_pc_text(sources: list[HeartbeatSource], timeout_minutes: int) -> str:
    pc_sources = [item for item in sources if item.source_type == "PC_AGENT"]
    if not pc_sources:
        return "Статус ПК\n---------\nПК-агенты пока не подключены."

    lines = [
        "Статус ПК",
        "---------",
        f"Таймаут: {timeout_minutes} мин.",
        "",
    ]
    for index, source in enumerate(pc_sources, start=1):
        payload = source.last_payload or {}
        metrics = payload.get("metrics") or {}
        cpu = metrics.get("cpu_percent", "n/a")
        ram = metrics.get("ram_used_percent", "n/a")
        now_playing = (payload.get("now_playing") or "").strip()
        activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
        activity_text = str(activity.get("text") or "").strip() if isinstance(activity, dict) else ""
        active_app = str(payload.get("active_app") or "").strip()
        if not now_playing:
            if activity_text:
                now_playing = activity_text
            elif active_app:
                now_playing = f"Открыто: {active_app}"
            else:
                now_playing = "нет данных"
        status = "В СЕТИ" if source.is_online else "НЕ В СЕТИ"
        lines.append(f"{index}. {source.source_name}")
        lines.append(f"   Состояние: {status}")
        lines.append(f"   CPU: {cpu}% | RAM: {ram}%")
        lines.append(f"   Активность: {now_playing}")
    return "\n".join(lines)


def format_server_text(metrics: dict, service_statuses: dict[str, str]) -> str:
    uptime = str(timedelta(seconds=int(metrics.get("uptime_seconds", 0))))
    lines = [
        "Сервер",
        "------",
        f"CPU: {metrics.get('cpu_percent', 0)}%",
        f"RAM: {metrics.get('ram_used_gb', 0)} / {metrics.get('ram_total_gb', 0)} GB",
        f"Disk: {metrics.get('disk_used_gb', 0)} / {metrics.get('disk_total_gb', 0)} GB",
        f"Сеть: RX {metrics.get('net_rx_mb', 0)} MB | TX {metrics.get('net_tx_mb', 0)} MB",
        f"Uptime: {uptime}",
    ]

    processes = metrics.get("top_processes") or []
    if processes:
        lines.append("")
        lines.append("Топ процессов:")
        for item in processes:
            lines.append(
                f"- PID {item.get('pid')} {item.get('name')} | cpu={item.get('cpu_percent')}% mem={item.get('memory_percent')}%"
            )

    if service_statuses:
        lines.append("")
        lines.append("Сервисы:")
        for name, status in service_statuses.items():
            lines.append(f"- {name}: {status}")
    return "\n".join(lines)


def format_logs_text(logs: list[MessageLog]) -> str:
    lines = ["Логи", "----"]
    if not logs:
        lines.append("Логи пока пустые.")
        return "\n".join(lines)

    for item in logs:
        edited_suffix = " (изменено)" if item.edited_at else ""
        deleted_suffix = " [удалено]" if item.deleted else ""
        text = (item.text_content or "<медиа/без текста>").replace("\n", " ")
        text = text[:120]
        lines.append(f"- chat={item.chat_id} msg={item.telegram_message_id}{edited_suffix}{deleted_suffix}: {text}")
    return "\n".join(lines)


def format_settings_text(config: AppConfig) -> str:
    away_message = (config.away_mode_message or DEFAULT_AWAY_MESSAGE).replace("\n", " ")
    away_message = away_message[:140] + ("..." if len(away_message) > 140 else "")

    quiet_range = format_time_range(config.quiet_hours_start_minute, config.quiet_hours_end_minute)
    away_schedule_range = format_time_range(config.away_schedule_start_minute, config.away_schedule_end_minute)
    away_until = _fmt_dt(config.away_until_at)
    bypass_count = len(get_away_bypass_user_ids(config))

    return (
        "Настройки\n"
        "---------\n"
        f"Режим сохранения: {config.save_mode}\n"
        f"Таймаут heartbeat: {config.heartbeat_timeout_minutes} мин.\n"
        f"Тихие часы: {'вкл' if config.quiet_hours_enabled else 'выкл'} ({quiet_range})\n"
        f"Режим «Не в сети»: {'вкл' if config.away_mode_enabled else 'выкл'}\n"
        f"Не в сети до: {away_until}\n"
        f"Расписание «Не в сети»: {'вкл' if config.away_schedule_enabled else 'выкл'} ({away_schedule_range})\n"
        f"Кому можно писать в режиме «не в сети»: {bypass_count}\n"
        f"Автоответ: {away_message}\n"
        f"Чат уведомлений: {config.notify_chat_id}\n"
        f"URL сервера: {config.service_base_url or '-'}\n"
        f"Shortcut iPhone: {config.iphone_shortcut_url or '-'}"
    )
