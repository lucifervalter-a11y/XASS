import asyncio
import logging
from datetime import datetime

from app.bot_api import TelegramBotClient
from app.config import Settings
from app.db import SessionLocal
from app.services.app_config import get_or_create_app_config
from app.services.heartbeat import is_quiet_hours, mark_offline_sources
from app.services.profile_runtime import sync_profile_now_playing_from_heartbeat, sync_profile_weather

logger = logging.getLogger(__name__)


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
                await sync_profile_now_playing_from_heartbeat(session, settings, config.heartbeat_timeout_minutes)
                if stale_sources and bot_client and not is_quiet_hours(config, settings):
                    chat_id = _notification_chat_id(settings, config.notify_chat_id)
                    if chat_id:
                        now = datetime.utcnow().isoformat()
                        for source in stale_sources:
                            text = (
                                f"OFFLINE alert\n"
                                f"source={source.source_name}\n"
                                f"type={source.source_type}\n"
                                f"last_seen={source.last_seen_at.isoformat()}\n"
                                f"server_time={now}"
                            )
                            await bot_client.send_message(chat_id, text)
            await sync_profile_weather(settings)
        except Exception:
            logger.exception("offline_check_loop error")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.heartbeat_check_interval_sec)
        except asyncio.TimeoutError:
            continue
