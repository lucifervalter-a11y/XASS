import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MessageLog


async def export_messages_csv(
    session: AsyncSession,
    export_root: str,
    *,
    limit: int = 5000,
) -> Path:
    result = await session.scalars(select(MessageLog).order_by(MessageLog.id.desc()).limit(limit))
    rows = list(result)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = Path(export_root) / f"messages_export_{ts}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "id",
                "chat_id",
                "chat_type",
                "message_id",
                "from_user_id",
                "direction",
                "message_date",
                "edited_at",
                "deleted",
                "text_content",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.id,
                    row.chat_id,
                    row.chat_type,
                    row.telegram_message_id,
                    row.from_user_id,
                    row.direction,
                    row.message_date.isoformat() if row.message_date else "",
                    row.edited_at.isoformat() if row.edited_at else "",
                    row.deleted,
                    row.text_content or "",
                ]
            )
    return path

