from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from app.bot_api import TelegramBotClient


class BotMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_file_id_uses_telegram_photo_method(self) -> None:
        client = TelegramBotClient("123:test")
        client._request = AsyncMock(return_value={"message_id": 1})
        try:
            await client.send_media_by_file_id(42, "photo-id", "photo", caption="deleted")
        finally:
            await client.close()

        client._request.assert_awaited_once_with(
            "sendPhoto",
            payload={"chat_id": 42, "photo": "photo-id", "caption": "deleted"},
        )

    async def test_video_note_omits_unsupported_caption(self) -> None:
        client = TelegramBotClient("123:test")
        client._request = AsyncMock(return_value={"message_id": 1})
        try:
            await client.send_media_by_file_id(42, "round-id", "video_note", caption="ignored")
        finally:
            await client.close()

        client._request.assert_awaited_once_with(
            "sendVideoNote",
            payload={"chat_id": 42, "video_note": "round-id"},
        )


if __name__ == "__main__":
    unittest.main()
