from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from app.bot_api import TelegramBotClient
from app.services.message_logging import _extract_chat, _extract_media_items, forwarded_from_label


class BotMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_chat_uses_human_name_before_username(self) -> None:
        self.assertEqual(
            _extract_chat(
                {
                    "chat": {
                        "id": 7,
                        "type": "private",
                        "first_name": "Анна",
                        "last_name": "Иванова",
                        "username": "anna",
                    }
                }
            ),
            (7, "private", "Анна Иванова"),
        )

    async def test_sticker_and_forward_origin_are_preserved_for_archive(self) -> None:
        message = {
            "sticker": {"file_id": "sticker-id", "file_unique_id": "unique", "is_animated": True},
            "forward_origin": {"type": "hidden_user", "sender_user_name": "Original author"},
        }
        media = _extract_media_items(message)
        self.assertEqual(media[0]["media_type"], "sticker")
        self.assertEqual(media[0]["mime_type"], "application/x-tgsticker")
        self.assertEqual(forwarded_from_label(message), "Original author")

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
