import logging
import secrets
import asyncio
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_api import TelegramApiError, TelegramBotClient
from app.config import Settings
from app.models import AppConfig, HeartbeatSource, MediaAsset, MessageLog, MessageRevision
from app.services.app_config import (
    DEFAULT_AWAY_MESSAGE,
    add_away_bypass_user_id,
    clear_away_until,
    cycle_save_mode,
    cycle_timeout,
    format_time_range,
    get_or_create_app_config,
    get_away_bypass_user_ids,
    is_away_mode_active,
    log_admin_action,
    parse_time_range,
    remove_away_bypass_user_id,
    set_away_message,
    set_away_bypass_user_ids,
    set_away_mode,
    set_away_for_minutes,
    set_away_schedule,
    set_quiet_hours_window,
    set_notify_chat,
    set_iphone_shortcut_url,
    set_service_base_url,
    toggle_away_mode,
    toggle_quiet_hours,
)
from app.services.agent_pairing import issue_pair_code
from app.services.auth import is_authorized, is_owner
from app.services.export import export_messages_csv
from app.services.heartbeat import delete_source_by_id, list_sources, rename_source
from app.services.message_logging import handle_update_logging
from app.services.monitoring import collect_server_metrics, collect_systemd_statuses
from app.services.panel import (
    format_pc_text,
    format_server_text,
    format_settings_text,
    format_status_text,
    main_panel_keyboard,
    panel_text,
    settings_keyboard,
)
from app.services.profile_editor import (
    ensure_profile_exists,
    load_profile,
    parse_link_input,
    parse_link_rename_input,
    parse_one_based_index,
    parse_stack_replace,
    parse_weather_location_input,
    profile_preview_text,
    rollback_last_profile_version,
    save_profile_with_backup,
    validate_http_url,
)
from app.services.profile_runtime import set_profile_now_playing_source, sync_profile_now_playing_from_heartbeat, sync_profile_weather
from app.services.projects_bot import ProjectsBotService
from app.services.updater import (
    CommitInfo,
    UpdateStatus,
    get_update_status,
    read_update_log_tail,
    rollback,
    run_update,
)

logger = logging.getLogger(__name__)

MESSAGE_KEYS = ("message", "business_message")
EDIT_NOTIFICATION_KEYS = ("edited_message", "edited_business_message")
DELETED_NOTIFICATION_KEY = "deleted_business_messages"
ALLOWED_AVATAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_AVATAR_FILE_SIZE = 15 * 1024 * 1024


class TelegramUpdateHandler:
    def __init__(self, settings: Settings, bot_client: TelegramBotClient | None):
        self.settings = settings
        self.bot_client = bot_client
        self.profile_dialogs: dict[int, dict[str, Any]] = {}
        self.profile_avatar_cursor: dict[int, int] = {}
        self.profile_avatar_upload_context: dict[int, dict[str, Any]] = {}
        self.away_bypass_contact_context: dict[int, dict[str, Any]] = {}
        self.projects_service = ProjectsBotService(
            settings=settings,
            bot_client=bot_client,
            safe_send=self._safe_send,
            safe_edit_or_send=self._safe_edit_or_send,
            path_to_url=self._avatar_path_to_url,
        )

    async def handle_update(self, session: AsyncSession, update: dict[str, Any]) -> None:
        config = await get_or_create_app_config(session, self.settings)
        await handle_update_logging(
            session,
            update,
            config=config,
            owner_user_id=self.settings.owner_user_id,
            bot_client=self.bot_client,
        )
        await self._notify_edit_events(session, config, update)
        await self._notify_deleted_events(session, config, update)

        callback = update.get("callback_query")
        if callback:
            await self._handle_callback(session, callback)
            return

        message = self._extract_message(update)
        if not message:
            return
        if await self._maybe_handle_away_mode(session, config, message):
            return
        if await self._maybe_handle_away_bypass_contact(session, config, message):
            return
        if await self._maybe_handle_profile_avatar_upload(message):
            return
        if await self._maybe_handle_profile_dialog_input(message):
            return
        if await self._maybe_handle_projects_upload(message):
            return
        if await self._maybe_handle_projects_dialog_input(message):
            return

        text = (message.get("text") or "").strip()
        if text.startswith("/"):
            await self._handle_command(session, message, text)

    def _extract_message(self, update: dict[str, Any]) -> dict[str, Any] | None:
        for key in MESSAGE_KEYS:
            item = update.get(key)
            if item:
                return item
        return None

    async def _handle_command(self, session: AsyncSession, message: dict[str, Any], text: str) -> None:
        from_user = message.get("from") or {}
        user_id = from_user.get("id")
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return
        if not is_authorized(user_id, self.settings):
            await self._safe_send(chat_id, "Доступ запрещен.")
            return

        config = await get_or_create_app_config(session, self.settings)
        command = text.split()[0].split("@")[0].lower()

        if command in ("/start", "/panel"):
            await self._safe_send(chat_id, panel_text(), reply_markup=main_panel_keyboard())
            return
        if command == "/server":
            m = collect_server_metrics(top_processes_limit=self.settings.top_processes_limit)
            s = collect_systemd_statuses(self.settings.monitored_services)
            await self._safe_send(chat_id, format_server_text(m, s))
            return
        if command == "/status":
            await self._safe_send(chat_id, format_status_text(await list_sources(session), config.heartbeat_timeout_minutes))
            return
        if command == "/pc":
            await self._safe_send(chat_id, format_pc_text(await list_sources(session), config.heartbeat_timeout_minutes))
            return
        if command == "/agents":
            await self._show_agents_panel(session, config, chat_id, None)
            return
        if command == "/pairpc":
            await self._send_pair_code(session, config, chat_id, user_id)
            return
        if command == "/agentzip":
            await self._send_agent_bundle(session, config, chat_id, user_id)
            return
        if command == "/logs":
            await self._safe_send(chat_id, await self._build_logs_text(session))
            return
        if command == "/export":
            await self._send_export(session, chat_id, user_id)
            return
        if command == "/media":
            await self._handle_media_command(session, chat_id, text)
            return
        if command == "/setnotify":
            await set_notify_chat(session, config, chat_id, user_id)
            await self._safe_send(chat_id, f"Чат уведомлений установлен: {chat_id}")
            return
        if command == "/seturl":
            await self._handle_set_url_command(session, config, chat_id, user_id, text)
            await self._safe_send(chat_id, format_settings_text(await get_or_create_app_config(session, self.settings)), reply_markup=settings_keyboard())
            return
        if command == "/setiphoneshortcut":
            await self._handle_set_iphone_shortcut_command(session, config, chat_id, user_id, text)
            await self._safe_send(chat_id, format_settings_text(await get_or_create_app_config(session, self.settings)), reply_markup=settings_keyboard())
            return
        if command == "/away":
            await self._handle_away_command(session, config, user_id, chat_id, text)
            await self._safe_send(chat_id, format_settings_text(await get_or_create_app_config(session, self.settings)), reply_markup=settings_keyboard())
            return
        if command == "/awayfor":
            await self._handle_away_for_command(session, config, user_id, chat_id, text)
            await self._safe_send(chat_id, format_settings_text(await get_or_create_app_config(session, self.settings)), reply_markup=settings_keyboard())
            return
        if command == "/awaytime":
            await self._handle_away_time_command(session, config, user_id, chat_id, text)
            await self._safe_send(chat_id, format_settings_text(await get_or_create_app_config(session, self.settings)), reply_markup=settings_keyboard())
            return
        if command == "/awayallow":
            await self._handle_away_allow_command(session, config, user_id, chat_id, text)
            await self._safe_send(chat_id, format_settings_text(await get_or_create_app_config(session, self.settings)), reply_markup=settings_keyboard())
            return
        if command == "/quiettime":
            await self._handle_quiet_time_command(session, config, user_id, chat_id, text)
            await self._safe_send(chat_id, format_settings_text(await get_or_create_app_config(session, self.settings)), reply_markup=settings_keyboard())
            return
        if command == "/awaytext":
            await self._handle_away_text_command(session, config, user_id, chat_id, text)
            await self._safe_send(chat_id, format_settings_text(await get_or_create_app_config(session, self.settings)), reply_markup=settings_keyboard())
            return
        if command == "/pcname":
            await self._handle_pcname_command(session, chat_id, user_id, text)
            return
        if command == "/update":
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Обновление доступно только владельцу.")
                return
            await self._show_update_panel(chat_id=chat_id, message_id=None)
            return
        if command == "/projects":
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Управление проектами доступно только владельцу.")
                return
            await self._show_projects_panel(chat_id=chat_id, message_id=None, page=0)
            return
        if command == "/projects_bg":
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Настройка фона доступна только владельцу.")
                return
            await self._show_projects_background_panel(chat_id=chat_id, message_id=None)
            return
        if command == "/profile_panel":
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Редактирование профиля доступно только владельцу.")
                return
            await self._show_profile_panel(chat_id)
            return
        if command == "/nowsource":
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
                return
            await self._handle_now_source_command(session, config, chat_id, text)
            return
        if command == "/iphonehook":
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
                return
            await self._handle_iphone_hook_command(config, chat_id)
            return
        if command in ("/connect_iphone", "/addiphone"):
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
                return
            await self._handle_connect_iphone_command(config, user_id, chat_id)
            return
        if command in ("/iphoneshortcut", "/shortcut_iphone"):
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
                return
            if user_id is None:
                return
            await self._send_iphone_shortcut_setup(config, user_id, chat_id)
            return
        if command in ("/connect_vk", "/addvk", "/vksetup"):
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
                return
            await self._handle_connect_vk_command(chat_id)
            return
        if command in ("/vkset", "/vk_token"):
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
                return
            await self._handle_vk_set_command(session, config, chat_id, user_id, text)
            return
        if command == "/vkclear":
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
                return
            await self._handle_vk_clear_command(chat_id, user_id)
            return
        if command == "/weatherloc":
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
                return
            await self._handle_weather_location_command(chat_id, user_id, text)
            return
        if command == "/weatherrefresh":
            if not is_owner(user_id, self.settings):
                await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
                return
            await self._handle_weather_refresh_command(chat_id)
            return

        await self._safe_send(chat_id, "Неизвестная команда. Используйте /panel.")

    async def _handle_away_command(self, session: AsyncSession, config: AppConfig, user_id: int, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await self._safe_send(
                chat_id,
                "Использование:\n"
                "/away on\n"
                "/away off\n"
                "/awayfor <минуты>\n"
                "/awaytime <ЧЧ:ММ-ЧЧ:ММ | off>\n"
                "/awayallow <list|clear|add ID|remove ID>",
            )
            return
        arg = parts[1].strip().lower()
        if arg in ("on", "1", "true", "вкл", "включить"):
            await set_away_mode(session, config, True, user_id)
            return
        if arg in ("off", "0", "false", "выкл", "выключить"):
            await set_away_mode(session, config, False, user_id)
            return
        await self._safe_send(chat_id, "Не понял аргумент. Используйте /away on или /away off.")

    async def _handle_away_for_command(self, session: AsyncSession, config: AppConfig, user_id: int, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await self._safe_send(chat_id, "Использование:\n/awayfor <минуты>\nПример: /awayfor 90")
            return
        value = parts[1].strip().lower()
        if value in ("off", "stop", "cancel", "0"):
            await clear_away_until(session, config, user_id)
            await self._safe_send(chat_id, "Таймер режима «не в сети» отключен.")
            return
        if not value.isdigit():
            await self._safe_send(chat_id, "Минуты должны быть числом. Пример: /awayfor 120")
            return
        minutes = int(value)
        updated = await set_away_for_minutes(session, config, minutes=minutes, actor_user_id=user_id)
        until_text = updated.away_until_at.isoformat() if updated.away_until_at else "-"
        await self._safe_send(chat_id, f"Режим «не в сети» включен на {minutes} мин.\nДо: {until_text}")

    async def _handle_away_time_command(self, session: AsyncSession, config: AppConfig, user_id: int, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await self._safe_send(chat_id, "Использование:\n/awaytime <ЧЧ:ММ-ЧЧ:ММ>\n/awaytime off\nПример: /awaytime 22:30-07:45")
            return
        value = parts[1].strip()
        if value.lower() in ("off", "disable", "0"):
            updated = await set_away_schedule(
                session,
                config,
                enabled=False,
                start_minute=config.away_schedule_start_minute,
                end_minute=config.away_schedule_end_minute,
                actor_user_id=user_id,
            )
            await self._safe_send(
                chat_id,
                f"Расписание режима «не в сети» отключено. Было: {format_time_range(updated.away_schedule_start_minute, updated.away_schedule_end_minute)}",
            )
            return
        try:
            start_minute, end_minute = parse_time_range(value)
        except ValueError as exc:
            await self._safe_send(chat_id, f"Ошибка: {exc}")
            return
        updated = await set_away_schedule(
            session,
            config,
            enabled=True,
            start_minute=start_minute,
            end_minute=end_minute,
            actor_user_id=user_id,
        )
        await self._safe_send(
            chat_id,
            f"Расписание режима «не в сети» обновлено: {format_time_range(updated.away_schedule_start_minute, updated.away_schedule_end_minute)}",
        )

    async def _handle_quiet_time_command(self, session: AsyncSession, config: AppConfig, user_id: int, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await self._safe_send(chat_id, "Использование:\n/quiettime <ЧЧ:ММ-ЧЧ:ММ>\nПример: /quiettime 23:00-08:00")
            return
        value = parts[1].strip()
        try:
            start_minute, end_minute = parse_time_range(value)
        except ValueError as exc:
            await self._safe_send(chat_id, f"Ошибка: {exc}")
            return
        updated = await set_quiet_hours_window(
            session,
            config,
            start_minute=start_minute,
            end_minute=end_minute,
            actor_user_id=user_id,
        )
        await self._safe_send(
            chat_id,
            f"Тихие часы обновлены: {format_time_range(updated.quiet_hours_start_minute, updated.quiet_hours_end_minute)}",
        )

    async def _handle_away_allow_command(self, session: AsyncSession, config: AppConfig, user_id: int, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) == 1:
            await self._safe_send(chat_id, self._away_allow_list_text(config))
            return

        action = parts[1].strip().lower()
        if action in ("list", "show"):
            await self._safe_send(chat_id, self._away_allow_list_text(config))
            return
        if action in ("clear", "reset"):
            await set_away_bypass_user_ids(session, config, set(), user_id)
            await self._safe_send(chat_id, "Список обхода очищен.")
            return

        if len(parts) < 3:
            await self._safe_send(chat_id, "Использование:\n/awayallow add <user_id>\n/awayallow remove <user_id>")
            return
        raw_id = parts[2].strip()
        if not raw_id.lstrip("-").isdigit():
            await self._safe_send(chat_id, "user_id должен быть числом.")
            return
        target_user_id = int(raw_id)
        if action in ("add", "+"):
            await add_away_bypass_user_id(session, config, target_user_id, user_id)
            await self._safe_send(chat_id, f"Добавлен в список обхода: {target_user_id}")
            return
        if action in ("remove", "del", "delete", "-"):
            await remove_away_bypass_user_id(session, config, target_user_id, user_id)
            await self._safe_send(chat_id, f"Удален из списка обхода: {target_user_id}")
            return
        await self._safe_send(chat_id, "Неизвестная команда. Используйте /awayallow list|add|remove|clear")

    async def _handle_away_text_command(self, session: AsyncSession, config: AppConfig, user_id: int, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) == 1 or not parts[1].strip():
            await self._safe_send(chat_id, "Использование:\n/awaytext <ваш текст>")
            return
        await set_away_message(session, config, parts[1], user_id)

    async def _handle_pcname_command(self, session: AsyncSession, chat_id: int, user_id: int, text: str) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await self._safe_send(chat_id, "Использование:\n/pcname <текущее_имя> <новое_имя>")
            return
        old_name = parts[1].strip()
        new_name = parts[2].strip()
        if not new_name:
            await self._safe_send(chat_id, "Новое имя не должно быть пустым.")
            return
        source = await rename_source(session, old_name, new_name)
        if source is None:
            await self._safe_send(chat_id, f"Источник '{old_name}' не найден.")
            return
        await log_admin_action(session, user_id, "rename_pc_source", {"from": old_name, "to": new_name})
        await self._safe_send(chat_id, f"Имя ПК обновлено: {old_name} -> {new_name}")

    async def _handle_weather_location_command(self, chat_id: int, user_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await self._safe_send(
                chat_id,
                "Использование:\n"
                "/weatherloc <Название | Широта | Долгота | Timezone>\n\n"
                "Пример:\n"
                "/weatherloc Москва | 55.7558 | 37.6176 | Europe/Moscow",
            )
            return

        try:
            location_name, latitude, longitude, timezone_name = parse_weather_location_input(parts[1])
        except ValueError as exc:
            await self._safe_send(chat_id, f"Ошибка: {exc}")
            return

        profile_path, backups_dir, audit_log_path = self._profile_paths()
        profile = load_profile(profile_path)
        profile["weather_location_name"] = location_name
        profile["weather_latitude"] = latitude
        profile["weather_longitude"] = longitude
        profile["weather_timezone"] = timezone_name
        profile["weather_auto_enabled"] = True

        new_profile, backup_path, changed_fields = save_profile_with_backup(
            profile_path=profile_path,
            backup_dir=backups_dir,
            audit_log_path=audit_log_path,
            actor_user_id=user_id,
            action="profile_set_weather_location_command",
            profile_data=profile,
            payload={
                "location_name": location_name,
                "latitude": latitude,
                "longitude": longitude,
                "timezone": timezone_name,
            },
        )
        await sync_profile_weather(self.settings, force=True)
        refreshed_profile = load_profile(profile_path)

        lines = [
            "Локация погоды обновлена.",
            f"Локация: {location_name}",
            f"Координаты: {latitude}, {longitude}",
            f"Часовой пояс: {timezone_name}",
            f"Авто-погода: {'вкл' if refreshed_profile.get('weather_auto_enabled', True) else 'выкл'}",
            f"Текущая погода: {refreshed_profile.get('weather_text') or 'нет данных'}",
        ]
        if changed_fields:
            lines.append(f"Измененные поля: {', '.join(changed_fields)}")
        if backup_path:
            lines.append(f"Бэкап: {backup_path.name}")
        await self._safe_send(chat_id, "\n".join(lines))

    async def _handle_weather_refresh_command(self, chat_id: int) -> None:
        updated = await sync_profile_weather(self.settings, force=True)
        profile = load_profile(Path(self.settings.profile_json_path))
        weather_text = profile.get("weather_text") or "нет данных"
        if updated:
            await self._safe_send(chat_id, f"Погода обновлена:\n{weather_text}")
            return
        await self._safe_send(chat_id, f"Не удалось обновить погоду. Текущее значение:\n{weather_text}")

    def _normalize_service_base_url(self, raw: str) -> str | None:
        text = (raw or "").strip()
        if not text:
            return None
        if "://" not in text:
            lower = text.lower()
            if lower.startswith("localhost") or lower.startswith("127.") or lower.startswith("10.") or lower.startswith("192.168.") or lower.startswith("172.16."):
                text = f"http://{text}"
            else:
                text = f"https://{text}"
        try:
            parsed = urlsplit(text)
        except Exception:
            return None
        if not parsed.scheme or not parsed.netloc:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    def _normalize_icloud_shortcut_url(self, raw: str) -> str | None:
        text = (raw or "").strip()
        if not text:
            return None
        try:
            parsed = urlsplit(text)
        except Exception:
            return None
        if parsed.scheme != "https":
            return None
        if parsed.netloc.lower() != "www.icloud.com":
            return None
        if not parsed.path.lower().startswith("/shortcuts/"):
            return None
        return urlunsplit(("https", "www.icloud.com", parsed.path, "", ""))

    def _guess_public_base_url(self, config: AppConfig | None = None) -> str | None:
        raw = ""
        if config and config.service_base_url:
            raw = str(config.service_base_url)
        elif self.settings.profile_public_url:
            raw = self.settings.profile_public_url
        return self._normalize_service_base_url(raw)

    def _iphone_shortcut_import_url(self, config: AppConfig | None = None) -> str | None:
        if not config:
            return None
        return self._normalize_icloud_shortcut_url(str(config.iphone_shortcut_url or ""))

    async def _handle_set_url_command(
        self,
        session: AsyncSession,
        config: AppConfig,
        chat_id: int,
        user_id: int | None,
        text: str,
    ) -> None:
        if not is_owner(user_id, self.settings):
            await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
            return
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            current = self._guess_public_base_url(config) or "-"
            await self._safe_send(
                chat_id,
                (
                    "Использование:\n"
                    "/seturl https://example.com\n"
                    "/seturl https://example.com:8443\n"
                    "/seturl http://127.0.0.1:8001\n"
                    "/seturl off\n\n"
                    f"Текущий URL: {current}"
                ),
            )
            return

        value = parts[1].strip()
        if value.lower() in {"off", "clear", "none", "-"}:
            await set_service_base_url(session, config, None, int(user_id))
            await self._safe_send(chat_id, "URL сервера очищен.")
            return

        normalized = self._normalize_service_base_url(value)
        if not normalized:
            await self._safe_send(chat_id, "Некорректный URL. Пример: https://example.com")
            return

        await set_service_base_url(session, config, normalized, int(user_id))
        await self._safe_send(chat_id, f"URL сервера сохранен: {normalized}")

    async def _handle_set_iphone_shortcut_command(
        self,
        session: AsyncSession,
        config: AppConfig,
        chat_id: int,
        user_id: int | None,
        text: str,
    ) -> None:
        if not is_owner(user_id, self.settings):
            await self._safe_send(chat_id, "Нет доступа. Команда доступна только владельцу.")
            return

        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            current = self._iphone_shortcut_import_url(config) or "-"
            await self._safe_send(
                chat_id,
                (
                    "Использование:\n"
                    "/setiphoneshortcut https://www.icloud.com/shortcuts/XXXXXXXX\n"
                    "/setiphoneshortcut off\n\n"
                    f"Текущий URL: {current}"
                ),
            )
            return

        value = parts[1].strip()
        if value.lower() in {"off", "clear", "none", "-"}:
            await set_iphone_shortcut_url(session, config, None, int(user_id))
            await self._safe_send(chat_id, "URL iPhone Shortcut очищен.")
            return

        normalized = self._normalize_icloud_shortcut_url(value)
        if not normalized:
            await self._safe_send(chat_id, "Некорректная ссылка. Нужен формат: https://www.icloud.com/shortcuts/...")
            return

        await set_iphone_shortcut_url(session, config, normalized, int(user_id))
        await self._safe_send(chat_id, f"URL iPhone Shortcut сохранен: {normalized}")

    def _profile_iphone_hook_key(self) -> str:
        profile = load_profile(Path(self.settings.profile_json_path))
        return str(profile.get("iphone_hook_key") or "").strip()

    def _effective_iphone_hook_key(self) -> str:
        return self._profile_iphone_hook_key() or (self.settings.iphone_now_playing_api_key or "").strip()

    def _profile_vk_credentials(self) -> tuple[int | None, str]:
        profile = load_profile(Path(self.settings.profile_json_path))
        profile_uid_raw = profile.get("vk_user_id")
        profile_uid: int | None = None
        if isinstance(profile_uid_raw, int):
            profile_uid = profile_uid_raw if profile_uid_raw > 0 else None
        elif isinstance(profile_uid_raw, str) and profile_uid_raw.strip().isdigit():
            parsed = int(profile_uid_raw.strip())
            profile_uid = parsed if parsed > 0 else None
        profile_token = str(profile.get("vk_access_token") or "").strip()
        if profile_uid and profile_token:
            return profile_uid, profile_token
        return self.settings.vk_user_id, (self.settings.vk_access_token or "").strip()

    def _build_vk_oauth_url(self) -> str | None:
        if not self.settings.vk_app_id:
            return None
        app_id = int(self.settings.vk_app_id)
        version = (self.settings.vk_api_version or "5.199").strip() or "5.199"
        return (
            "https://oauth.vk.com/authorize"
            f"?client_id={app_id}"
            "&display=mobile"
            "&redirect_uri=https://oauth.vk.com/blank.html"
            "&scope=status,offline"
            "&response_type=token"
            f"&v={version}"
        )

    async def _ensure_profile_iphone_hook_key(self, user_id: int) -> str:
        existing = self._profile_iphone_hook_key()
        if existing:
            return existing

        profile_path, backups_dir, audit_log_path = self._profile_paths()
        profile = load_profile(profile_path)
        new_key = f"iph_{secrets.token_urlsafe(24)}"
        profile["iphone_hook_key"] = new_key
        save_profile_with_backup(
            profile_path=profile_path,
            backup_dir=backups_dir,
            audit_log_path=audit_log_path,
            actor_user_id=user_id,
            action="profile_set_iphone_hook_key",
            profile_data=profile,
            payload={"source": "connect_iphone_command"},
        )
        return new_key

    async def _handle_iphone_hook_command(self, config: AppConfig, chat_id: int) -> None:
        key = self._effective_iphone_hook_key()
        base_url = self._guess_public_base_url(config)
        endpoint = f"{base_url}/profile/now-playing/external" if base_url else "(сначала задайте URL через /seturl)"

        lines = [
            "iPhone hook для now playing",
            "---------------------------",
            f"Endpoint: {endpoint}",
            f"X-Api-Key: {key if key else '(не задан)'}",
            "",
            "JSON body пример:",
            '{"text":"Artist - Title","source":"iphone"}',
            "",
            "Команда переключения источника:",
            "/nowsource iphone",
        ]
        if not key:
            lines.append("Сначала выполните /connect_iphone — бот создаст ключ автоматически.")
        await self._safe_send(chat_id, "\n".join(lines))

    async def _handle_connect_iphone_command(self, config: AppConfig, user_id: int, chat_id: int) -> None:
        key = await self._ensure_profile_iphone_hook_key(user_id)
        set_profile_now_playing_source(self.settings, "iphone")
        base_url = self._guess_public_base_url(config)
        endpoint = f"{base_url}/profile/now-playing/external" if base_url else ""
        cmd = ""
        if endpoint:
            cmd = (
                f'curl -X POST "{endpoint}" '
                f'-H "X-Api-Key: {key}" '
                '-H "Content-Type: application/json" '
                '-d "{\\"text\\":\\"Artist - Title\\",\\"source\\":\\"iphone\\"}"'
            )
        lines = [
            "Подключение iPhone готово.",
            "",
            "1) Источник уже переключен на iPhone.",
            "",
            "2) Endpoint:",
            endpoint or "(не задан)",
            "",
            "3) Ключ:",
            key,
            "",
            "4) Быстрая проверка командой:",
            cmd or "(сначала задайте URL через /seturl)",
            "",
            "5) Для iOS Shortcuts: нажмите кнопку «🧭 Галерея Shortcuts» ниже или команду /iphoneshortcut.",
        ]
        await self._safe_send(
            chat_id,
            "\n".join(lines),
            reply_markup=self._iphone_shortcut_setup_keyboard("iphone", self._iphone_shortcut_import_url(config)),
        )

    async def _send_iphone_shortcut_setup(self, config: AppConfig, user_id: int, chat_id: int) -> None:
        key = await self._ensure_profile_iphone_hook_key(user_id)
        set_profile_now_playing_source(self.settings, "iphone")

        base_url = self._guess_public_base_url(config)
        if not base_url:
            await self._safe_send(
                chat_id,
                "Сначала задайте URL сервера: /seturl https://your-domain.tld\n"
                "После этого повторите команду /iphoneshortcut.",
            )
            return

        endpoint = f"{base_url}/profile/now-playing/external"
        shortcut_name = "Serverredus Now Playing"
        run_shortcut_url = f"shortcuts://run-shortcut?name={quote(shortcut_name)}"
        import_url = self._iphone_shortcut_import_url(config)

        lines = [
            "Готовая заготовка для iPhone Shortcuts",
            "--------------------------------------",
            f"Название команды: {shortcut_name}",
            f"Endpoint: {endpoint}",
            f"X-Api-Key: {key}",
            "",
            "Как собрать команду (1-2 минуты):",
            "1) Откройте приложение Shortcuts (или кнопку «🧭 Галерея Shortcuts»).",
            "2) Создайте новую команду и добавьте действие «Получить текущую песню».",
            "3) Добавьте «Получить содержимое URL»:",
            f"   URL: {endpoint}",
            "   Метод: POST",
            "   Заголовки:",
            f"   - X-Api-Key: {key}",
            "   - Content-Type: application/json",
            "   JSON body (проще):",
            '   {"source":"iphone","artist":"[Artist]","title":"[Title]"}',
            "   или один полем text:",
            '   {"source":"iphone","text":"[Artist] - [Title]"}',
            "4) В Automation выберите запуск по «Музыка открыта» или «Началось воспроизведение».",
            "",
            "После создания можно запускать вручную по ссылке:",
            run_shortcut_url,
        ]
        if import_url:
            lines.extend(["", "Готовый импорт вашей команды:", import_url])
        else:
            lines.extend(["", "Если у вас есть iCloud-ссылка на готовую команду, задайте её:", "/setiphoneshortcut https://www.icloud.com/shortcuts/..."])
        await self._safe_send(
            chat_id,
            "\n".join(lines),
            reply_markup=self._iphone_shortcut_setup_keyboard("iphone", import_url),
        )

    async def _handle_connect_vk_command(self, chat_id: int) -> None:
        oauth_url = self._build_vk_oauth_url()
        current_uid, current_token = self._profile_vk_credentials()
        lines = [
            "Подключение VK для now listening",
            "--------------------------------",
            "Безопасный вариант: только OAuth token.",
            "Логин/пароль/коды подтверждения через бота не используются.",
            "",
            f"Текущее состояние: user_id={current_uid or '-'}, token={'задан' if current_token else 'не задан'}",
            "",
            "Команда сохранения токена:",
            "/vkset <vk_user_id> <vk_access_token>",
            "Команда очистки:",
            "/vkclear",
            "Источник:",
            "/nowsource vk",
        ]
        if oauth_url:
            lines.extend(["", "OAuth URL (получить access_token):", oauth_url])
        else:
            lines.extend(
                [
                    "",
                    "Для автополучения ссылки OAuth задайте VK_APP_ID в .env.",
                ]
            )
        await self._safe_send(chat_id, "\n".join(lines), reply_markup=self._now_source_switch_keyboard("vk"))

    async def _handle_vk_set_command(
        self,
        session: AsyncSession,
        config: AppConfig,
        chat_id: int,
        user_id: int,
        text: str,
    ) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await self._safe_send(chat_id, "Использование:\n/vkset <vk_user_id> <vk_access_token>")
            return
        vk_user_id_text = parts[1].strip()
        vk_token = parts[2].strip()
        if not vk_user_id_text.isdigit():
            await self._safe_send(chat_id, "vk_user_id должен быть числом.")
            return
        if len(vk_token) < 20:
            await self._safe_send(chat_id, "Слишком короткий token. Проверьте значение.")
            return

        vk_user_id = int(vk_user_id_text)
        profile_path, backups_dir, audit_log_path = self._profile_paths()
        profile = load_profile(profile_path)
        profile["vk_user_id"] = vk_user_id
        profile["vk_access_token"] = vk_token
        profile["vk_connected_at"] = datetime.now(timezone.utc).isoformat()

        _, backup_path, changed = save_profile_with_backup(
            profile_path=profile_path,
            backup_dir=backups_dir,
            audit_log_path=audit_log_path,
            actor_user_id=user_id,
            action="profile_set_vk_credentials",
            profile_data=profile,
            payload={"vk_user_id": vk_user_id, "token_len": len(vk_token)},
        )

        set_profile_now_playing_source(self.settings, "vk")
        await sync_profile_now_playing_from_heartbeat(session, self.settings, config.heartbeat_timeout_minutes)
        updated_profile = load_profile(profile_path)
        lines = [
            "VK подключен.",
            f"vk_user_id: {vk_user_id}",
            f"Сейчас в профиле: {updated_profile.get('now_listening_text') or 'нет данных'}",
            "Источник now listening переключен на VK.",
        ]
        if changed:
            lines.append(f"Измененные поля: {', '.join(changed)}")
        if backup_path:
            lines.append(f"Бэкап: {backup_path.name}")
        lines.extend(["", "Далее включите источник: /nowsource vk"])
        await self._safe_send(chat_id, "\n".join(lines))

    async def _handle_vk_clear_command(self, chat_id: int, user_id: int) -> None:
        profile_path, backups_dir, audit_log_path = self._profile_paths()
        profile = load_profile(profile_path)
        profile["vk_user_id"] = ""
        profile["vk_access_token"] = ""
        profile["vk_connected_at"] = ""
        _, backup_path, changed = save_profile_with_backup(
            profile_path=profile_path,
            backup_dir=backups_dir,
            audit_log_path=audit_log_path,
            actor_user_id=user_id,
            action="profile_clear_vk_credentials",
            profile_data=profile,
            payload={},
        )
        lines = ["VK-данные очищены."]
        if changed:
            lines.append(f"Измененные поля: {', '.join(changed)}")
        if backup_path:
            lines.append(f"Бэкап: {backup_path.name}")
        await self._safe_send(chat_id, "\n".join(lines))

    async def _handle_now_source_command(
        self,
        session: AsyncSession,
        config: AppConfig,
        chat_id: int,
        text: str,
    ) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            current_source = self._current_now_source()
            await self._safe_send(
                chat_id,
                self._build_now_source_text(current_source),
                reply_markup=self._now_source_switch_keyboard(current_source),
            )
            return

        target = self._normalize_now_source(parts[1].strip())
        if not target:
            await self._safe_send(chat_id, "Неверный источник. Используйте: pc, iphone или vk.")
            return

        await self._set_now_source(
            session=session,
            config=config,
            chat_id=chat_id,
            target=target,
            message_id=None,
            from_agents_panel=False,
        )

    def _normalize_now_source(self, raw: str) -> str | None:
        source_aliases = {
            "pc": "pc_agent",
            "pc_agent": "pc_agent",
            "iphone": "iphone",
            "ios": "iphone",
            "vk": "vk",
            "vkontakte": "vk",
        }
        return source_aliases.get((raw or "").strip().lower())

    def _current_now_source(self) -> str:
        profile = load_profile(Path(self.settings.profile_json_path))
        return str(profile.get("now_listening_source") or self.settings.now_playing_source_default or "pc_agent").strip().lower()

    def _display_now_source(self, value: str) -> str:
        mapping = {"pc_agent": "PC", "iphone": "iPhone", "vk": "VK"}
        return mapping.get(value, value or "pc_agent")

    def _now_source_switch_keyboard(self, current_source: str) -> dict[str, Any]:
        def _button(label: str, target: str) -> dict[str, str]:
            prefix = "✅ " if current_source == target else ""
            return {"text": f"{prefix}{label}", "callback_data": f"agents:nowsource:set:{target}"}

        return {
            "inline_keyboard": [
                [
                    _button("ПК", "pc_agent"),
                    _button("iPhone", "iphone"),
                    _button("VK", "vk"),
                ],
            ]
        }

    def _iphone_shortcut_setup_keyboard(self, current_source: str, import_url: str | None = None) -> dict[str, Any]:
        source_rows = self._now_source_switch_keyboard(current_source).get("inline_keyboard", [])
        import_rows: list[list[dict[str, str]]] = []
        if import_url:
            import_rows.append([{"text": "📥 Импортировать Shortcut", "url": import_url}])
        return {
            "inline_keyboard": [
                *import_rows,
                [{"text": "🧭 Галерея Shortcuts", "url": "https://www.icloud.com/shortcuts/"}],
                *source_rows,
            ]
        }

    def _build_now_source_text(self, current_source: str) -> str:
        return (
            "Источник now listening\n"
            f"Текущий: {self._display_now_source(current_source)}\n\n"
            "Выберите источник кнопкой ниже или командой:\n"
            "/nowsource pc\n"
            "/nowsource iphone\n"
            "/nowsource vk\n\n"
            "Подсказка для iPhone webhook: /iphonehook"
        )

    async def _set_now_source(
        self,
        *,
        session: AsyncSession,
        config: AppConfig,
        chat_id: int,
        target: str,
        message_id: int | None,
        from_agents_panel: bool,
    ) -> None:
        changed, normalized = set_profile_now_playing_source(self.settings, target)
        await sync_profile_now_playing_from_heartbeat(session, self.settings, config.heartbeat_timeout_minutes)

        lines = [
            "Источник now listening обновлён.",
            f"Новый источник: {normalized}",
            "Текущее значение пересинхронизировано.",
        ]
        if normalized == "iphone" and not self._effective_iphone_hook_key():
            lines.append("Внимание: ключ iPhone hook не задан. Выполните /connect_iphone.")
        if normalized == "vk":
            vk_uid, vk_token = self._profile_vk_credentials()
            if not vk_uid or not vk_token:
                lines.append("Внимание: VK не подключен. Выполните /connect_vk или /vkset <id> <token>.")
        if not changed:
            lines.append("Источник уже был выбран ранее.")

        text = "\n".join(lines)
        if from_agents_panel:
            sources = await list_sources(session)
            await self._safe_edit_or_send(
                chat_id,
                message_id,
                f"{text}\n\n{self._agents_panel_text(sources, config.heartbeat_timeout_minutes)}",
                self._agents_panel_keyboard(sources),
            )
            return

        await self._safe_send(chat_id, text, reply_markup=self._now_source_switch_keyboard(normalized))

    def _agents_panel_keyboard(self, sources: list[HeartbeatSource]) -> dict[str, Any]:
        current_source = self._current_now_source()
        rows: list[list[dict[str, str]]] = [
            [
                {"text": "🔑 Код подключения", "callback_data": "agents:pair:create"},
                {"text": "➕ Как добавить", "callback_data": "agents:add_help"},
            ],
            [
                {"text": "📦 Скачать ZIP для ПК", "callback_data": "agents:bundle:send"},
            ],
            [
                {"text": "🍎 Подключить iPhone", "callback_data": "agents:connect:iphone"},
                {"text": "🟦 Подключить VK", "callback_data": "agents:connect:vk"},
            ],
            [
                {"text": "🧩 Установить Shortcut", "callback_data": "agents:iphone:shortcut"},
            ],
            [
                {
                    "text": f"{'✅ ' if current_source == 'pc_agent' else ''}ПК",
                    "callback_data": "agents:nowsource:set:pc_agent",
                },
                {
                    "text": f"{'✅ ' if current_source == 'iphone' else ''}iPhone",
                    "callback_data": "agents:nowsource:set:iphone",
                },
                {
                    "text": f"{'✅ ' if current_source == 'vk' else ''}VK",
                    "callback_data": "agents:nowsource:set:vk",
                },
            ],
            [
                {"text": "🔄 Обновить", "callback_data": "panel:agents"},
            ],
        ]
        for source in sources[:8]:
            title = source.source_name
            if len(title) > 22:
                title = f"{title[:19]}..."
            rows.append([{"text": f"🗑 Удалить: {title}", "callback_data": f"agents:delete:{source.id}"}])
        rows.append([{"text": "⬅️ Назад", "callback_data": "panel:home"}])
        return {"inline_keyboard": rows}

    def _agents_panel_text(self, sources: list[HeartbeatSource], timeout_minutes: int) -> str:
        current_source = self._current_now_source()
        if not sources:
            return (
                "Управление агентами\n"
                "-------------------\n"
                "Агенты пока не подключены.\n\n"
                "Как добавить:\n"
                "1) Нажмите «🔑 Код подключения».\n"
                "2) На ПК запустите run_agent.bat и введите URL сервера + код.\n"
                "3) После первого heartbeat агент появится в списке автоматически.\n\n"
                f"Источник музыки сейчас: {self._display_now_source(current_source)}."
            )

        now = datetime.now(timezone.utc)
        online_count = sum(1 for item in sources if item.is_online)
        lines = [
            "Управление агентами",
            "-------------------",
            f"Всего: {len(sources)} | В сети: {online_count} | Не в сети: {len(sources) - online_count}",
            f"Таймаут offline: {timeout_minutes} мин.",
            f"Источник музыки: {self._display_now_source(current_source)}",
            "",
            "Список:",
        ]
        for idx, source in enumerate(sources, start=1):
            last_seen = source.last_seen_at
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            age_sec = max(int((now - last_seen.astimezone(timezone.utc)).total_seconds()), 0)
            status = "🟢 в сети" if source.is_online else "🔴 не в сети"
            lines.append(f"{idx}. {source.source_name} [{source.source_type}] — {status}, {age_sec} сек. назад")
        lines.extend(
            [
                "",
                "Удаление: кнопки ниже.",
                "Добавление: кнопки «🔑 Код подключения» и «➕ Как добавить».",
                "Архив для ПК: кнопка «📦 Скачать ZIP для ПК».",
                "Подключение iPhone/VK: кнопки «🍎 Подключить iPhone», «🧩 Установить Shortcut» и «🟦 Подключить VK».",
                "Переключение музыки: кнопки «ПК / iPhone / VK» или команда /nowsource <pc|iphone|vk>.",
            ]
        )
        return "\n".join(lines)

    async def _show_agents_panel(
        self,
        session: AsyncSession,
        config: AppConfig,
        chat_id: int | None,
        message_id: int | None,
    ) -> None:
        if chat_id is None:
            return
        sources = await list_sources(session)
        text = self._agents_panel_text(sources, config.heartbeat_timeout_minutes)
        await self._safe_edit_or_send(chat_id, message_id, text, self._agents_panel_keyboard(sources))

    async def _send_pair_code(self, session: AsyncSession, config: AppConfig, chat_id: int, user_id: int | None) -> None:
        if not is_owner(user_id, self.settings):
            await self._safe_send(chat_id, "Нет доступа. Выдача кода доступна только владельцу.")
            return

        result = await issue_pair_code(
            session,
            actor_user_id=user_id,
            ttl_minutes=self.settings.agent_pair_code_ttl_minutes,
            code_length=self.settings.agent_pair_code_length,
        )
        await log_admin_action(
            session,
            int(user_id),
            "issue_pair_code",
            {"ttl_minutes": result.ttl_minutes, "expires_at": result.expires_at.isoformat()},
        )
        expires_at_utc = result.expires_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        server_url = self._agent_server_url_hint(config)
        text = (
            "Код подключения создан.\n"
            f"Код: {result.code}\n"
            f"Действует: {result.ttl_minutes} мин. (до {expires_at_utc})\n\n"
            "На ПК:\n"
            "1) Запустите run_agent.bat\n"
            "2) Введите URL сервера и этот код\n\n"
            "Либо запуск с параметрами:\n"
            f"run_agent.bat --server-url {server_url} --pair-code {result.code}"
        )
        await self._safe_send(chat_id, text)

    def _agent_server_url_hint(self, config: AppConfig | None = None) -> str:
        base = self._guess_public_base_url(config)
        if base:
            return base
        return "http://127.0.0.1:8001"

    def _build_pc_agent_archive(self, *, pair_code: str, server_url_hint: str) -> Path:
        source_dir = (Path(__file__).resolve().parent.parent / "pc_client").resolve()
        if not source_dir.exists() or not source_dir.is_dir():
            raise FileNotFoundError("pc_client directory not found")

        export_dir = Path(self.settings.export_root)
        export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        archive_path = export_dir / f"pc_agent_bundle_{stamp}.zip"

        with ZipFile(archive_path, mode="w", compression=ZIP_DEFLATED) as archive:
            for item in source_dir.rglob("*"):
                if not item.is_file():
                    continue
                if "__pycache__" in item.parts:
                    continue
                if item.suffix.lower() in {".pyc", ".pyo"}:
                    continue
                arcname = (Path("pc_client") / item.relative_to(source_dir)).as_posix()
                archive.write(item, arcname=arcname)

            quickstart = (
                "Serverredus PC Agent Quick Start\n"
                "===============================\n\n"
                "1) Распакуйте архив в любую папку.\n"
                "2) Запустите run_agent.bat.\n"
                "3) Если нужен запуск одной командой:\n\n"
                f"run_agent.bat --server-url {server_url_hint} --pair-code {pair_code}\n\n"
                "4) (Опционально) Для автозапуска запустите install_autostart.bat.\n"
            )
            archive.writestr("pc_client/START_HERE.txt", quickstart)

        return archive_path

    async def _send_agent_bundle(self, session: AsyncSession, config: AppConfig, chat_id: int, user_id: int | None) -> None:
        if not is_owner(user_id, self.settings):
            await self._safe_send(chat_id, "Нет доступа. Выдача архива доступна только владельцу.")
            return
        if not self.bot_client:
            await self._safe_send(chat_id, "BOT_TOKEN не настроен.")
            return

        result = await issue_pair_code(
            session,
            actor_user_id=user_id,
            ttl_minutes=self.settings.agent_pair_code_ttl_minutes,
            code_length=self.settings.agent_pair_code_length,
        )
        server_url_hint = self._agent_server_url_hint(config)
        try:
            archive_path = self._build_pc_agent_archive(pair_code=result.code, server_url_hint=server_url_hint)
        except Exception as exc:
            logger.exception("Не удалось собрать ZIP агента")
            await self._safe_send(chat_id, f"Не удалось собрать архив агента: {exc}")
            return

        await log_admin_action(
            session,
            int(user_id),
            "send_pc_agent_bundle",
            {
                "archive_path": str(archive_path),
                "pair_code_ttl_minutes": result.ttl_minutes,
                "pair_code_expires_at": result.expires_at.isoformat(),
            },
        )

        expires_at_utc = result.expires_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        caption = (
            "Архив ПК-агента готов.\n"
            f"Код привязки: {result.code}\n"
            f"Действует до: {expires_at_utc}\n\n"
            "После распаковки:\n"
            f"run_agent.bat --server-url {server_url_hint} --pair-code {result.code}"
        )
        await self.bot_client.send_document(chat_id, archive_path, caption=caption)

    def _away_allow_list_text(self, config: AppConfig) -> str:
        user_ids = sorted(get_away_bypass_user_ids(config))
        if not user_ids:
            return (
                "Список обхода режима «не в сети» пуст.\n"
                "Добавить:\n"
                "• через кнопку «Кому можно писать» в /panel -> Настройки\n"
                "• или командой /awayallow add <user_id>"
            )
        lines = [
            "Список обхода режима «не в сети»:",
            *(f"{idx}. {uid}" for idx, uid in enumerate(user_ids, start=1)),
            "",
            f"Всего: {len(user_ids)}",
        ]
        return "\n".join(lines)

    def _away_bypass_inline_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "➕ Добавить контактом", "callback_data": "settings:away_bypass:add"},
                    {"text": "➖ Удалить контактом", "callback_data": "settings:away_bypass:remove"},
                ],
                [
                    {"text": "📄 Показать список", "callback_data": "settings:away_bypass:list"},
                    {"text": "🧹 Очистить", "callback_data": "settings:away_bypass:clear"},
                ],
                [
                    {"text": "⬅️ К настройкам", "callback_data": "panel:settings"},
                ],
            ]
        }

    def _contact_request_keyboard(self) -> dict[str, Any]:
        return {
            "keyboard": [
                [{"text": "📱 Отправить контакт", "request_contact": True}],
                [{"text": "Отмена"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }

    def _remove_reply_keyboard(self) -> dict[str, Any]:
        return {"remove_keyboard": True}

    def _set_away_contact_context(self, user_id: int, chat_id: int, mode: str) -> None:
        self.away_bypass_contact_context[user_id] = {
            "chat_id": chat_id,
            "mode": mode,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        }

    def _clear_away_contact_context(self, user_id: int) -> None:
        self.away_bypass_contact_context.pop(user_id, None)

    async def _maybe_handle_away_bypass_contact(self, session: AsyncSession, config: AppConfig, message: dict[str, Any]) -> bool:
        from_user = message.get("from") or {}
        user_id = from_user.get("id")
        if user_id is None:
            return False
        context = self.away_bypass_contact_context.get(user_id)
        if not isinstance(context, dict):
            return False

        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or context.get("chat_id") != chat_id:
            return False

        expires_at = context.get("expires_at")
        if isinstance(expires_at, datetime):
            expires = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires.astimezone(timezone.utc):
                self._clear_away_contact_context(user_id)
                await self._safe_send(chat_id, "Время ожидания контакта истекло.", reply_markup=self._remove_reply_keyboard())
                return True

        text = (message.get("text") or "").strip().lower()
        if text in ("отмена", "/cancel", "cancel"):
            self._clear_away_contact_context(user_id)
            await self._safe_send(chat_id, "Операция отменена.", reply_markup=self._remove_reply_keyboard())
            return True

        contact = message.get("contact") or {}
        if not isinstance(contact, dict) or not contact:
            await self._safe_send(chat_id, "Отправьте контакт кнопкой ниже или нажмите «Отмена».")
            return True

        target_user_id = contact.get("user_id")
        if target_user_id is None:
            await self._safe_send(chat_id, "У контакта нет Telegram user_id. Нужен контакт Telegram-пользователя.")
            return True

        mode = str(context.get("mode") or "add")
        if mode == "add":
            await add_away_bypass_user_id(session, config, int(target_user_id), user_id)
            result_text = f"Добавлен в обход режима «не в сети»: {target_user_id}"
        else:
            await remove_away_bypass_user_id(session, config, int(target_user_id), user_id)
            result_text = f"Удален из обхода режима «не в сети»: {target_user_id}"

        self._clear_away_contact_context(user_id)
        updated_config = await get_or_create_app_config(session, self.settings)
        await self._safe_send(
            chat_id,
            f"{result_text}\n\n{self._away_allow_list_text(updated_config)}",
            reply_markup=self._remove_reply_keyboard(),
        )
        return True

    async def _handle_callback(self, session: AsyncSession, callback: dict[str, Any]) -> None:
        data = (callback.get("data") or "").strip()
        callback_id = callback.get("id")
        user_id = (callback.get("from") or {}).get("id")
        msg = callback.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")

        if callback_id and self.bot_client:
            await self._answer_callback_safe(callback_id)
        if not is_authorized(user_id, self.settings):
            if chat_id is not None:
                await self._safe_send(chat_id, "Доступ запрещен.")
            return

        config = await get_or_create_app_config(session, self.settings)

        if data in ("panel", "panel:home"):
            await self._safe_edit_or_send(chat_id, message_id, panel_text(), main_panel_keyboard())
            return
        if data == "panel:status":
            await self._safe_edit_or_send(chat_id, message_id, format_status_text(await list_sources(session), config.heartbeat_timeout_minutes), main_panel_keyboard())
            return
        if data == "panel:server":
            m = collect_server_metrics(top_processes_limit=self.settings.top_processes_limit)
            s = collect_systemd_statuses(self.settings.monitored_services)
            await self._safe_edit_or_send(chat_id, message_id, format_server_text(m, s), main_panel_keyboard())
            return
        if data == "panel:pc":
            await self._safe_edit_or_send(chat_id, message_id, format_pc_text(await list_sources(session), config.heartbeat_timeout_minutes), main_panel_keyboard())
            return
        if data == "panel:agents":
            await self._show_agents_panel(session, config, chat_id, message_id)
            return
        if data == "panel:logs":
            await self._safe_edit_or_send(chat_id, message_id, await self._build_logs_text(session), main_panel_keyboard())
            return
        if data == "panel:update":
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Обновление доступно только владельцу.", main_panel_keyboard())
                return
            await self._show_update_panel(chat_id=chat_id, message_id=message_id)
            return
        if data == "agents:pair:create":
            if chat_id is None:
                return
            await self._send_pair_code(session, config, chat_id, user_id)
            await self._show_agents_panel(session, config, chat_id, message_id)
            return
        if data == "agents:bundle:send":
            if chat_id is None:
                return
            await self._send_agent_bundle(session, config, chat_id, user_id)
            await self._show_agents_panel(session, config, chat_id, message_id)
            return
        if data == "agents:connect:iphone":
            if chat_id is None:
                return
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Подключение iPhone доступно только владельцу.", self._agents_panel_keyboard(await list_sources(session)))
                return
            if user_id is None:
                return
            await self._handle_connect_iphone_command(config, user_id, chat_id)
            await self._show_agents_panel(session, config, chat_id, message_id)
            return
        if data == "agents:iphone:shortcut":
            if chat_id is None:
                return
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Установка Shortcut доступна только владельцу.", self._agents_panel_keyboard(await list_sources(session)))
                return
            if user_id is None:
                return
            await self._send_iphone_shortcut_setup(config, user_id, chat_id)
            await self._show_agents_panel(session, config, chat_id, message_id)
            return
        if data == "agents:connect:vk":
            if chat_id is None:
                return
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Подключение VK доступно только владельцу.", self._agents_panel_keyboard(await list_sources(session)))
                return
            await self._handle_connect_vk_command(chat_id)
            await self._show_agents_panel(session, config, chat_id, message_id)
            return
        if data.startswith("agents:nowsource:set:"):
            if chat_id is None:
                return
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(
                    chat_id,
                    message_id,
                    "Нет доступа. Переключение источника доступно только владельцу.",
                    self._agents_panel_keyboard(await list_sources(session)),
                )
                return
            raw_target = data.split(":", maxsplit=3)[3]
            target = self._normalize_now_source(raw_target)
            if not target:
                await self._safe_edit_or_send(chat_id, message_id, "Некорректный источник.", self._agents_panel_keyboard(await list_sources(session)))
                return
            await self._set_now_source(
                session=session,
                config=config,
                chat_id=chat_id,
                target=target,
                message_id=message_id,
                from_agents_panel=True,
            )
            return
        if data == "agents:add_help":
            server_url_hint = self._agent_server_url_hint(config)
            text = (
                "Добавление агента\n"
                "-----------------\n"
                "Новый агент добавляется автоматически после первого heartbeat.\n\n"
                "Рекомендуемый способ (без общего AGENT_API_KEY):\n"
                "1) Нажмите «🔑 Код подключения» в разделе агентов.\n"
                "2) На ПК запустите run_agent.bat.\n"
                "3) Введите URL сервера и код привязки.\n\n"
                "Быстро:\n"
                "1) На машине с агентом откройте проект.\n"
                "2) Запустите install.sh и выберите режим agent.\n"
                "3) Укажите URL сервера и AGENT_API_KEY.\n\n"
                "Или вручную:\n"
                f"python agent/agent.py --server-url {server_url_hint} --api-key <AGENT_API_KEY> "
                "--source-name my-pc --source-type PC_AGENT --interval-sec 30\n\n"
                "Проще: нажмите «📦 Скачать ZIP для ПК» и запустите run_agent.bat из архива."
            )
            await self._safe_edit_or_send(chat_id, message_id, text, self._agents_panel_keyboard(await list_sources(session)))
            return
        if data.startswith("agents:delete:"):
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Удаление агентов доступно только владельцу.", main_panel_keyboard())
                return
            source_id_text = data.split(":", maxsplit=2)[2]
            try:
                source_id = int(source_id_text)
            except ValueError:
                await self._safe_edit_or_send(chat_id, message_id, "Некорректный ID агента.", self._agents_panel_keyboard(await list_sources(session)))
                return
            deleted = await delete_source_by_id(session, source_id)
            if deleted is None:
                await self._safe_edit_or_send(chat_id, message_id, "Агент не найден или уже удален.", self._agents_panel_keyboard(await list_sources(session)))
                return
            await log_admin_action(
                session,
                user_id,
                "delete_agent_source",
                {"source_id": source_id, "source_name": deleted.source_name, "source_type": deleted.source_type},
            )
            updated_sources = await list_sources(session)
            text = (
                f"Агент удален: {deleted.source_name}\n\n"
                f"{self._agents_panel_text(updated_sources, config.heartbeat_timeout_minutes)}"
            )
            await self._safe_edit_or_send(chat_id, message_id, text, self._agents_panel_keyboard(updated_sources))
            return
        if data == "panel:profile":
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Раздел профиля доступен только владельцу.", main_panel_keyboard())
                return
            await self._show_profile_panel(chat_id, message_id)
            return
        if data == "panel:projects":
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Раздел проектов доступен только владельцу.", main_panel_keyboard())
                return
            await self._show_projects_panel(chat_id=chat_id, message_id=message_id, page=0)
            return
        if data == "panel:settings":
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data.startswith("profile:"):
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(
                    chat_id,
                    message_id,
                    "Нет доступа. Редактирование профиля доступно только владельцу.",
                    main_panel_keyboard(),
                )
                return
            await self._handle_profile_callback(chat_id, message_id, user_id, data)
            return
        if data.startswith("projects:"):
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(
                    chat_id,
                    message_id,
                    "Нет доступа. Управление проектами доступно только владельцу.",
                    main_panel_keyboard(),
                )
                return
            await self._handle_projects_callback(chat_id=chat_id, message_id=message_id, user_id=user_id, data=data)
            return
        if data == "panel:export":
            if chat_id is not None:
                await self._send_export(session, chat_id, user_id)
            return
        if data == "update:refresh":
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Обновление доступно только владельцу.", main_panel_keyboard())
                return
            await self._show_update_panel(chat_id=chat_id, message_id=message_id)
            return
        if data == "update:changes":
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Обновление доступно только владельцу.", main_panel_keyboard())
                return
            if chat_id is None:
                return
            await self._send_update_changes(chat_id=chat_id)
            return
        if data == "update:run":
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Обновление доступно только владельцу.", main_panel_keyboard())
                return
            await self._run_update_flow(chat_id=chat_id, message_id=message_id)
            return
        if data == "update:rollback:ask":
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Откат доступен только владельцу.", main_panel_keyboard())
                return
            await self._safe_edit_or_send(chat_id, message_id, "Откатить проект к предыдущему известному commit?", self._update_rollback_confirm_keyboard())
            return
        if data == "update:rollback:run":
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Откат доступен только владельцу.", main_panel_keyboard())
                return
            await self._run_rollback_flow(chat_id=chat_id, message_id=message_id)
            return
        if data == "update:rollback:cancel":
            if not is_owner(user_id, self.settings):
                await self._safe_edit_or_send(chat_id, message_id, "Нет доступа. Откат доступен только владельцу.", main_panel_keyboard())
                return
            await self._show_update_panel(chat_id=chat_id, message_id=message_id)
            return
        if data == "settings:save_mode":
            config = await cycle_save_mode(session, config, user_id)
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data == "settings:timeout":
            config = await cycle_timeout(session, config, user_id)
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data == "settings:quiet":
            config = await toggle_quiet_hours(session, config, user_id)
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data == "settings:quiet_time":
            text = (
                "Настройка тихих часов\n"
                "Формат: /quiettime ЧЧ:ММ-ЧЧ:ММ\n"
                "Пример: /quiettime 23:00-08:00\n\n"
                f"Текущее значение: {format_time_range(config.quiet_hours_start_minute, config.quiet_hours_end_minute)}"
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🌙 23:00-08:00", "callback_data": "settings:quiet_time:set:23:00-08:00"},
                        {"text": "🌙 00:00-07:00", "callback_data": "settings:quiet_time:set:00:00-07:00"},
                    ],
                    [{"text": "⬅️ К настройкам", "callback_data": "panel:settings"}],
                ]
            }
            await self._safe_edit_or_send(chat_id, message_id, text, keyboard)
            return
        if data.startswith("settings:quiet_time:set:"):
            value = data.split(":", maxsplit=3)[3]
            try:
                start_minute, end_minute = parse_time_range(value)
            except ValueError as exc:
                await self._safe_edit_or_send(chat_id, message_id, f"Ошибка: {exc}", settings_keyboard())
                return
            config = await set_quiet_hours_window(
                session,
                config,
                start_minute=start_minute,
                end_minute=end_minute,
                actor_user_id=user_id,
            )
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data == "settings:away_toggle":
            config = await toggle_away_mode(session, config, user_id)
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data == "settings:away_for":
            text = (
                "Режим «не в сети» на время\n"
                "Выберите длительность ниже или команда:\n"
                "/awayfor <минуты>\n\n"
                f"Текущий таймер до: {config.away_until_at.isoformat() if config.away_until_at else '-'}"
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "30 мин", "callback_data": "settings:away_for:set:30"},
                        {"text": "60 мин", "callback_data": "settings:away_for:set:60"},
                    ],
                    [
                        {"text": "120 мин", "callback_data": "settings:away_for:set:120"},
                        {"text": "8 часов", "callback_data": "settings:away_for:set:480"},
                    ],
                    [
                        {"text": "Выключить таймер", "callback_data": "settings:away_for:off"},
                    ],
                    [{"text": "⬅️ К настройкам", "callback_data": "panel:settings"}],
                ]
            }
            await self._safe_edit_or_send(chat_id, message_id, text, keyboard)
            return
        if data.startswith("settings:away_for:set:"):
            minutes_text = data.split(":", maxsplit=3)[3]
            if not minutes_text.isdigit():
                await self._safe_edit_or_send(chat_id, message_id, "Ошибка: минуты должны быть числом.", settings_keyboard())
                return
            config = await set_away_for_minutes(session, config, minutes=int(minutes_text), actor_user_id=user_id)
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data == "settings:away_for:off":
            config = await clear_away_until(session, config, user_id)
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data == "settings:away_schedule":
            text = (
                "Расписание режима «не в сети»\n"
                "Формат: /awaytime ЧЧ:ММ-ЧЧ:ММ\n"
                "Отключить: /awaytime off\n\n"
                f"Текущее: {'вкл' if config.away_schedule_enabled else 'выкл'} "
                f"({format_time_range(config.away_schedule_start_minute, config.away_schedule_end_minute)})"
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🌘 22:00-08:00", "callback_data": "settings:away_schedule:set:22:00-08:00"},
                        {"text": "🌘 00:00-07:00", "callback_data": "settings:away_schedule:set:00:00-07:00"},
                    ],
                    [{"text": "Отключить расписание", "callback_data": "settings:away_schedule:off"}],
                    [{"text": "⬅️ К настройкам", "callback_data": "panel:settings"}],
                ]
            }
            await self._safe_edit_or_send(chat_id, message_id, text, keyboard)
            return
        if data.startswith("settings:away_schedule:set:"):
            value = data.split(":", maxsplit=3)[3]
            try:
                start_minute, end_minute = parse_time_range(value)
            except ValueError as exc:
                await self._safe_edit_or_send(chat_id, message_id, f"Ошибка: {exc}", settings_keyboard())
                return
            config = await set_away_schedule(
                session,
                config,
                enabled=True,
                start_minute=start_minute,
                end_minute=end_minute,
                actor_user_id=user_id,
            )
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data == "settings:away_schedule:off":
            config = await set_away_schedule(
                session,
                config,
                enabled=False,
                start_minute=config.away_schedule_start_minute,
                end_minute=config.away_schedule_end_minute,
                actor_user_id=user_id,
            )
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data == "settings:away_bypass":
            text = self._away_allow_list_text(config)
            await self._safe_edit_or_send(chat_id, message_id, text, self._away_bypass_inline_keyboard())
            return
        if data == "settings:away_bypass:list":
            config = await get_or_create_app_config(session, self.settings)
            text = self._away_allow_list_text(config)
            await self._safe_edit_or_send(chat_id, message_id, text, self._away_bypass_inline_keyboard())
            return
        if data == "settings:away_bypass:clear":
            await set_away_bypass_user_ids(session, config, set(), user_id)
            config = await get_or_create_app_config(session, self.settings)
            await self._safe_edit_or_send(chat_id, message_id, self._away_allow_list_text(config), self._away_bypass_inline_keyboard())
            return
        if data == "settings:away_bypass:add":
            if chat_id is None:
                return
            self._set_away_contact_context(user_id, chat_id, "add")
            await self._safe_send(
                chat_id,
                "Отправьте контакт пользователя, которому можно писать в режиме «не в сети».",
                reply_markup=self._contact_request_keyboard(),
            )
            return
        if data == "settings:away_bypass:remove":
            if chat_id is None:
                return
            self._set_away_contact_context(user_id, chat_id, "remove")
            await self._safe_send(
                chat_id,
                "Отправьте контакт пользователя, которого нужно убрать из списка обхода.",
                reply_markup=self._contact_request_keyboard(),
            )
            return
        if data == "settings:away_help":
            text = (
                "Настройка автоответа:\n"
                "/awaytext <ваш текст>\n\n"
                "Полезные команды:\n"
                "/away on | /away off\n"
                "/awayfor 90\n"
                "/awaytime 22:00-08:00\n"
                "/awayallow list"
            )
            await self._safe_edit_or_send(chat_id, message_id, text, settings_keyboard())
            return
        if data == "settings:set_notify_chat" and chat_id is not None:
            config = await set_notify_chat(session, config, chat_id, user_id)
            await self._safe_edit_or_send(chat_id, message_id, format_settings_text(config), settings_keyboard())
            return
        if data == "settings:set_url":
            current_url = self._guess_public_base_url(config) or "-"
            text = (
                "URL сервера для авто-команд\n"
                "---------------------------\n"
                "Бот будет подставлять этот URL в команды для ПК/iPhone/VK.\n\n"
                "Установить:\n"
                "/seturl https://example.com\n"
                "или\n"
                "/seturl http://1.2.3.4:8001\n\n"
                "Очистить:\n"
                "/seturl off\n\n"
                f"Текущий URL: {current_url}"
            )
            await self._safe_edit_or_send(chat_id, message_id, text, settings_keyboard())
            return
        if data == "settings:set_iphone_shortcut_url":
            current_url = self._iphone_shortcut_import_url(config) or "-"
            text = (
                "URL готового iPhone Shortcut\n"
                "---------------------------\n"
                "Бот будет добавлять кнопку «📥 Импортировать Shortcut».\n\n"
                "Установить:\n"
                "/setiphoneshortcut https://www.icloud.com/shortcuts/XXXXXXXX\n\n"
                "Очистить:\n"
                "/setiphoneshortcut off\n\n"
                f"Текущий URL: {current_url}"
            )
            await self._safe_edit_or_send(chat_id, message_id, text, settings_keyboard())
            return

    async def _show_projects_panel(self, *, chat_id: int | None, message_id: int | None, page: int = 0) -> None:
        await self.projects_service.show_panel(chat_id=chat_id, message_id=message_id, page=page)

    async def _show_projects_background_panel(self, *, chat_id: int | None, message_id: int | None) -> None:
        await self.projects_service.show_bg(chat_id=chat_id, message_id=message_id)

    async def _handle_projects_callback(
        self,
        *,
        chat_id: int | None,
        message_id: int | None,
        user_id: int,
        data: str,
    ) -> None:
        await self.projects_service.handle_callback(chat_id=chat_id, message_id=message_id, user_id=user_id, data=data)

    async def _maybe_handle_projects_dialog_input(self, message: dict[str, Any]) -> bool:
        user_id = (message.get("from") or {}).get("id")
        return await self.projects_service.maybe_handle_dialog_input(message, user_id=user_id)

    async def _maybe_handle_projects_upload(self, message: dict[str, Any]) -> bool:
        user_id = (message.get("from") or {}).get("id")
        return await self.projects_service.maybe_handle_upload(message, user_id=user_id)

    def _format_commit_datetime(self, raw_iso: str) -> str:
        text = (raw_iso or "").strip()
        if not text:
            return "-"
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _format_commit_brief(self, item: CommitInfo | None) -> str:
        if item is None:
            return "-"
        stamp = self._format_commit_datetime(item.date_iso)
        subject = (item.subject or "").strip()
        return f"{item.short_hash} ({stamp}) {subject}"

    def _update_panel_keyboard(self, status: UpdateStatus) -> dict[str, Any]:
        updates_label = "🚀 Обновить сейчас" if status.has_updates else "🚀 Проверить и обновить"
        rows: list[list[dict[str, str]]] = [
            [{"text": "🔄 Обновить статус", "callback_data": "update:refresh"}],
            [
                {"text": "📋 Показать изменения", "callback_data": "update:changes"},
                {"text": updates_label, "callback_data": "update:run"},
            ],
            [{"text": "↩️ Откатиться", "callback_data": "update:rollback:ask"}],
            [{"text": "⬅️ Назад", "callback_data": "panel:home"}],
        ]
        return {"inline_keyboard": rows}

    def _update_rollback_confirm_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ Подтвердить откат", "callback_data": "update:rollback:run"},
                    {"text": "✖️ Отмена", "callback_data": "update:rollback:cancel"},
                ],
            ]
        }

    def _format_update_status_text(self, status: UpdateStatus) -> str:
        lines = [
            "Обновление проекта",
            "------------------",
            f"Ветка: {status.branch}",
            f"Текущая версия: {self._format_commit_brief(status.current)}",
        ]
        if status.remote:
            lines.append(f"Последняя в origin: {self._format_commit_brief(status.remote)}")
        else:
            lines.append("Последняя в origin: -")

        if status.has_updates:
            lines.append("Статус: доступно обновление.")
        else:
            lines.append("Статус: обновлений нет.")

        if status.commits:
            lines.extend(["", "Коротко по изменениям:"])
            for item in status.commits[:5]:
                lines.append(f"- {item.short_hash} {item.subject}")

        if status.release:
            release_name = (status.release.get("name") or status.release.get("tag") or "").strip()
            if release_name:
                lines.extend(["", f"Release: {release_name}"])

        if status.errors:
            lines.extend(["", "Ошибки при проверке:"])
            lines.extend(f"- {err}" for err in status.errors)
        return "\n".join(lines)

    def _chunk_text(self, text: str, limit: int = 3500) -> list[str]:
        clean = text.strip()
        if not clean:
            return []
        if len(clean) <= limit:
            return [clean]
        chunks: list[str] = []
        current = ""
        for line in clean.splitlines():
            candidate = f"{current}\n{line}".strip() if current else line
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
            if len(line) <= limit:
                current = line
            else:
                start = 0
                while start < len(line):
                    part = line[start : start + limit]
                    if len(part) == limit:
                        chunks.append(part)
                    else:
                        current = part
                    start += limit
                if len(line) % limit == 0:
                    current = ""
        if current:
            chunks.append(current)
        return chunks

    async def _show_update_panel(self, *, chat_id: int | None, message_id: int | None) -> None:
        if chat_id is None:
            return
        status = await asyncio.to_thread(get_update_status, self.settings)
        text = self._format_update_status_text(status)
        await self._safe_edit_or_send(chat_id, message_id, text, self._update_panel_keyboard(status))

    async def _send_update_changes(self, *, chat_id: int) -> None:
        status = await asyncio.to_thread(get_update_status, self.settings)
        lines = [
            "Изменения перед обновлением",
            "---------------------------",
            f"Ветка: {status.branch}",
            "",
        ]
        if status.release:
            release_name = (status.release.get("name") or status.release.get("tag") or "").strip() or "-"
            published = self._format_commit_datetime(str(status.release.get("published_at") or ""))
            lines.extend(
                [
                    f"Release: {release_name}",
                    f"Опубликован: {published}",
                ]
            )
            release_url = str(status.release.get("url") or "").strip()
            if release_url:
                lines.append(f"URL: {release_url}")
            body = str(status.release.get("body") or "").strip()
            if body:
                lines.extend(["", "Release notes:", body])
        elif status.changelog_excerpt:
            lines.extend(["CHANGELOG.md (фрагмент):", status.changelog_excerpt])
        elif status.commits:
            lines.append("Релиз-нот не найден, показываю коммиты.")
        else:
            lines.append("Изменений между текущей и удаленной версией нет.")

        if status.commits:
            lines.extend(["", "Коммиты:"])
            for item in status.commits:
                lines.append(
                    f"- {item.short_hash} {item.subject} ({item.author}, {self._format_commit_datetime(item.date_iso)})"
                )

        text = "\n".join(lines).strip()
        for chunk in self._chunk_text(text):
            await self._safe_send(chat_id, chunk)

    async def _run_update_flow(self, *, chat_id: int | None, message_id: int | None) -> None:
        if chat_id is None:
            return
        await self._safe_edit_or_send(chat_id, message_id, "Обновляю проект, подождите...", None)
        result = await asyncio.to_thread(run_update, self.settings)
        status = await asyncio.to_thread(get_update_status, self.settings)
        log_tail = await asyncio.to_thread(read_update_log_tail, self.settings, 40)

        summary_lines = []
        if result.ok:
            summary_lines.append("✅ Обновление завершено.")
        else:
            summary_lines.append("❌ Обновление завершилось с ошибкой.")
            if result.error:
                summary_lines.append(f"Ошибка: {result.error}")

        summary_lines.extend(
            [
                f"Ветка: {result.branch}",
                f"Было: {self._format_commit_brief(result.before)}",
                f"Стало: {self._format_commit_brief(result.after)}",
                f"Изменено файлов: {len(result.changed_files)}",
            ]
        )
        if result.steps:
            summary_lines.append("Шаги: " + ", ".join(result.steps))

        await self._safe_edit_or_send(
            chat_id,
            message_id,
            "\n".join(summary_lines),
            self._update_panel_keyboard(status),
        )

        if log_tail:
            for chunk in self._chunk_text(f"Логи обновления (последние строки):\n{log_tail}"):
                await self._safe_send(chat_id, chunk)

    async def _run_rollback_flow(self, *, chat_id: int | None, message_id: int | None) -> None:
        if chat_id is None:
            return
        await self._safe_edit_or_send(chat_id, message_id, "Выполняю откат, подождите...", None)
        result = await asyncio.to_thread(rollback, self.settings, None)
        status = await asyncio.to_thread(get_update_status, self.settings)
        log_tail = await asyncio.to_thread(read_update_log_tail, self.settings, 40)

        lines = []
        if result.ok:
            lines.append("✅ Откат выполнен.")
        else:
            lines.append("❌ Откат завершился с ошибкой.")
            if result.error:
                lines.append(f"Ошибка: {result.error}")
        lines.extend(
            [
                f"Целевой commit: {result.target_commit or '-'}",
                f"Было: {self._format_commit_brief(result.before)}",
                f"Стало: {self._format_commit_brief(result.after)}",
            ]
        )
        if result.steps:
            lines.append("Шаги: " + ", ".join(result.steps))

        await self._safe_edit_or_send(
            chat_id,
            message_id,
            "\n".join(lines),
            self._update_panel_keyboard(status),
        )

        if log_tail:
            for chunk in self._chunk_text(f"Логи обновления (последние строки):\n{log_tail}"):
                await self._safe_send(chat_id, chunk)

    def _profile_paths(self) -> tuple[Path, Path, Path]:
        return (
            Path(self.settings.profile_json_path),
            Path(self.settings.profile_backups_dir),
            Path(self.settings.profile_audit_log_path),
        )

    def _avatars_dir(self) -> Path:
        return Path(self.settings.profile_avatars_dir)

    def _set_avatar_upload_context(self, user_id: int, chat_id: int, *, ttl_seconds: int = 600) -> None:
        self.profile_avatar_upload_context[user_id] = {
            "chat_id": chat_id,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        }

    def _is_avatar_upload_allowed(self, user_id: int, chat_id: int) -> bool:
        context = self.profile_avatar_upload_context.get(user_id)
        if not isinstance(context, dict):
            return False
        if context.get("chat_id") != chat_id:
            return False
        expires_at = context.get("expires_at")
        if not isinstance(expires_at, datetime):
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) <= expires_at.astimezone(timezone.utc)

    def _avatar_path_to_url(self, avatar_path: Path) -> str:
        root = Path.cwd().resolve()
        resolved = avatar_path.resolve()
        try:
            relative = resolved.relative_to(root)
            return relative.as_posix()
        except ValueError:
            return resolved.as_posix()

    def _avatar_url_to_path(self, avatar_url: str | None) -> Path | None:
        if not avatar_url:
            return None
        raw = str(avatar_url).strip().replace("\\", "/")
        if not raw:
            return None

        candidate = Path(raw)
        resolved = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
        avatars_root = self._avatars_dir().resolve()
        try:
            resolved.relative_to(avatars_root)
        except ValueError:
            return None
        if not resolved.exists() or not resolved.is_file():
            return None
        return resolved

    def _list_avatar_files(self) -> list[Path]:
        avatars_dir = self._avatars_dir()
        avatars_dir.mkdir(parents=True, exist_ok=True)
        files = [
            item
            for item in avatars_dir.iterdir()
            if item.is_file() and item.suffix.lower() in ALLOWED_AVATAR_EXTENSIONS
        ]
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return files

    def _resolve_avatar_cursor(self, user_id: int, profile: dict[str, Any], avatars: list[Path]) -> int:
        if not avatars:
            self.profile_avatar_cursor[user_id] = 0
            return 0

        saved_idx = self.profile_avatar_cursor.get(user_id)
        if isinstance(saved_idx, int) and 0 <= saved_idx < len(avatars):
            return saved_idx

        current_path = self._avatar_url_to_path(profile.get("avatar_url"))
        if current_path and current_path in avatars:
            idx = avatars.index(current_path)
        else:
            idx = 0
        self.profile_avatar_cursor[user_id] = idx
        return idx

    def _profile_avatars_keyboard(self, has_avatars: bool) -> dict[str, Any]:
        if not has_avatars:
            return {
                "inline_keyboard": [
                    [
                        {"text": "⬅️ К профилю", "callback_data": "profile:panel"},
                    ],
                ]
            }
        return {
            "inline_keyboard": [
                [
                    {"text": "◀️ Пред", "callback_data": "profile:avatars:prev"},
                    {"text": "▶️ След", "callback_data": "profile:avatars:next"},
                ],
                [
                    {"text": "✅ Сделать текущей", "callback_data": "profile:avatars:set"},
                    {"text": "📤 Показать", "callback_data": "profile:avatars:show"},
                ],
                [
                    {"text": "⬅️ К профилю", "callback_data": "profile:panel"},
                ],
            ]
        }

    async def _render_avatar_panel(
        self,
        *,
        chat_id: int,
        message_id: int | None,
        user_id: int,
        send_preview: bool = False,
    ) -> None:
        self._set_avatar_upload_context(user_id, chat_id)
        profile_path, _, _ = self._profile_paths()
        profile = ensure_profile_exists(profile_path)
        avatars = self._list_avatar_files()
        if not avatars:
            await self._safe_edit_or_send(
                chat_id,
                message_id,
                (
                    "Аватары\n"
                    "-------\n"
                    "Список пуст.\n"
                    "Просто отправьте боту фото или image-файл, чтобы добавить новый аватар."
                ),
                self._profile_avatars_keyboard(False),
            )
            return

        idx = self._resolve_avatar_cursor(user_id, profile, avatars)
        selected = avatars[idx]
        selected_url = self._avatar_path_to_url(selected)
        is_current = profile.get("avatar_url") == selected_url
        current_value = profile.get("avatar_url") or "не установлен"

        lines = [
            "Аватары",
            "-------",
            f"Выбран: {idx + 1}/{len(avatars)}",
            f"Файл: {selected.name}",
            f"Текущий на сайте: {current_value}",
            f"Кандидат: {selected_url}",
            "",
            "Чтобы добавить новую аватарку: отправьте фото или image-файл боту.",
        ]
        if is_current:
            lines.append("Этот файл уже активен на сайте.")

        await self._safe_edit_or_send(
            chat_id,
            message_id,
            "\n".join(lines),
            self._profile_avatars_keyboard(True),
        )

        if send_preview and self.bot_client:
            await self.bot_client.send_document(
                chat_id,
                selected,
                caption=f"Аватар {idx + 1}/{len(avatars)}: {selected.name}",
            )

    def _guess_avatar_extension(
        self,
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
        telegram_file_path: str | None = None,
    ) -> str:
        for raw_value in (file_name, telegram_file_path):
            if raw_value:
                suffix = Path(str(raw_value)).suffix.lower()
                if suffix in ALLOWED_AVATAR_EXTENSIONS:
                    return suffix
        mime_map = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        mapped = mime_map.get((mime_type or "").strip().lower())
        return mapped or ""

    def _extract_avatar_meta(self, message: dict[str, Any]) -> tuple[str, str, int, str] | None:
        photo_items = message.get("photo")
        if isinstance(photo_items, list) and photo_items:
            best = None
            for item in photo_items:
                if not isinstance(item, dict) or not item.get("file_id"):
                    continue
                if best is None or int(item.get("file_size") or 0) >= int(best.get("file_size") or 0):
                    best = item
            if best and best.get("file_id"):
                file_size = int(best.get("file_size") or 0)
                return str(best["file_id"]), ".jpg", file_size, "photo"

        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            mime_type = str(document.get("mime_type") or "").lower()
            file_name = str(document.get("file_name") or "")
            extension = self._guess_avatar_extension(file_name=file_name, mime_type=mime_type)
            is_image = mime_type.startswith("image/") or extension in ALLOWED_AVATAR_EXTENSIONS
            if not is_image:
                return None
            if not extension:
                extension = ".jpg"
            file_size = int(document.get("file_size") or 0)
            return str(document["file_id"]), extension, file_size, "document"

        return None

    async def _maybe_handle_profile_avatar_upload(self, message: dict[str, Any]) -> bool:
        from_user = message.get("from") or {}
        user_id = from_user.get("id")
        if user_id is None or not is_owner(user_id, self.settings):
            return False
        if not self.bot_client:
            return False

        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return False
        if not self._is_avatar_upload_allowed(user_id, chat_id):
            return False

        meta = self._extract_avatar_meta(message)
        if meta is None:
            return False

        file_id, extension, file_size, source_kind = meta
        if file_size > MAX_AVATAR_FILE_SIZE:
            await self._safe_send(
                chat_id,
                f"Файл слишком большой ({file_size // (1024 * 1024)} MB). Максимум: {MAX_AVATAR_FILE_SIZE // (1024 * 1024)} MB.",
            )
            return True

        try:
            file_meta = await self.bot_client.get_file(file_id)
            telegram_file_path = file_meta.get("file_path")
            if not telegram_file_path:
                raise RuntimeError("Telegram не вернул file_path")

            extension = self._guess_avatar_extension(
                file_name=None,
                mime_type=(message.get("document") or {}).get("mime_type") if isinstance(message.get("document"), dict) else None,
                telegram_file_path=telegram_file_path,
            ) if extension == ".jpg" else extension
            if not extension:
                extension = ".jpg"

            avatars_dir = self._avatars_dir()
            avatars_dir.mkdir(parents=True, exist_ok=True)

            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            local_path = avatars_dir / f"avatar_{stamp}{extension}"
            await self.bot_client.download_file(str(telegram_file_path), local_path)

            avatar_url = self._avatar_path_to_url(local_path)
            profile_path, backups_dir, audit_log_path = self._profile_paths()
            profile = load_profile(profile_path)
            profile["avatar_url"] = avatar_url

            new_profile, backup_path, _ = save_profile_with_backup(
                profile_path=profile_path,
                backup_dir=backups_dir,
                audit_log_path=audit_log_path,
                actor_user_id=user_id,
                action="profile_avatar_upload",
                profile_data=profile,
                payload={
                    "avatar_url": avatar_url,
                    "source": source_kind,
                    "file_size": file_size,
                },
            )

            avatars = self._list_avatar_files()
            self.profile_avatar_cursor[user_id] = avatars.index(local_path) if local_path in avatars else 0

            lines = [
                "Аватар загружен и установлен как текущий.",
                f"Файл: {local_path.name}",
                f"Путь: {new_profile.get('avatar_url')}",
            ]
            if backup_path:
                lines.append(f"Бэкап: {backup_path.name}")
            lines.append("Листание: /profile_panel -> 🖼 Аватары")
            await self._safe_send(chat_id, "\n".join(lines))
            return True
        except Exception:
            logger.exception("Не удалось загрузить аватар")
            await self._safe_send(chat_id, "Не удалось загрузить аватар. Попробуйте другое фото/файл.")
            return True

    def _profile_panel_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "✏️ Имя/заголовок/описание", "callback_data": "profile:basic"},
                    {"text": "🔗 Ссылки", "callback_data": "profile:links"},
                ],
                [
                    {"text": "🧩 Стек", "callback_data": "profile:stack"},
                    {"text": "📝 Цитата", "callback_data": "profile:set:quote"},
                ],
                [
                    {"text": "🎵 Now listening", "callback_data": "profile:set:now_listening_text"},
                    {"text": "🌤 Weather текст", "callback_data": "profile:set:weather_text"},
                ],
                [
                    {"text": "🎧 Авто-музыка", "callback_data": "profile:toggle:now_auto"},
                    {"text": "🌦 Авто-погода", "callback_data": "profile:toggle:weather_auto"},
                ],
                [
                    {"text": "📍 Локация погоды", "callback_data": "profile:set:weather_location"},
                    {"text": "⏱ Интервал погоды", "callback_data": "profile:set:weather_refresh_minutes"},
                ],
                [
                    {"text": "🔄 Обновить погоду", "callback_data": "profile:weather:refresh"},
                    {"text": "🖼 Аватары", "callback_data": "profile:avatars"},
                ],
                [
                    {"text": "👁 Предпросмотр", "callback_data": "profile:preview"},
                    {"text": "♻️ Откат", "callback_data": "profile:rollback:ask"},
                ],
                [
                    {"text": "⬅️ Назад", "callback_data": "panel:home"},
                ],
            ]
        }
    def _profile_basic_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "Имя", "callback_data": "profile:set:name"},
                    {"text": "Заголовок", "callback_data": "profile:set:title"},
                ],
                [
                    {"text": "Описание", "callback_data": "profile:set:bio"},
                    {"text": "Username", "callback_data": "profile:set:username"},
                ],
                [
                    {"text": "Telegram URL", "callback_data": "profile:set:telegram_url"},
                    {"text": "Avatar URL", "callback_data": "profile:set:avatar_url"},
                ],
                [
                    {"text": "⬅️ К профилю", "callback_data": "profile:panel"},
                ],
            ]
        }

    def _profile_links_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "➕ Добавить", "callback_data": "profile:links:add"},
                    {"text": "✏️ Переименовать", "callback_data": "profile:links:rename"},
                ],
                [
                    {"text": "🗑 Удалить", "callback_data": "profile:links:delete"},
                    {"text": "⬅️ К профилю", "callback_data": "profile:panel"},
                ],
            ]
        }

    def _profile_stack_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "➕ Добавить", "callback_data": "profile:stack:add"},
                    {"text": "🗑 Удалить", "callback_data": "profile:stack:delete"},
                ],
                [
                    {"text": "♻️ Заменить весь", "callback_data": "profile:stack:replace"},
                    {"text": "⬅️ К профилю", "callback_data": "profile:panel"},
                ],
            ]
        }

    def _profile_confirm_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ Сохранить", "callback_data": "profile:confirm:save"},
                    {"text": "✖️ Отмена", "callback_data": "profile:confirm:cancel"},
                ],
            ]
        }

    def _profile_rollback_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ Откатить", "callback_data": "profile:rollback:apply"},
                    {"text": "✖️ Отмена", "callback_data": "profile:rollback:cancel"},
                ],
            ]
        }

    async def _show_profile_panel(self, chat_id: int, message_id: int | None = None) -> None:
        profile_path, _, _ = self._profile_paths()
        profile = ensure_profile_exists(profile_path)
        weather_location = profile.get("weather_location_name") or "Москва"
        weather_coords = f"{profile.get('weather_latitude')}, {profile.get('weather_longitude')}"
        weather_timezone = profile.get("weather_timezone") or "Europe/Moscow"
        weather_interval = profile.get("weather_refresh_minutes") or 60
        text = (
            "Редактор профиля сайта\n"
            "----------------------\n"
            "Изменяются только разрешенные поля profile.json.\n"
            "Редактирование произвольных файлов отключено.\n\n"
            f"Текущее имя: {profile.get('name') or '-'}\n"
            f"Текущий заголовок: {profile.get('title') or '-'}\n"
            f"Текущий avatar_url: {profile.get('avatar_url') or 'не задан'}\n"
            f"Авто now playing: {'вкл' if profile.get('now_listening_auto_enabled', True) else 'выкл'}\n"
            f"Авто-погода: {'вкл' if profile.get('weather_auto_enabled', True) else 'выкл'}\n"
            f"Локация погоды: {weather_location} ({weather_coords})\n"
            f"Часовой пояс: {weather_timezone}\n"
            f"Интервал погоды: {weather_interval} мин.\n\n"
            "Добавление аватара: откройте раздел «🖼 Аватары», затем отправьте фото/файл в этот же чат.\n"
            "Команда для локации: /weatherloc Москва | 55.7558 | 37.6176 | Europe/Moscow\n"
            "Ручное обновление: /weatherrefresh\n"
            "Выберите раздел ниже."
        )
        await self._safe_edit_or_send(chat_id, message_id, text, self._profile_panel_keyboard())

    def _clear_profile_dialog(self, user_id: int) -> None:
        self.profile_dialogs.pop(user_id, None)

    async def _start_profile_dialog(
        self,
        *,
        user_id: int,
        chat_id: int,
        action: str,
        prompt: str,
        audit_action: str,
        audit_payload: dict[str, Any] | None = None,
    ) -> None:
        self.profile_dialogs[user_id] = {
            "chat_id": chat_id,
            "action": action,
            "step": "await_input",
            "audit_action": audit_action,
            "audit_payload": audit_payload or {},
        }
        text = f"{prompt}\n\nОтправьте ответ текстом. Для отмены: /cancel"
        await self._safe_send(chat_id, text)

    def _render_links_short(self, profile: dict[str, Any]) -> str:
        links = profile.get("links") or []
        if not links:
            return "Ссылок пока нет."
        lines = ["Текущие ссылки:"]
        for idx, item in enumerate(links, start=1):
            lines.append(f"{idx}. {item.get('label', '-')} -> {item.get('url', '-')}")
        return "\n".join(lines)

    def _render_stack_short(self, profile: dict[str, Any]) -> str:
        stack = profile.get("stack") or []
        if not stack:
            return "Стек пока пуст."
        lines = ["Текущий стек:"]
        for idx, item in enumerate(stack, start=1):
            lines.append(f"{idx}. {item}")
        return "\n".join(lines)

    async def _handle_profile_callback(
        self,
        chat_id: int | None,
        message_id: int | None,
        user_id: int,
        data: str,
    ) -> None:
        if chat_id is None:
            return

        profile_path, backups_dir, audit_log_path = self._profile_paths()
        profile = ensure_profile_exists(profile_path)

        if data in ("profile:panel", "profile:home"):
            await self._show_profile_panel(chat_id, message_id)
            return

        if data == "profile:basic":
            text = (
                "Редактирование основных полей\n"
                "-----------------------------\n"
                f"Имя: {profile.get('name') or '-'}\n"
                f"Заголовок: {profile.get('title') or '-'}\n"
                f"Описание: {profile.get('bio') or '-'}"
            )
            await self._safe_edit_or_send(chat_id, message_id, text, self._profile_basic_keyboard())
            return

        if data == "profile:links":
            text = (
                "Редактирование ссылок\n"
                "---------------------\n"
                f"{self._render_links_short(profile)}\n\n"
                "Выберите действие."
            )
            await self._safe_edit_or_send(chat_id, message_id, text, self._profile_links_keyboard())
            return

        if data == "profile:stack":
            text = (
                "Редактирование стека\n"
                "--------------------\n"
                f"{self._render_stack_short(profile)}\n\n"
                "Выберите действие."
            )
            await self._safe_edit_or_send(chat_id, message_id, text, self._profile_stack_keyboard())
            return

        if data == "profile:toggle:now_auto":
            profile["now_listening_auto_enabled"] = not bool(profile.get("now_listening_auto_enabled", True))
            new_profile, backup_path, _ = save_profile_with_backup(
                profile_path=profile_path,
                backup_dir=backups_dir,
                audit_log_path=audit_log_path,
                actor_user_id=user_id,
                action="profile_toggle_now_listening_auto",
                profile_data=profile,
                payload={"enabled": bool(profile.get("now_listening_auto_enabled"))},
            )
            lines = [
                f"Авто now listening: {'вкл' if new_profile.get('now_listening_auto_enabled', True) else 'выкл'}",
            ]
            if backup_path:
                lines.append(f"Бэкап: {backup_path.name}")
            await self._safe_edit_or_send(chat_id, message_id, "\n".join(lines), self._profile_panel_keyboard())
            return

        if data == "profile:toggle:weather_auto":
            profile["weather_auto_enabled"] = not bool(profile.get("weather_auto_enabled", True))
            new_profile, backup_path, _ = save_profile_with_backup(
                profile_path=profile_path,
                backup_dir=backups_dir,
                audit_log_path=audit_log_path,
                actor_user_id=user_id,
                action="profile_toggle_weather_auto",
                profile_data=profile,
                payload={"enabled": bool(profile.get("weather_auto_enabled"))},
            )
            lines = [
                f"Авто-погода: {'вкл' if new_profile.get('weather_auto_enabled', True) else 'выкл'}",
            ]
            if backup_path:
                lines.append(f"Бэкап: {backup_path.name}")
            await self._safe_edit_or_send(chat_id, message_id, "\n".join(lines), self._profile_panel_keyboard())
            return

        if data == "profile:weather:refresh":
            updated = await sync_profile_weather(self.settings, force=True)
            fresh_profile = load_profile(profile_path)
            weather_text = fresh_profile.get("weather_text") or "нет данных"
            state_text = "Погода обновлена." if updated else "Не удалось обновить погоду."
            await self._safe_edit_or_send(
                chat_id,
                message_id,
                f"{state_text}\n{weather_text}",
                self._profile_panel_keyboard(),
            )
            return

        if data == "profile:avatars":
            await self._render_avatar_panel(chat_id=chat_id, message_id=message_id, user_id=user_id, send_preview=True)
            return
        if data == "profile:avatars:prev":
            avatars = self._list_avatar_files()
            if not avatars:
                await self._render_avatar_panel(chat_id=chat_id, message_id=message_id, user_id=user_id, send_preview=False)
                return
            current = self._resolve_avatar_cursor(user_id, profile, avatars)
            self.profile_avatar_cursor[user_id] = (current - 1) % len(avatars)
            await self._render_avatar_panel(chat_id=chat_id, message_id=message_id, user_id=user_id, send_preview=True)
            return
        if data == "profile:avatars:next":
            avatars = self._list_avatar_files()
            if not avatars:
                await self._render_avatar_panel(chat_id=chat_id, message_id=message_id, user_id=user_id, send_preview=False)
                return
            current = self._resolve_avatar_cursor(user_id, profile, avatars)
            self.profile_avatar_cursor[user_id] = (current + 1) % len(avatars)
            await self._render_avatar_panel(chat_id=chat_id, message_id=message_id, user_id=user_id, send_preview=True)
            return
        if data == "profile:avatars:show":
            await self._render_avatar_panel(chat_id=chat_id, message_id=message_id, user_id=user_id, send_preview=True)
            return
        if data == "profile:avatars:set":
            avatars = self._list_avatar_files()
            if not avatars:
                await self._render_avatar_panel(chat_id=chat_id, message_id=message_id, user_id=user_id, send_preview=False)
                return
            idx = self._resolve_avatar_cursor(user_id, profile, avatars)
            chosen = avatars[idx]
            chosen_url = self._avatar_path_to_url(chosen)
            profile["avatar_url"] = chosen_url
            new_profile, backup_path, changed = save_profile_with_backup(
                profile_path=profile_path,
                backup_dir=backups_dir,
                audit_log_path=audit_log_path,
                actor_user_id=user_id,
                action="profile_avatar_set_from_gallery",
                profile_data=profile,
                payload={
                    "avatar_url": chosen_url,
                    "file_name": chosen.name,
                    "index": idx + 1,
                    "total": len(avatars),
                },
            )
            lines = ["Аватар применен."]
            if changed:
                lines.append(f"Измененные поля: {', '.join(changed)}")
            if backup_path:
                lines.append(f"Бэкап: {backup_path.name}")
            lines.append(f"Текущий avatar_url: {new_profile.get('avatar_url') or '-'}")
            lines.append("")
            lines.append("Можно листать дальше или вернуться в профиль.")
            await self._safe_edit_or_send(
                chat_id,
                message_id,
                "\n".join(lines),
                self._profile_avatars_keyboard(True),
            )
            return

        if data == "profile:preview":
            text = profile_preview_text(profile, self.settings.profile_public_url or None)
            await self._safe_edit_or_send(chat_id, message_id, text, self._profile_panel_keyboard())
            return

        if data == "profile:rollback:ask":
            await self._safe_edit_or_send(
                chat_id,
                message_id,
                "Откатить profile.json к последней резервной копии?",
                self._profile_rollback_keyboard(),
            )
            return

        if data == "profile:rollback:cancel":
            await self._show_profile_panel(chat_id, message_id)
            return

        if data == "profile:rollback:apply":
            try:
                restored, restored_from, previous_backup = rollback_last_profile_version(
                    profile_path=profile_path,
                    backup_dir=backups_dir,
                    audit_log_path=audit_log_path,
                    actor_user_id=user_id,
                )
            except FileNotFoundError:
                await self._safe_edit_or_send(
                    chat_id,
                    message_id,
                    "Откат невозможен: резервные копии не найдены.",
                    self._profile_panel_keyboard(),
                )
                return

            lines = [
                "Откат выполнен.",
                f"Восстановлено из: {restored_from.name}",
            ]
            if previous_backup:
                lines.append(f"Текущая версия перед откатом сохранена: {previous_backup.name}")
            lines.extend(["", profile_preview_text(restored, self.settings.profile_public_url or None)])
            await self._safe_edit_or_send(chat_id, message_id, "\n".join(lines), self._profile_panel_keyboard())
            return

        if data.startswith("profile:set:"):
            field_name = data.split(":", maxsplit=2)[2]
            prompts = {
                "name": ("set:name", "profile_set_name", "Введите новое имя для профиля."),
                "title": ("set:title", "profile_set_title", "Введите новый заголовок (title)."),
                "bio": ("set:bio", "profile_set_bio", "Введите новое описание (bio)."),
                "username": ("set:username", "profile_set_username", "Введите username без @."),
                "telegram_url": ("set:telegram_url", "profile_set_telegram_url", "Введите Telegram URL (https://...)."),
                "avatar_url": ("set:avatar_url", "profile_set_avatar_url", "Введите URL аватара (https://...) или '-' чтобы очистить."),
                "quote": ("set:quote", "profile_set_quote", "Введите новую цитату."),
                "now_listening_text": (
                    "set:now_listening_text",
                    "profile_set_now_listening_text",
                    "Введите текст для блока Now listening (если авто-музыка выключена).",
                ),
                "weather_text": (
                    "set:weather_text",
                    "profile_set_weather_text",
                    "Введите текст для блока Weather (если авто-погода выключена).",
                ),
                "weather_location": (
                    "set:weather_location",
                    "profile_set_weather_location",
                    "Формат: Название | Широта | Долгота | Timezone (опционально).\n"
                    "Пример: Москва | 55.7558 | 37.6176 | Europe/Moscow",
                ),
                "weather_refresh_minutes": (
                    "set:weather_refresh_minutes",
                    "profile_set_weather_refresh_minutes",
                    "Введите интервал авто-обновления погоды в минутах (10..720).",
                ),
            }
            selected = prompts.get(field_name)
            if not selected:
                await self._safe_edit_or_send(chat_id, message_id, "Неизвестное поле.", self._profile_panel_keyboard())
                return
            action, audit_action, prompt = selected
            await self._start_profile_dialog(
                user_id=user_id,
                chat_id=chat_id,
                action=action,
                prompt=prompt,
                audit_action=audit_action,
                audit_payload={"field": field_name},
            )
            return

        if data == "profile:links:add":
            await self._start_profile_dialog(
                user_id=user_id,
                chat_id=chat_id,
                action="links:add",
                prompt="Формат: Название | https://example.com",
                audit_action="profile_links_add",
            )
            return

        if data == "profile:links:rename":
            await self._start_profile_dialog(
                user_id=user_id,
                chat_id=chat_id,
                action="links:rename",
                prompt=(
                    f"{self._render_links_short(profile)}\n\n"
                    "Формат: Номер | Новое название | https://new-url (URL можно не указывать)"
                ),
                audit_action="profile_links_rename",
            )
            return

        if data == "profile:links:delete":
            await self._start_profile_dialog(
                user_id=user_id,
                chat_id=chat_id,
                action="links:delete",
                prompt=f"{self._render_links_short(profile)}\n\nВведите номер ссылки для удаления.",
                audit_action="profile_links_delete",
            )
            return

        if data == "profile:stack:add":
            await self._start_profile_dialog(
                user_id=user_id,
                chat_id=chat_id,
                action="stack:add",
                prompt="Введите технологию для добавления в стек.",
                audit_action="profile_stack_add",
            )
            return

        if data == "profile:stack:delete":
            await self._start_profile_dialog(
                user_id=user_id,
                chat_id=chat_id,
                action="stack:delete",
                prompt=f"{self._render_stack_short(profile)}\n\nВведите номер элемента для удаления.",
                audit_action="profile_stack_delete",
            )
            return

        if data == "profile:stack:replace":
            await self._start_profile_dialog(
                user_id=user_id,
                chat_id=chat_id,
                action="stack:replace",
                prompt="Введите новый стек через запятую. Пример: Python, FastAPI, PostgreSQL",
                audit_action="profile_stack_replace",
            )
            return

        if data == "profile:confirm:cancel":
            self._clear_profile_dialog(user_id)
            await self._safe_edit_or_send(chat_id, message_id, "Изменение отменено.", self._profile_panel_keyboard())
            return

        if data == "profile:confirm:save":
            state = self.profile_dialogs.get(user_id)
            if not state or state.get("step") != "await_confirm":
                await self._safe_edit_or_send(
                    chat_id,
                    message_id,
                    "Нет ожидающих изменений для сохранения.",
                    self._profile_panel_keyboard(),
                )
                return

            candidate_profile = state.get("candidate_profile")
            if not isinstance(candidate_profile, dict):
                self._clear_profile_dialog(user_id)
                await self._safe_edit_or_send(
                    chat_id,
                    message_id,
                    "Черновик изменений поврежден. Повторите редактирование.",
                    self._profile_panel_keyboard(),
                )
                return

            new_profile, backup_path, changed = save_profile_with_backup(
                profile_path=profile_path,
                backup_dir=backups_dir,
                audit_log_path=audit_log_path,
                actor_user_id=user_id,
                action=str(state.get("audit_action") or "profile_update"),
                profile_data=candidate_profile,
                payload=state.get("audit_payload") if isinstance(state.get("audit_payload"), dict) else None,
            )
            if (
                bool(new_profile.get("weather_auto_enabled", True))
                and any(
                    field in changed
                    for field in (
                        "weather_auto_enabled",
                        "weather_location_name",
                        "weather_latitude",
                        "weather_longitude",
                        "weather_timezone",
                        "weather_refresh_minutes",
                    )
                )
            ):
                await sync_profile_weather(self.settings, force=True)
                new_profile = load_profile(profile_path)
            self._clear_profile_dialog(user_id)

            lines = ["Изменения сохранены."]
            if changed:
                lines.append(f"Измененные поля: {', '.join(changed)}")
            if backup_path:
                lines.append(f"Бэкап: {backup_path.name}")
            lines.extend(["", profile_preview_text(new_profile, self.settings.profile_public_url or None)])
            await self._safe_edit_or_send(chat_id, message_id, "\n".join(lines), self._profile_panel_keyboard())
            return

    async def _maybe_handle_profile_dialog_input(self, message: dict[str, Any]) -> bool:
        from_user = message.get("from") or {}
        user_id = from_user.get("id")
        if user_id is None:
            return False

        state = self.profile_dialogs.get(user_id)
        if not state:
            return False

        if not is_owner(user_id, self.settings):
            self._clear_profile_dialog(user_id)
            return False

        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or state.get("chat_id") != chat_id:
            return False

        text = (message.get("text") or message.get("caption") or "").strip()
        if text.lower() == "/cancel":
            self._clear_profile_dialog(user_id)
            await self._safe_send(chat_id, "Изменение отменено.")
            return True

        if state.get("step") != "await_input":
            return False

        if not text:
            await self._safe_send(chat_id, "Нужно отправить текст. Для отмены: /cancel")
            return True
        if text.startswith("/"):
            return False

        profile_path, _, _ = self._profile_paths()
        profile = load_profile(profile_path)
        try:
            candidate_profile, summary, payload = self._prepare_profile_candidate(
                profile=profile,
                action=str(state.get("action") or ""),
                raw_text=text,
            )
        except ValueError as exc:
            await self._safe_send(chat_id, f"Ошибка: {exc}\nПопробуйте снова или отмените: /cancel")
            return True

        state["step"] = "await_confirm"
        state["candidate_profile"] = candidate_profile
        state["summary"] = summary
        audit_payload = state.get("audit_payload")
        if not isinstance(audit_payload, dict):
            audit_payload = {}
        audit_payload.update(payload)
        state["audit_payload"] = audit_payload

        await self._safe_send(
            chat_id,
            f"{summary}\n\nПодтвердите действие:",
            reply_markup=self._profile_confirm_keyboard(),
        )
        return True

    def _prepare_profile_candidate(
        self,
        *,
        profile: dict[str, Any],
        action: str,
        raw_text: str,
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        candidate = dict(profile)
        candidate["links"] = [
            {"label": str(item.get("label", "")), "url": str(item.get("url", ""))}
            for item in (profile.get("links") or [])
            if isinstance(item, dict)
        ]
        candidate["stack"] = [str(item) for item in (profile.get("stack") or []) if str(item).strip()]

        payload: dict[str, Any] = {}
        value = raw_text.strip()

        if action == "set:name":
            candidate["name"] = value
            return candidate, f"Новое имя: {value}", {"value": value}
        if action == "set:title":
            candidate["title"] = value
            return candidate, f"Новый заголовок: {value}", {"value": value}
        if action == "set:bio":
            candidate["bio"] = value
            return candidate, "Описание обновлено.", {"value": value[:500]}
        if action == "set:username":
            username = value.lstrip("@").strip()
            if not username:
                raise ValueError("Username не должен быть пустым")
            candidate["username"] = username
            return candidate, f"Username: {username}", {"value": username}
        if action == "set:telegram_url":
            url = validate_http_url(value, field_name="telegram_url")
            if not url:
                raise ValueError("Telegram URL не должен быть пустым")
            candidate["telegram_url"] = url
            return candidate, f"Telegram URL: {url}", {"value": url}
        if action == "set:avatar_url":
            avatar = "" if value == "-" else validate_http_url(value, field_name="avatar_url")
            candidate["avatar_url"] = avatar
            summary = "Avatar URL очищен." if not avatar else f"Avatar URL: {avatar}"
            return candidate, summary, {"value": avatar}
        if action == "set:quote":
            candidate["quote"] = value
            return candidate, "Цитата обновлена.", {"value": value[:500]}
        if action == "set:now_listening_text":
            candidate["now_listening_text"] = value
            return candidate, "Now listening обновлен.", {"value": value[:500]}
        if action == "set:weather_text":
            candidate["weather_text"] = value
            return candidate, "Weather обновлен.", {"value": value[:500]}
        if action == "set:weather_location":
            location_name, latitude, longitude, timezone_name = parse_weather_location_input(value)
            candidate["weather_location_name"] = location_name
            candidate["weather_latitude"] = latitude
            candidate["weather_longitude"] = longitude
            candidate["weather_timezone"] = timezone_name
            candidate["weather_auto_enabled"] = True
            summary = (
                "Локация погоды обновлена.\n"
                f"{location_name}: {latitude}, {longitude}, timezone={timezone_name}"
            )
            return (
                candidate,
                summary,
                {
                    "location_name": location_name,
                    "latitude": latitude,
                    "longitude": longitude,
                    "timezone": timezone_name,
                },
            )
        if action == "set:weather_refresh_minutes":
            try:
                refresh_minutes = int(float(value.replace(",", ".")))
            except ValueError as exc:
                raise ValueError("Интервал должен быть числом") from exc
            if refresh_minutes < 10 or refresh_minutes > 720:
                raise ValueError("Интервал должен быть в диапазоне 10..720 минут")
            candidate["weather_refresh_minutes"] = refresh_minutes
            return (
                candidate,
                f"Интервал погоды: {refresh_minutes} мин.",
                {"refresh_minutes": refresh_minutes},
            )

        if action == "links:add":
            label, url = parse_link_input(value)
            candidate["links"].append({"label": label, "url": url})
            return candidate, f"Добавлена ссылка: {label} -> {url}", {"label": label, "url": url}

        if action == "links:rename":
            index, new_label, new_url = parse_link_rename_input(value)
            if index >= len(candidate["links"]):
                raise ValueError("Ссылка с таким номером не найдена")
            old = candidate["links"][index]
            candidate["links"][index]["label"] = new_label
            if new_url:
                candidate["links"][index]["url"] = new_url
            return (
                candidate,
                f"Ссылка #{index + 1} обновлена: {old.get('label')} -> {new_label}",
                {"index": index + 1, "label": new_label, "url": candidate["links"][index]["url"]},
            )

        if action == "links:delete":
            index = parse_one_based_index(value)
            if index >= len(candidate["links"]):
                raise ValueError("Ссылка с таким номером не найдена")
            deleted = candidate["links"].pop(index)
            return (
                candidate,
                f"Ссылка удалена: {deleted.get('label', '-')}",
                {"index": index + 1, "label": deleted.get("label"), "url": deleted.get("url")},
            )

        if action == "stack:add":
            chip = value
            if not chip:
                raise ValueError("Название технологии не должно быть пустым")
            candidate["stack"].append(chip)
            return candidate, f"Добавлено в стек: {chip}", {"value": chip}

        if action == "stack:delete":
            index = parse_one_based_index(value)
            if index >= len(candidate["stack"]):
                raise ValueError("Элемент стека с таким номером не найден")
            removed = candidate["stack"].pop(index)
            return candidate, f"Удалено из стека: {removed}", {"index": index + 1, "value": removed}

        if action == "stack:replace":
            new_stack = parse_stack_replace(value)
            candidate["stack"] = new_stack
            return candidate, "Стек полностью обновлен.", {"value": new_stack}

        raise ValueError("Неизвестное действие редактирования")

    async def _send_export(self, session: AsyncSession, chat_id: int, user_id: int) -> None:
        if not self.bot_client:
            await self._safe_send(chat_id, "BOT_TOKEN не настроен.")
            return
        path = await export_messages_csv(session, self.settings.export_root)
        await log_admin_action(session, user_id, "export_messages", {"path": str(path)})
        await self.bot_client.send_document(chat_id, path, caption="Экспорт сообщений")

    def _notify_chat_id(self, config: AppConfig) -> int | None:
        return config.notify_chat_id or self.settings.notify_chat_id or self.settings.owner_user_id

    async def _latest_pc_activity_line(self, session: AsyncSession) -> str | None:
        source = await session.scalar(
            select(HeartbeatSource)
            .where(HeartbeatSource.source_type == "PC_AGENT")
            .order_by(HeartbeatSource.last_seen_at.desc())
            .limit(1)
        )
        if source is None:
            return None
        payload = source.last_payload or {}
        now_playing = (payload.get("now_playing") or "").strip()
        if now_playing:
            return f"Сейчас на ПК {source.source_name}: слушает {now_playing}"
        activity = payload.get("activity") or {}
        text = (activity.get("text") or "").strip()
        if text:
            return f"Сейчас на ПК {source.source_name}: {text}"
        active_app = (payload.get("active_app") or "").strip()
        if active_app:
            return f"Сейчас на ПК {source.source_name}: открыт {active_app}"
        return None

    async def _maybe_handle_away_mode(self, session: AsyncSession, config: AppConfig, message: dict[str, Any]) -> bool:
        if not is_away_mode_active(config, self.settings):
            return False
        from_user = message.get("from") or {}
        user_id = from_user.get("id")
        bypass_user_ids = get_away_bypass_user_ids(config)
        if (
            user_id is None
            or from_user.get("is_bot")
            or is_authorized(user_id, self.settings)
            or user_id in bypass_user_ids
        ):
            return False

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        business_connection_id = message.get("business_connection_id")
        if chat_id is None or message_id is None:
            return False

        away_text = config.away_mode_message or DEFAULT_AWAY_MESSAGE
        activity_line = await self._latest_pc_activity_line(session)
        if activity_line:
            away_text = f"{away_text}\n\n{activity_line}"

        await self._safe_send(chat_id, away_text, business_connection_id=business_connection_id)
        await self._notify_away_capture(session, config, message)

        try:
            if self.bot_client:
                if business_connection_id:
                    await self.bot_client.delete_business_messages(
                        business_connection_id=business_connection_id,
                        chat_id=chat_id,
                        message_ids=[message_id],
                    )
                else:
                    await self.bot_client.delete_message(chat_id, message_id)
        except TelegramApiError as exc:
            logger.warning("Не удалось удалить сообщение в away-режиме: %s", exc)
        except Exception:
            logger.warning("Не удалось удалить сообщение в away-режиме", exc_info=True)
        return True

    async def _notify_away_capture(self, session: AsyncSession, config: AppConfig, message: dict[str, Any]) -> None:
        if not self.bot_client:
            return
        notify_chat_id = self._notify_chat_id(config)
        if notify_chat_id is None:
            return

        from_user = message.get("from") or {}
        source_chat = message.get("chat") or {}
        source_chat_id = source_chat.get("id")
        source_message_id = message.get("message_id")
        business_connection_id = message.get("business_connection_id")
        if source_chat_id is None or source_message_id is None:
            return

        copied = False
        full_name = from_user.get("first_name") or from_user.get("username") or "Пользователь"
        if from_user.get("last_name"):
            full_name = f"{full_name} {from_user['last_name']}".strip()
        try:
            await self.bot_client.copy_message(
                chat_id=notify_chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                business_connection_id=business_connection_id,
            )
            copied = True
        except TelegramApiError as exc:
            logger.warning("Не удалось скопировать исходное сообщение: %s", exc)
        except Exception:
            logger.warning("Не удалось скопировать исходное сообщение", exc_info=True)

        if copied:
            return

        body = (message.get("text") or message.get("caption") or "медиа/без текста").strip()
        info = await self._latest_pc_activity_line(session)
        lines = [
            "📥 <b>Перехвачено в режиме «Не в сети»</b>",
            f"От: <b>{escape(full_name)}</b>",
            f"<blockquote>{escape(body[:1000])}</blockquote>",
        ]
        if info:
            lines.extend(["", f"<i>{escape(info)}</i>"])
        await self._safe_send(notify_chat_id, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

    async def _notify_edit_events(self, session: AsyncSession, config: AppConfig, update: dict[str, Any]) -> None:
        if not self.bot_client:
            return
        notify_chat_id = self._notify_chat_id(config)
        if notify_chat_id is None:
            return

        for key in EDIT_NOTIFICATION_KEYS:
            edited = update.get(key)
            if not edited:
                continue
            chat_id = (edited.get("chat") or {}).get("id")
            message_id = edited.get("message_id")
            if chat_id is None or message_id is None:
                continue
            message_log = await session.scalar(select(MessageLog).where(MessageLog.chat_id == chat_id, MessageLog.telegram_message_id == message_id))
            if message_log is None:
                continue
            revisions = list(await session.scalars(select(MessageRevision).where(MessageRevision.message_id == message_log.id).order_by(MessageRevision.revision_index.asc())))
            if len(revisions) < 2:
                continue
            old_text = revisions[-2].text_content or ""
            new_text = revisions[-1].text_content or ""
            if old_text == new_text:
                continue
            actor = (edited.get("from") or {}).get("first_name") or "Пользователь"
            diff = self._inline_diff(old_text, new_text)
            card = (
                f"✍️ <b>{escape(actor)} изменил сообщение.</b>\n\n"
                f"<b>Старый текст:</b>\n<blockquote>{escape(old_text[:800] or '—')}</blockquote>\n\n"
                f"<b>Новый текст:</b>\n<blockquote>{escape(new_text[:800] or '—')}</blockquote>\n\n"
                f"<b>Изменилось:</b>\n<blockquote>{diff}</blockquote>"
            )
            await self._safe_send(notify_chat_id, card, parse_mode="HTML", disable_web_page_preview=True)

    def _build_deleted_message_links(self, chat_id: int, message_id: int, chat_username: str | None = None) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        username = (chat_username or "").strip().lstrip("@")
        if username:
            links.append(("Открыть в чате", f"https://t.me/{username}/{message_id}"))

        chat_id_str = str(chat_id)
        if chat_id_str.startswith("-100"):
            internal_id = chat_id_str[4:]
            if internal_id.isdigit():
                links.append(("Открыть (t.me/c)", f"https://t.me/c/{internal_id}/{message_id}"))

        links.append(("Открыть в Telegram", f"tg://openmessage?chat_id={chat_id}&message_id={message_id}"))

        deduped: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for label, url in links:
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            deduped.append((label, url))
        return deduped

    def _format_html_links(self, links: list[tuple[str, str]]) -> str:
        if not links:
            return ""
        return " | ".join(f'<a href="{escape(url, quote=True)}">{escape(label)}</a>' for label, url in links)

    async def _notify_deleted_events(self, session: AsyncSession, config: AppConfig, update: dict[str, Any]) -> None:
        if not self.bot_client:
            return
        payload = update.get(DELETED_NOTIFICATION_KEY)
        if not payload:
            return
        notify_chat_id = self._notify_chat_id(config)
        if notify_chat_id is None:
            return
        chat = payload.get("chat") or {}
        chat_id = chat.get("id") or payload.get("chat_id")
        chat_username = chat.get("username")
        message_ids = payload.get("message_ids") or []
        if chat_id is None:
            return
        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            return
        for mid in message_ids:
            try:
                mid_int = int(mid)
            except (TypeError, ValueError):
                continue
            log_item = await session.scalar(select(MessageLog).where(MessageLog.chat_id == chat_id_int, MessageLog.telegram_message_id == mid_int))
            txt = "сообщение удалено до чтения"
            if log_item and log_item.text_content:
                txt = log_item.text_content
            log_chat = (log_item.raw_event or {}).get("chat") if log_item and isinstance(log_item.raw_event, dict) else {}
            log_username = (log_chat or {}).get("username") if isinstance(log_chat, dict) else None
            effective_username = chat_username or log_username
            link_html = self._format_html_links(self._build_deleted_message_links(chat_id_int, mid_int, effective_username))
            link_block = f"\n{link_html}" if link_html else ""
            await self._safe_send(
                notify_chat_id,
                (
                    f"🗑️ <b>Сообщение удалено</b>\n"
                    f"Чат: <code>{chat_id_int}</code> | Msg: <code>{mid_int}</code>{link_block}\n"
                    f"<blockquote>{escape(txt[:900])}</blockquote>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    def _inline_diff(self, old_text: str, new_text: str) -> str:
        if old_text == new_text:
            return escape(new_text)
        p = 0
        m = min(len(old_text), len(new_text))
        while p < m and old_text[p] == new_text[p]:
            p += 1
        oe = len(old_text)
        ne = len(new_text)
        while oe > p and ne > p and old_text[oe - 1] == new_text[ne - 1]:
            oe -= 1
            ne -= 1
        return (
            f"{escape(old_text[:p])}"
            f"{('<s>' + escape(old_text[p:oe]) + '</s>') if old_text[p:oe] else ''}"
            f"{('<b>' + escape(new_text[p:ne]) + '</b>') if new_text[p:ne] else ''}"
            f"{escape(old_text[oe:])}"
        )

    async def _build_logs_text(self, session: AsyncSession) -> str:
        logs = list(await session.scalars(select(MessageLog).order_by(MessageLog.id.desc()).limit(10)))
        if not logs:
            return "Логи\n----\nАрхив пока пуст."
        lines = ["Логи", "----", "Последние 10 сообщений:", ""]
        for i, item in enumerate(logs, start=1):
            state = "УДАЛЕНО" if item.deleted else "АКТИВНО"
            txt = (item.text_content or "<медиа/без текста>").replace("\n", " ")[:90]
            lines.append(f"{i}. chat={item.chat_id} msg={item.telegram_message_id} статус={state}")
            lines.append(f"   текст={txt}")
        return "\n".join(lines)

    async def _handle_media_command(self, session: AsyncSession, chat_id: int, text: str) -> None:
        parts = text.split()
        if len(parts) != 3:
            await self._safe_send(chat_id, "Использование: /media <chat_id> <message_id>")
            return
        try:
            source_chat_id = int(parts[1])
            source_message_id = int(parts[2])
        except ValueError:
            await self._safe_send(chat_id, "chat_id и message_id должны быть числами.")
            return
        log = await session.scalar(select(MessageLog).where(MessageLog.chat_id == source_chat_id, MessageLog.telegram_message_id == source_message_id))
        if not log:
            await self._safe_send(chat_id, "Сообщение не найдено.")
            return
        assets = list(await session.scalars(select(MediaAsset).where(MediaAsset.message_id == log.id)))
        if not assets:
            await self._safe_send(chat_id, "Для этого сообщения нет медиа.")
            return
        if not self.bot_client:
            await self._safe_send(chat_id, "BOT_TOKEN не настроен.")
            return
        sent = False
        for asset in assets:
            if not asset.local_path:
                continue
            p = Path(asset.local_path)
            if not p.exists():
                continue
            await self.bot_client.send_document(chat_id, p, caption=f"{asset.media_type}: {source_chat_id}/{source_message_id}")
            sent = True
        if not sent:
            await self._safe_send(chat_id, "Файлы медиа не найдены на диске.")

    async def _safe_edit_or_send(
        self,
        chat_id: int | None,
        message_id: int | None,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        if chat_id is None or not self.bot_client:
            return
        try:
            if message_id is not None:
                await self.bot_client.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                )
                return
        except Exception:
            pass
        await self._safe_send(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)

    async def _safe_send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
        business_connection_id: str | None = None,
    ) -> None:
        if not self.bot_client:
            return
        try:
            await self.bot_client.send_message(
                chat_id,
                text,
                business_connection_id=business_connection_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
        except TelegramApiError as exc:
            logger.warning("Не удалось отправить сообщение: %s", exc)
        except Exception:
            logger.warning("Не удалось отправить сообщение", exc_info=True)

    async def _answer_callback_safe(self, callback_id: str) -> None:
        if not self.bot_client:
            return
        try:
            await self.bot_client.answer_callback_query(callback_id)
        except TelegramApiError as exc:
            desc = (exc.description or "").lower()
            if exc.status_code == 400 and ("query is too old" in desc or "query_id_invalid" in desc):
                return
            logger.warning("Не удалось подтвердить callback: %s", exc)
        except Exception:
            logger.warning("Не удалось подтвердить callback", exc_info=True)


