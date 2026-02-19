import mimetypes
from pathlib import Path
from typing import Any

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
        response = await self.client.post(url, json=payload, data=data, files=files, timeout=timeout)
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

    async def download_file(self, file_path: str, destination: Path) -> None:
        url = f"{self.file_url}/{file_path}"
        response = await self.client.get(url)
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)

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

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        result = await self._request("deleteMessage", payload={"chat_id": chat_id, "message_id": message_id})
        return bool(result)

    async def delete_business_messages(
        self,
        *,
        business_connection_id: str,
        chat_id: int,
        message_ids: list[int],
    ) -> bool:
        payload: dict[str, Any] = {
            "business_connection_id": business_connection_id,
            "chat_id": chat_id,
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
