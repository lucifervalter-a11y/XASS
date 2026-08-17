from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentArchiveTarget, HeartbeatSource, MediaAsset, MessageLog, MessageRevision
from app.services.message_logging import forwarded_from_label


async def archive_target_map(session: AsyncSession) -> dict[str, AgentArchiveTarget]:
    rows = list(await session.scalars(select(AgentArchiveTarget)))
    return {row.source_name: row for row in rows}


async def set_archive_target(
    session: AsyncSession,
    *,
    source_name: str,
    enabled: bool,
    actor_user_id: int,
) -> AgentArchiveTarget:
    source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == source_name))
    if source is None:
        raise ValueError("Агент не найден")
    target = await session.scalar(select(AgentArchiveTarget).where(AgentArchiveTarget.source_name == source_name))
    if target is None:
        target = AgentArchiveTarget(source_name=source_name)
        session.add(target)
    target.enabled = bool(enabled)
    target.updated_by_user_id = actor_user_id
    await session.commit()
    await session.refresh(target)
    return target


async def is_archive_target(session: AsyncSession, source_name: str) -> bool:
    target = await session.scalar(
        select(AgentArchiveTarget).where(
            AgentArchiveTarget.source_name == source_name,
            AgentArchiveTarget.enabled.is_(True),
        )
    )
    return target is not None


def _media_entry(asset: MediaAsset, message: MessageLog) -> dict[str, Any]:
    return {
        "id": asset.id,
        "type": asset.media_type,
        "mime_type": asset.mime_type or "application/octet-stream",
        "file_size": asset.file_size,
        "file_unique_id": asset.file_unique_id or "",
        "file_name": _asset_file_name(asset, message),
        "download_path": f"/agent/archive/media/{asset.id}",
    }


def _asset_file_name(asset: MediaAsset, message: MessageLog) -> str:
    raw_event = message.raw_event if isinstance(message.raw_event, dict) else {}
    media = raw_event.get(asset.media_type) if isinstance(raw_event, dict) else None
    name = ""
    if isinstance(media, dict):
        name = str(media.get("file_name") or "").strip()
    if name:
        return name[:220]
    extension = ""
    mime = (asset.mime_type or "").lower()
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "video/webm": ".webm",
        "image/webp": ".webp",
        "application/x-tgsticker": ".tgs",
        "application/pdf": ".pdf",
    }.get(mime, "")
    return f"{asset.media_type}-{asset.id}{extension}"


async def archive_events_after(
    session: AsyncSession,
    *,
    cursor: int,
    limit: int = 100,
) -> list[dict[str, Any]]:
    revisions = list(
        await session.scalars(
            select(MessageRevision)
            .where(MessageRevision.id > max(0, int(cursor)))
            .order_by(MessageRevision.id.asc())
            .limit(max(1, min(int(limit), 200)))
        )
    )
    result: list[dict[str, Any]] = []
    for revision in revisions:
        message = await session.get(MessageLog, revision.message_id)
        if message is None:
            continue
        assets = list(
            await session.scalars(
                select(MediaAsset)
                .where(
                    MediaAsset.message_id == message.id,
                    MediaAsset.archive_allowed.is_(True),
                )
                .order_by(MediaAsset.id.asc())
            )
        )
        result.append(
            {
                "event_id": revision.id,
                "event": revision.event_type,
                "message_id": message.id,
                "telegram_message_id": message.telegram_message_id,
                "chat_id": message.chat_id,
                "chat_type": message.chat_type,
                "chat_title": message.chat_title or "",
                "from_user_id": message.from_user_id,
                "from_username": message.from_username or "",
                "direction": message.direction,
                "reply_to_message_id": message.reply_to_message_id,
                "forwarded_from": forwarded_from_label(message.raw_event if isinstance(message.raw_event, dict) else {}),
                "text": revision.text_content or message.text_content or "",
                "deleted": bool(message.deleted),
                "message_date": message.message_date.isoformat() if message.message_date else None,
                "event_date": revision.created_at.isoformat() if revision.created_at else None,
                "media": [_media_entry(asset, message) for asset in assets],
            }
        )
    return result


async def archive_summary(session: AsyncSession) -> dict[str, Any]:
    messages = int(await session.scalar(select(func.count(MessageLog.id))) or 0)
    deleted = int(await session.scalar(select(func.count(MessageLog.id)).where(MessageLog.deleted.is_(True))) or 0)
    media = int(await session.scalar(select(func.count(MediaAsset.id)).where(MediaAsset.archive_allowed.is_(True))) or 0)
    targets = await archive_target_map(session)
    return {
        "messages": messages,
        "deleted": deleted,
        "media": media,
        "targets": sum(1 for item in targets.values() if item.enabled),
        "mode": "agent",
    }
