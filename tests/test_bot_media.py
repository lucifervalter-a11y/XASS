from __future__ import annotations

import unittest
import traceback
from unittest.mock import AsyncMock

import httpx

from app.bot_api import TelegramApiError, TelegramBotClient, TelegramFileResponse
from app.services.message_logging import _extract_chat, _extract_media_items, forwarded_from_label


class BotMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_error_never_exposes_bot_token(self) -> None:
        token = "123456789:super-secret-bot-token"

        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed URL {request.url}", request=request)

        client = TelegramBotClient(token)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        try:
            with self.assertRaises(TelegramApiError) as raised:
                await client.get_me()
            rendered = "".join(traceback.format_exception(raised.exception))
            self.assertNotIn(token, str(raised.exception))
            self.assertNotIn(token, rendered)
            self.assertIn("method=getMe", str(raised.exception))
        finally:
            await client.close()

    async def test_file_http_error_never_exposes_bot_token(self) -> None:
        token = "123456789:another-secret-token"

        def unavailable(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, request=request)

        client = TelegramBotClient(token)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
        try:
            with self.assertRaises(TelegramApiError) as raised:
                await client.open_file_stream("music/file.mp3")
            rendered = "".join(traceback.format_exception(raised.exception))
            self.assertNotIn(token, str(raised.exception))
            self.assertNotIn(token, rendered)
            self.assertIn("http=503", str(raised.exception))
        finally:
            await client.close()

    async def test_file_close_error_never_exposes_bot_token(self) -> None:
        token = "123456789:close-secret-token"
        request = httpx.Request("GET", f"https://api.telegram.org/file/bot{token}/music/file.mp3")

        class ClosingResponse:
            status_code = 200
            headers = httpx.Headers({"content-type": "audio/mpeg"})

            async def aclose(self) -> None:
                raise httpx.ReadError(f"close failed for {request.url}", request=request)

        response = TelegramFileResponse(ClosingResponse())  # type: ignore[arg-type]
        with self.assertRaises(TelegramApiError) as raised:
            await response.aclose()
        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn(token, rendered)
        self.assertIn("method=downloadFile", str(raised.exception))

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
