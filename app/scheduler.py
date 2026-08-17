import asyncio
import logging
from datetime import datetime, timezone

from app.bot_api import TelegramBotClient
from app.config import Settings
from app.db import SessionLocal
from app.services.app_config import get_or_create_app_config
from app.services.heartbeat import is_quiet_hours, list_sources, mark_offline_sources
from app.services.profile_runtime import sync_profile_now_playing_from_heartbeat, sync_profile_weather

logger = logging.getLogger(__name__)
_alert_state: dict[str, tuple[str, float]] = {}


def source_health_alerts(source: object) -> list[tuple[str, str, str]]:
    payload = getattr(source, "last_payload", {})
    if not isinstance(payload, dict):
        return []
    name = str(getattr(source, "source_name", "agent") or "agent")
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    archive = payload.get("archive_status") if isinstance(payload.get("archive_status"), dict) else {}
    alerts: list[tuple[str, str, str]] = []
    try:
        disk_percent = float(metrics.get("disk_used_percent") or 0)
    except (TypeError, ValueError):
        disk_percent = 0
    if disk_percent >= 92:
        alerts.append((f"{name}:disk", f"{disk_percent:.1f}", f"На агенте {name} заканчивается место: диск занят на {disk_percent:.1f}%."))
    try:
        free_bytes = int(archive.get("free_bytes") or 0)
    except (TypeError, ValueError):
        free_bytes = 0
    if archive.get("enabled") and 0 < free_bytes < 2 * 1024**3:
        free_gb = free_bytes / 1024**3
        alerts.append((f"{name}:archive-space", f"{free_bytes // (64 * 1024**2)}", f"Локальному архиву на {name} осталось {free_gb:.1f} ГБ."))
    archive_error = str(archive.get("last_error") or "").strip()
    if archive_error:
        alerts.append((f"{name}:archive-error", archive_error, f"Ошибка синхронизации архива на {name}: {archive_error[:350]}"))
    agent_error = str(payload.get("last_error") or "").strip()
    if agent_error:
        signature = f"{payload.get('last_error_at') or ''}:{agent_error}"
        alerts.append((f"{name}:agent-error", signature, f"Последняя ошибка агента {name}: {agent_error[:350]}"))
    return alerts


def _new_alerts(alerts: list[tuple[str, str, str]], now_ts: float, cooldown_sec: int = 3600) -> list[str]:
    result: list[str] = []
    for key, signature, text in alerts:
        previous = _alert_state.get(key)
        if previous and previous[0] == signature and now_ts - previous[1] < cooldown_sec:
            continue
        _alert_state[key] = (signature, now_ts)
        result.append(text)
    return result


def _notification_chat_id(settings: Settings, config_notify_chat_id: int | None) -> int | None:
    if config_notify_chat_id:
        return config_notify_chat_id
    if settings.notify_chat_id:
        return settings.notify_chat_id
    if settings.owner_user_id:
        return settings.owner_user_id
    return None


async def offline_check_loop(
    settings: Settings,
    bot_client: TelegramBotClient | None,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            async with SessionLocal() as session:
                config = await get_or_create_app_config(session, settings)
                stale_sources = await mark_offline_sources(session, config.heartbeat_timeout_minutes)
                sources = await list_sources(session)
                await sync_profile_now_playing_from_heartbeat(session, settings, config.heartbeat_timeout_minutes)
                if stale_sources and bot_client and not is_quiet_hours(config, settings):
                    chat_id = _notification_chat_id(settings, config.notify_chat_id)
                    if chat_id:
                        now = datetime.now(timezone.utc).isoformat()
                        for source in stale_sources:
                            text = (
                                f"OFFLINE alert\n"
                                f"source={source.source_name}\n"
                                f"type={source.source_type}\n"
                                f"last_seen={source.last_seen_at.isoformat()}\n"
                                f"server_time={now}"
                            )
                            await bot_client.send_message(chat_id, text)
                if bot_client and not is_quiet_hours(config, settings):
                    chat_id = _notification_chat_id(settings, config.notify_chat_id)
                    alerts = [alert for source in sources if source.is_online for alert in source_health_alerts(source)]
                    for text in _new_alerts(alerts, datetime.now(timezone.utc).timestamp()):
                        if chat_id:
                            await bot_client.send_message(chat_id, text)
            await sync_profile_weather(settings)
        except Exception:
            logger.exception("offline_check_loop error")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.heartbeat_check_interval_sec)
        except asyncio.TimeoutError:
            continue
