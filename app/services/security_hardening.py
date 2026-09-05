from __future__ import annotations

import hmac
from typing import Any

from fastapi import HTTPException, status

from app.config import Settings
from app.services.vk_bind import verify_vk_bind_token


def verify_api_key_secure(header_value: str | None, expected: str, reason: str) -> None:
    if not expected:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{reason} key is not configured")
    provided = (header_value or "").strip().encode("utf-8")
    wanted = expected.encode("utf-8")
    if len(provided) != len(wanted) or not hmac.compare_digest(provided, wanted):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid {reason} key")


def verify_secret_equal(provided: str, expected: str) -> bool:
    a = (provided or "").encode("utf-8")
    b = (expected or "").encode("utf-8")
    return len(a) == len(b) and hmac.compare_digest(a, b)


def validate_webhook_request(secret_path: str, header_secret: str | None, settings: Settings) -> None:
    expected_path = (settings.telegram_webhook_path or "").strip()
    path_ok = bool(expected_path) and "change-me" not in expected_path
    if path_ok:
        path_ok = verify_secret_equal(secret_path, expected_path)
    if not path_ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook path not found")

    expected_secret = (settings.telegram_secret_token or "").strip()
    if not expected_secret or "change-me" in expected_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TELEGRAM_SECRET_TOKEN is not configured",
        )
    if not verify_secret_equal((header_secret or "").strip(), expected_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram secret token")


def require_vk_bind_secret(secret: str, settings: Settings) -> dict[str, Any]:
    bind = verify_vk_bind_token((secret or "").strip(), settings)
    if bind is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired VK bind secret")
    return bind
