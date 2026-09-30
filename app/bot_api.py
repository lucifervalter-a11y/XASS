import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


class TelegramApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        status_code: int | None = None,
        error_code: int | None = None,
        description: str | None = None,
    ):
        super().__init__(message)
        self.method = method
        self.status_code = status_code
        self.error_code = error_code
        self.description = description

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.method:
            parts.append(f"method={self.method}")
        if self.status_code is not None:
            parts.append(f"http={self.status_code}")
        if self.error_code is not None:
            parts.append(f"tg={self.error_code}")
        if self.description:
            parts.append(f"desc={self.description}")
        return " | ".join(parts)


def _safe_http_error(message: str, method: str, exc: httpx.HTTPError) -> TelegramApiError:
    """Convert token-bearing httpx errors into a log-safe public exception."""

    response = getattr(exc, "response", None)
    status_code = response.status_code if isinstance(response, httpx.Response) else None
    return TelegramApiError(message, method=method, status_code=status_code)


class TelegramFileResponse:
    """A streamed Telegram response which never rethrows a token-bearing URL."""

    def __init__(self, response: httpx.Response):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers

    async def aiter_bytes(self, chunk_size: int | None = None):
        try:
            async for chunk in self._response.aiter_bytes(chunk_size):
                yield chunk
        except httpx.HTTPError as exc:
            raise _safe_http_error("Telegram file download failed", "downloadFile", exc) from None

    async def aclose(self) -> None:
        try:
            await self._response.aclose()
        except httpx.HTTPError as exc:
            raise _safe_http_error("Telegram file close failed", "downloadFile", exc) from None


class TelegramBotClient:
    def __init__(self, token: str, timeout_sec: int = 20):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_url = f"https://api.telegram.org/file/bot{token}"
        self.client = httpx.AsyncClient(timeout=timeout_sec)

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        *,
        payload: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        if not self.token:
            raise TelegramApiError("BOT_TOKEN is empty")

        url = f"{self.base_url}/{method}"
        try:
            response = await self.client.post(url, json=payload, data=data, files=files, timeout=timeout)
        except httpx.HTTPError as exc:
            raise _safe_http_error("Telegram API request failed", method, exc) from None
        body: dict[str, Any]
        try:
            body = response.json()
        except Exception:
            body = {}

        if response.status_code >= 400:
            raise TelegramApiError(
                "Telegram API HTTP error",
                method=method,
                status_code=response.status_code,
                error_code=body.get("error_code"),
                description=body.get("description"),
            )

        if not body.get("ok"):
            raise TelegramApiError(
                "Telegram API returned ok=false",
                method=method,
                status_code=response.status_code,
                error_code=body.get("error_code"),
                description=body.get("description"),
            )
        return body["result"]

    async def set_webhook(self, webhook_url: str, secret_token: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"url": webhook_url, "allowed_updates": ["message", "edited_message", "callback_query", "business_message", "edited_business_message", "deleted_business_messages"]}
        if secret_token:
            payload["secret_token"] = secret_token
        return await self._request("setWebhook", payload=payload)

    async def delete_webhook(self, drop_pending_updates: bool = False) -> dict[str, Any]:
        payload = {"drop_pending_updates": drop_pending_updates}
        return await self._request("deleteWebhook", payload=payload)

    async def get_me(self) -> dict[str, Any]:
        result = await self._request("getMe")
        return result if isinstance(result, dict) else {}

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 25,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates:
            payload["allowed_updates"] = allowed_updates
        # Telegram long-polling timeout should be lower than transport timeout.
        transport_timeout = httpx.Timeout(timeout=timeout + 15.0, connect=10.0)
        result = await self._request("getUpdates", payload=payload, timeout=transport_timeout)
        return result if isinstance(result, list) else []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        business_connection_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
            "disable_notification": disable_notification,
        }
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self._request("sendMessage", payload=payload)

    async def send_photo(
        self,
        chat_id: int,
        photo: str,
        *,
        business_connection_id: str | None = None,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo,
            "disable_notification": disable_notification,
        }
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        if caption:
            payload["caption"] = caption
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self._request("sendPhoto", payload=payload)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        business_connection_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self._request("editMessageText", payload=payload)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        return await self._request("answerCallbackQuery", payload=payload)

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return await self._request("getFile", payload={"file_id": file_id})

    async def get_user_profile_photos(self, user_id: int, *, limit: int = 1) -> dict[str, Any]:
        result = await self._request("getUserProfilePhotos", payload={"user_id": user_id, "offset": 0, "limit": limit})
        return result if isinstance(result, dict) else {}

    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        result = await self._request("getChat", payload={"chat_id": chat_id})
        return result if isinstance(result, dict) else {}

    async def download_file(self, file_path: str, destination: Path) -> None:
        response = await self.open_file_stream(file_path)
        try:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
        finally:
            await response.aclose()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)

    async def open_file_stream(
        self,
        file_path: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        headers: dict[str, str] | None = None,
    ) -> TelegramFileResponse:
        """Open one Telegram file while keeping the bot token out of errors."""

        cleaned = str(file_path or "").strip().lstrip("/")
        if not cleaned or len(cleaned) > 1024 or "\\" in cleaned or any(ord(char) < 32 for char in cleaned):
            raise TelegramApiError("Telegram returned an invalid file path", method="downloadFile")
        url = f"{self.file_url}/{quote(cleaned, safe='/')}"
        response: httpx.Response | None = None
        try:
            request = self.client.build_request("GET", url, headers=headers, timeout=timeout)
            response = await self.client.send(request, stream=True)
            response.raise_for_status()
            return TelegramFileResponse(response)
        except httpx.HTTPError as exc:
            if response is not None:
                try:
                    await response.aclose()
                except httpx.HTTPError:
                    # Preserve the original, already-sanitized download failure;
                    # closing a response must never surface its token-bearing URL.
                    pass
            raise _safe_http_error("Telegram file download failed", "downloadFile", exc) from None

    async def download_file_bytes(
        self,
        file_path: str,
        *,
        max_bytes: int,
        timeout: float | httpx.Timeout | None = None,
    ) -> tuple[bytes, str]:
        response = await self.open_file_stream(file_path, timeout=timeout, headers={"Accept-Encoding": "identity"})
        try:
            content = bytearray()
            async for chunk in response.aiter_bytes(64 * 1024):
                if len(chunk) > max_bytes - len(content):
                    raise TelegramApiError("Telegram file exceeds the allowed size", method="downloadFile")
                content.extend(chunk)
            return bytes(content), str(response.headers.get("content-type") or "application/octet-stream")
        finally:
            await response.aclose()

    async def send_document(self, chat_id: int, path: Path, caption: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as handle:
            files = {"document": (path.name, handle, mime_type)}
            return await self._request("sendDocument", data=data, files=files)

    async def send_document_by_file_id(self, chat_id: int, file_id: str, caption: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "document": file_id}
        if caption:
            payload["caption"] = caption
        return await self._request("sendDocument", payload=payload)

    async def send_media_by_file_id(
        self,
        chat_id: int,
        file_id: str,
        media_type: str,
        caption: str | None = None,
    ) -> dict[str, Any]:
        telegram_type = (media_type or "document").strip().lower()
        method_and_field = {
            "photo": ("sendPhoto", "photo"),
            "video": ("sendVideo", "video"),
            "voice": ("sendVoice", "voice"),
            "video_note": ("sendVideoNote", "video_note"),
            "audio": ("sendAudio", "audio"),
            "document": ("sendDocument", "document"),
        }
        method, field = method_and_field.get(telegram_type, method_and_field["document"])
        payload: dict[str, Any] = {"chat_id": chat_id, field: file_id}
        if caption and telegram_type != "video_note":
            payload["caption"] = caption
        return await self._request(method, payload=payload)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        result = await self._request("deleteMessage", payload={"chat_id": chat_id, "message_id": message_id})
        return bool(result)

    async def delete_business_messages(
        self,
        *,
        business_connection_id: str,
        message_ids: list[int],
    ) -> bool:
        payload: dict[str, Any] = {
            "business_connection_id": business_connection_id,
            "message_ids": message_ids,
        }
        result = await self._request("deleteBusinessMessages", payload=payload)
        return bool(result)

    async def copy_message(
        self,
        *,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        business_connection_id: str | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        }
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        if caption:
            payload["caption"] = caption
        return await self._request("copyMessage", payload=payload)

    async def set_chat_menu_button(
        self,
        *,
        chat_id: int | None = None,
        menu_button: dict[str, Any] | None = None,
    ) -> bool:
        payload: dict[str, Any] = {}
        if chat_id is not None:
            payload["chat_id"] = chat_id
        if menu_button is not None:
            payload["menu_button"] = menu_button
        result = await self._request("setChatMenuButton", payload=payload)
        return bool(result)

    async def set_my_commands(
        self,
        commands: list[dict[str, Any]],
        *,
        scope: dict[str, Any] | None = None,
        language_code: str | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"commands": commands}
        if scope is not None:
            payload["scope"] = scope
        if language_code is not None:
            payload["language_code"] = language_code
        result = await self._request("setMyCommands", payload=payload)
        return bool(result)
