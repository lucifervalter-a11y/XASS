from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path


_BOT_USERNAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def normalize_bot_username(value: object) -> str:
    username = str(value or "").strip().lstrip("@").lower()
    if _BOT_USERNAME_RE.fullmatch(username) is None:
        return ""
    return username


def load_cached_bot_username(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return normalize_bot_username(payload.get("username"))


def save_cached_bot_username(path: Path, username: object) -> str:
    normalized = normalize_bot_username(username)
    if not normalized:
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "username": normalized,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return normalized
