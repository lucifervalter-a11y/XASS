from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_api import TelegramBotClient
from app.enums import MessageEventType, SaveMode
from app.models import AppConfig, MediaAsset, MessageLog, MessageRevision
from app.storage import build_media_path

CREATE_KEYS = ("message", "business_message", "channel_post")
EDIT_KEYS = ("edited_message", "edited_business_message", "edited_channel_post")


def _ts_to_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _extract_text(message: dict[str, Any]) -> str | None:
    return message.get("text") or message.get("caption")


def _extract_reply_to(message: dict[str, Any]) -> int | None:
    reply = message.get("reply_to_message") or {}
    return reply.get("message_id")


def _extract_chat(message: dict[str, Any]) -> tuple[int | None, str, str | None]:
    chat = message.get("chat") or {}
    return chat.get("id"), chat.get("type", "unknown"), chat.get("title") or chat.get("username")


def _extract_user(message: dict[str, Any]) -> tuple[int | None, str | None]:
    user = message.get("from") or {}
    username = user.get("username")
    if not username:
        parts = [user.get("first_name"), user.get("last_name")]
        username = " ".join(part for part in parts if part)
    return user.get("id"), username


def _extract_media_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    if message.get("photo"):
        photo = message["photo"][-1]
        items.append(
            {
                "media_type": "photo",
                "file_id": photo.get("file_id"),
                "file_unique_id": photo.get("file_unique_id"),
                "file_size": photo.get("file_size"),
                "mime_type": "image/jpeg",
            }
        )

    keys = ("video", "document", "voice", "video_note", "audio")
    for key in keys:
        media = message.get(key)
        if not media:
            continue
        items.append(
            {
                "media_type": key,
                "file_id": media.get("file_id"),
                "file_unique_id": media.get("file_unique_id"),
                "file_size": media.get("file_size"),
                "mime_type": media.get("mime_type"),
            }
        )

    return [item for item in items if item.get("file_id")]


def _is_allowed(save_mode: SaveMode, chat_type: str) -> bool:
    if save_mode == SaveMode.SAVE_OFF:
        return False
    if save_mode == SaveMode.SAVE_PRIVATE_ONLY and chat_type != "private":
        return False
    if save_mode == SaveMode.SAVE_GROUPS_ONLY and chat_type == "private":
        return False
    return True


def _direction(from_user_id: int | None, owner_user_id: int) -> str:
    if owner_user_id and from_user_id and from_user_id == owner_user_id:
        return "outgoing"
    return "incoming"


async def _get_message_log(
    session: AsyncSession,
    chat_id: int,
    telegram_message_id: int,
) -> MessageLog | None:
    stmt: Select[tuple[MessageLog]] = select(MessageLog).where(
        MessageLog.chat_id == chat_id,
        MessageLog.telegram_message_id == telegram_message_id,
    )
    return await session.scalar(stmt)


async def _next_revision_index(session: AsyncSession, message_id: int) -> int:
    stmt = select(func.count(MessageRevision.id)).where(MessageRevision.message_id == message_id)
    count = await session.scalar(stmt)
    return int(count or 0) + 1


async def _add_revision(
    session: AsyncSession,
    message_id: int,
    event_type: MessageEventType,
    text_content: str | None,
) -> None:
    revision = MessageRevision(
        message_id=message_id,
        revision_index=await _next_revision_index(session, message_id),
        event_type=event_type.value,
        text_content=text_content,
    )
    session.add(revision)


async def _store_media(
    session: AsyncSession,
    bot_client: TelegramBotClient | None,
    message: MessageLog,
    media_items: list[dict[str, Any]],
    save_mode: SaveMode,
) -> None:
    for media_item in media_items:
        existing = await session.scalar(
            select(MediaAsset).where(
                MediaAsset.message_id == message.id,
                MediaAsset.file_id == media_item["file_id"],
            )
        )
        if existing:
            continue

        asset = MediaAsset(
            message_id=message.id,
            media_type=media_item["media_type"],
            file_id=media_item["file_id"],
            file_unique_id=media_item.get("file_unique_id"),
            mime_type=media_item.get("mime_type"),
            file_size=media_item.get("file_size"),
        )
        session.add(asset)
        await session.flush()

        if save_mode != SaveMode.SAVE_FULL or not bot_client:
            continue

        try:
            tg_file = await bot_client.get_file(media_item["file_id"])
            file_path = tg_file.get("file_path")
            if not file_path:
                continue
            local_path = build_media_path(
                chat_id=message.chat_id,
                message_id=message.telegram_message_id,
                file_id=media_item["file_id"],
                source_file_path=file_path,
            )
            await bot_client.download_file(file_path, local_path)
            asset.telegram_file_path = file_path
            asset.local_path = str(local_path)
            if tg_file.get("file_size"):
                asset.file_size = tg_file["file_size"]
        except Exception as exc:
            asset.download_error = str(exc)[:250]


async def log_single_message(
    session: AsyncSession,
    *,
    message: dict[str, Any],
    event_type: MessageEventType,
    config: AppConfig,
    owner_user_id: int,
    bot_client: TelegramBotClient | None,
) -> None:
    chat_id, chat_type, chat_title = _extract_chat(message)
    if chat_id is None:
        return
    save_mode = SaveMode(config.save_mode)
    if not _is_allowed(save_mode, chat_type):
        return

    message_id = message.get("message_id")
    if message_id is None:
        return

    from_user_id, from_username = _extract_user(message)
    text_content = _extract_text(message)
    message_date = _ts_to_datetime(message.get("date"))
    edited_at = _ts_to_datetime(message.get("edit_date"))
    reply_to = _extract_reply_to(message)
    direction = _direction(from_user_id, owner_user_id)
    media_items = _extract_media_items(message)

    existing = await _get_message_log(session, chat_id=chat_id, telegram_message_id=message_id)
    if existing is None:
        existing = MessageLog(
            chat_id=chat_id,
            chat_type=chat_type,
            chat_title=chat_title,
            telegram_message_id=message_id,
            from_user_id=from_user_id,
            from_username=from_username,
            direction=direction,
            reply_to_message_id=reply_to,
            message_date=message_date,
            edited_at=edited_at,
            text_content=text_content,
            raw_event=message,
            tags="business" if "business_connection_id" in message else None,
        )
        session.add(existing)
        await session.flush()
        await _add_revision(session, existing.id, MessageEventType.CREATE, text_content)
    else:
        existing.chat_type = chat_type
        existing.chat_title = chat_title
        existing.from_user_id = from_user_id
        existing.from_username = from_username
        existing.direction = direction
        existing.reply_to_message_id = reply_to
        existing.raw_event = message
        existing.deleted = False
        existing.deleted_at = None
        if message_date:
            existing.message_date = message_date
        if edited_at:
            existing.edited_at = edited_at

        if event_type == MessageEventType.EDIT and text_content != existing.text_content:
            existing.text_content = text_content
            await _add_revision(session, existing.id, MessageEventType.EDIT, text_content)

    if media_items:
        await _store_media(session, bot_client, existing, media_items, save_mode)


async def mark_deleted_messages(session: AsyncSession, payload: dict[str, Any]) -> None:
    chat = payload.get("chat") or {}
    chat_id = chat.get("id") or payload.get("chat_id")
    chat_type = chat.get("type", "unknown")
    chat_title = chat.get("title") or chat.get("username")
    message_ids = payload.get("message_ids") or []
    if chat_id is None or not message_ids:
        return

    existing_items = list(
        await session.scalars(
            select(MessageLog).where(
                MessageLog.chat_id == chat_id,
                MessageLog.telegram_message_id.in_(message_ids),
            )
        )
    )
    existing_by_message_id = {item.telegram_message_id: item for item in existing_items}

    now = datetime.now(timezone.utc)
    for message_id in message_ids:
        message_log = existing_by_message_id.get(message_id)
        if message_log is None:
            # Keep a tombstone entry when delete event arrives before create event.
            tombstone = MessageLog(
                chat_id=chat_id,
                chat_type=chat_type,
                chat_title=chat_title,
                telegram_message_id=message_id,
                from_user_id=None,
                from_username=None,
                direction="incoming",
                reply_to_message_id=None,
                message_date=now,
                edited_at=None,
                text_content=None,
                tags="delete_tombstone",
                deleted=True,
                deleted_at=now,
                raw_event=payload,
            )
            session.add(tombstone)
            await session.flush()
            await _add_revision(session, tombstone.id, MessageEventType.DELETE, None)
            continue

        message_log.deleted = True
        message_log.deleted_at = now
        message_log.raw_event = payload
        await _add_revision(session, message_log.id, MessageEventType.DELETE, message_log.text_content)


async def handle_update_logging(
    session: AsyncSession,
    update: dict[str, Any],
    *,
    config: AppConfig,
    owner_user_id: int,
    bot_client: TelegramBotClient | None,
) -> None:
    for key in CREATE_KEYS:
        message = update.get(key)
        if message:
            await log_single_message(
                session,
                message=message,
                event_type=MessageEventType.CREATE,
                config=config,
                owner_user_id=owner_user_id,
                bot_client=bot_client,
            )

    for key in EDIT_KEYS:
        message = update.get(key)
        if message:
            await log_single_message(
                session,
                message=message,
                event_type=MessageEventType.EDIT,
                config=config,
                owner_user_id=owner_user_id,
                bot_client=bot_client,
            )

    deleted_payload = update.get("deleted_business_messages")
    if deleted_payload:
        save_mode = SaveMode(config.save_mode)
        payload_chat_type = ((deleted_payload.get("chat") or {}).get("type") or "unknown")
        if _is_allowed(save_mode, payload_chat_type):
            await mark_deleted_messages(session, deleted_payload)

    await session.commit()

