from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from app.config import Settings

# initData older than this many seconds is rejected.
MAX_AUTH_AGE_SEC = 24 * 60 * 60


@dataclass(slots=True)
class MiniAppUser:
    user_id: int
    first_name: str
    last_name: str
    username: str
    is_owner: bool


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def verify_init_data(init_data: str, bot_token: str) -> dict[str, str] | None:
    """Validate a Telegram WebApp initData string.

    Returns the parsed key/value pairs when the HMAC signature matches and the
    payload is fresh, otherwise None.
    """
    raw = (init_data or "").strip()
    if not raw or not bot_token:
        return None

    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{key}={pairs[key]}" for key in sorted(pairs.keys())
    )
    secret = _secret_key(bot_token)
    computed = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None

    auth_date_raw = pairs.get("auth_date", "")
    if auth_date_raw.isdigit():
        age = time.time() - int(auth_date_raw)
        if age > MAX_AUTH_AGE_SEC:
            return None

    return pairs


def parse_user(pairs: dict[str, str]) -> dict[str, Any] | None:
    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        data = json.loads(user_raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def authenticate(init_data: str, settings: Settings) -> MiniAppUser | None:
    """Full Mini App authentication: verify signature, extract user, check authz."""
    pairs = verify_init_data(init_data, settings.bot_token)
    if pairs is None:
        return None
    user = parse_user(pairs)
    if user is None:
        return None
    try:
        user_id = int(user.get("id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if user_id <= 0:
        return None
    if user_id not in settings.all_authorized_user_ids:
        return None
    return MiniAppUser(
        user_id=user_id,
        first_name=str(user.get("first_name") or ""),
        last_name=str(user.get("last_name") or ""),
        username=str(user.get("username") or ""),
        is_owner=bool(settings.owner_user_id and user_id == settings.owner_user_id),
    )
