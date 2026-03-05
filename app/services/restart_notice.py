from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings


def _notice_path(settings: Settings) -> Path:
    return Path(settings.restart_notice_path)


def save_restart_notice(settings: Settings, *, chat_id: int, reason: str) -> None:
    path = _notice_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "chat_id": int(chat_id),
        "reason": str(reason).strip() or "перезапуск",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def pop_restart_notice(settings: Settings) -> dict[str, Any] | None:
    path = _notice_path(settings)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = None
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    if not isinstance(payload, dict):
        return None
    return payload


def get_restart_notice(settings: Settings) -> dict[str, Any] | None:
    path = _notice_path(settings)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def clear_restart_notice(settings: Settings) -> None:
    path = _notice_path(settings)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
