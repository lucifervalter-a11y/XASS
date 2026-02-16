import asyncio
import logging

import httpx

from app.bot_api import TelegramApiError, TelegramBotClient
from app.config import Settings
from app.db import SessionLocal
from app.telegram_handler import TelegramUpdateHandler

logger = logging.getLogger(__name__)

ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]


async def telegram_polling_loop(
    settings: Settings,
    bot_client: TelegramBotClient | None,
    update_handler: TelegramUpdateHandler,
    stop_event: asyncio.Event,
) -> None:
    if not bot_client:
        logger.warning("Polling requested but BOT_TOKEN is empty")
        return

    offset: int | None = None
    try:
        await bot_client.delete_webhook(drop_pending_updates=settings.polling_drop_pending_updates)
        logger.info("Polling mode enabled, webhook disabled")
    except Exception:
        logger.exception("Failed to disable webhook before polling")

    while not stop_event.is_set():
        try:
            updates = await bot_client.get_updates(
                offset=offset,
                timeout=settings.polling_request_timeout_sec,
                allowed_updates=ALLOWED_UPDATES,
            )
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                async with SessionLocal() as session:
                    await update_handler.handle_update(session, update)
        except TelegramApiError as exc:
            if exc.status_code == 409:
                logger.warning(
                    "Polling conflict (409): webhook or another getUpdates consumer is active. "
                    "Trying to reset webhook and retry."
                )
                try:
                    await bot_client.delete_webhook(drop_pending_updates=False)
                except Exception:
                    logger.warning("Unable to reset webhook after 409 conflict")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=settings.polling_retry_delay_sec)
                except asyncio.TimeoutError:
                    continue
                continue
            logger.warning("telegram_polling_loop Telegram API error: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.polling_retry_delay_sec)
            except asyncio.TimeoutError:
                continue
        except httpx.ReadTimeout:
            # Long-poll request can timeout on network edges; retry without noisy traceback.
            continue
        except Exception:
            logger.exception("telegram_polling_loop error")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.polling_retry_delay_sec)
            except asyncio.TimeoutError:
                continue
