from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.miniapp import MiniAppUser


MAX_LOGIN_AGE_SEC = 10 * 60
SESSION_AGE_SEC = 30 * 24 * 60 * 60
ACTION_PROOF_AGE_SEC = 2 * 60
COOKIE_NAME = "xass_pwa"


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _session_secret(settings: Settings) -> bytes:
    material = f"{settings.bot_token}|{settings.setup_api_key}|xass-pwa-v1"
    return hashlib.sha256(material.encode("utf-8")).digest()


def _session_generation(settings: Settings) -> int:
    path = Path(getattr(settings, "pwa_session_generation_path", "./data/pwa_session_generation"))
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip() or 0))
    except (OSError, ValueError):
        return 0


def rotate_session_generation(settings: Settings) -> int:
    path = Path(getattr(settings, "pwa_session_generation_path", "./data/pwa_session_generation"))
    generation = _session_generation(settings) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(str(generation), encoding="utf-8")
    temporary.replace(path)
    return generation


def verify_telegram_login(payload: dict[str, Any], bot_token: str) -> dict[str, str] | None:
    """Verify the payload produced by Telegram's website Login Widget."""
    if not bot_token:
        return None
    values = {str(key): str(value) for key, value in payload.items() if value is not None}
    received_hash = values.pop("hash", "")
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    auth_date = values.get("auth_date", "")
    if not auth_date.isdigit():
        return None
    age = time.time() - int(auth_date)
    if age < -60 or age > MAX_LOGIN_AGE_SEC:
        return None
    return values


def authenticate_telegram_login(payload: dict[str, Any], settings: Settings) -> MiniAppUser | None:
    values = verify_telegram_login(payload, settings.bot_token)
    if values is None:
        return None
    try:
        user_id = int(values.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if not settings.owner_user_id or user_id != settings.owner_user_id:
        return None
    return MiniAppUser(
        user_id=user_id,
        first_name=values.get("first_name", ""),
        last_name=values.get("last_name", ""),
        username=values.get("username", ""),
        is_owner=True,
    )


def issue_session(user: MiniAppUser, settings: Settings, *, now: int | None = None) -> str:
    issued_at = int(time.time() if now is None else now)
    payload = {
        "v": 1,
        "id": user.user_id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "username": user.username,
        "iat": issued_at,
        "exp": issued_at + SESSION_AGE_SEC,
        "gen": _session_generation(settings),
    }
    encoded = _b64_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_session_secret(settings), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64_encode(signature)}"


def authenticate_session(token: str, settings: Settings, *, now: int | None = None) -> MiniAppUser | None:
    try:
        encoded, received_signature = token.split(".", 1)
        expected = hmac.new(_session_secret(settings), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64_decode(received_signature)):
            return None
        payload = json.loads(_b64_decode(encoded))
        current = int(time.time() if now is None else now)
        user_id = int(payload.get("id") or 0)
        if int(payload.get("v") or 0) != 1 or current >= int(payload.get("exp") or 0):
            return None
        if int(payload.get("gen") or 0) != _session_generation(settings):
            return None
        if not settings.owner_user_id or user_id != settings.owner_user_id:
            return None
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
        return None
    return MiniAppUser(
        user_id=user_id,
        first_name=str(payload.get("first_name") or ""),
        last_name=str(payload.get("last_name") or ""),
        username=str(payload.get("username") or ""),
        is_owner=True,
    )


def issue_action_proof(user_id: int, purpose: str, settings: Settings, *, now: int | None = None) -> str:
    issued_at = int(time.time() if now is None else now)
    payload = {"v": 1, "id": int(user_id), "purpose": str(purpose), "iat": issued_at, "exp": issued_at + ACTION_PROOF_AGE_SEC}
    encoded = _b64_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_session_secret(settings), b"action." + encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64_encode(signature)}"


def verify_action_proof(token: str, user_id: int, purpose: str, settings: Settings, *, now: int | None = None) -> bool:
    try:
        encoded, received_signature = token.split(".", 1)
        expected = hmac.new(_session_secret(settings), b"action." + encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64_decode(received_signature)):
            return False
        payload = json.loads(_b64_decode(encoded))
        current = int(time.time() if now is None else now)
        return bool(
            int(payload.get("v") or 0) == 1
            and int(payload.get("id") or 0) == int(user_id)
            and str(payload.get("purpose") or "") == str(purpose)
            and current < int(payload.get("exp") or 0)
        )
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
        return False
