from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any

from app.config import Settings

VK_BIND_TTL_SEC = 15 * 60


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _vk_bind_secret(settings: Settings) -> bytes:
    material = f"{settings.bot_token}|{settings.setup_api_key}|xass-vk-bind-v1"
    return hashlib.sha256(material.encode("utf-8")).digest()


def issue_vk_bind_token(
    settings: Settings,
    *,
    chat_id: int | None = None,
    ttl_seconds: int = VK_BIND_TTL_SEC,
    now: int | None = None,
) -> str:
    """Short-lived HMAC token for VK OAuth redirect_uri (never embed SETUP_API_KEY)."""
    issued_at = int(time.time() if now is None else now)
    ttl = max(60, min(int(ttl_seconds), 30 * 60))
    payload: dict[str, Any] = {
        "v": 1,
        "purpose": "vk_bind",
        "iat": issued_at,
        "exp": issued_at + ttl,
    }
    if chat_id is not None and int(chat_id) != 0:
        payload["chat_id"] = int(chat_id)
    encoded = _b64_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_vk_bind_secret(settings), b"vk_bind." + encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64_encode(signature)}"


def verify_vk_bind_token(
    token: str,
    settings: Settings,
    *,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Validate a VK bind token. Returns payload dict or None."""
    raw = (token or "").strip()
    if not raw or "." not in raw:
        return None
    try:
        encoded, received_signature = raw.split(".", 1)
        expected = hmac.new(
            _vk_bind_secret(settings),
            b"vk_bind." + encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, _b64_decode(received_signature)):
            return None
        payload = json.loads(_b64_decode(encoded))
        current = int(time.time() if now is None else now)
        if (
            int(payload.get("v") or 0) != 1
            or str(payload.get("purpose") or "") != "vk_bind"
            or current >= int(payload.get("exp") or 0)
        ):
            return None
        return payload if isinstance(payload, dict) else None
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
        return None
