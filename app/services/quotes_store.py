from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_QUOTES = [
    "Делаем просто, надежно и без магии.",
    "Меньше слов — больше дела.",
    "Хороший код объясняет себя сам.",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return secrets.token_hex(6)


def _normalize_quote(raw: Any, *, fallback_id: str) -> dict[str, Any] | None:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return {"id": fallback_id, "text": text, "created_at": _now_iso()}
    if isinstance(raw, dict):
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        quote_id = str(raw.get("id") or "").strip() or fallback_id
        created_at = str(raw.get("created_at") or "").strip() or _now_iso()
        return {"id": quote_id, "text": text, "created_at": created_at}
    return None


def normalize_quotes(raw: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    if isinstance(raw, dict):
        raw = raw.get("quotes")
    if isinstance(raw, list):
        for index, item in enumerate(raw, start=1):
            normalized = _normalize_quote(item, fallback_id=_new_id())
            if normalized is None:
                continue
            while normalized["id"] in seen_ids:
                normalized["id"] = _new_id()
            seen_ids.add(normalized["id"])
            items.append(normalized)
    return items


def load_quotes(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return [
            {"id": _new_id(), "text": text, "created_at": _now_iso()}
            for text in DEFAULT_QUOTES
        ]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return normalize_quotes(raw)


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def save_quotes(path: Path, quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = normalize_quotes(quotes)
    _atomic_write(path, {"quotes": normalized, "updated_at": _now_iso()})
    return normalized


def ensure_quotes_exists(path: Path) -> list[dict[str, Any]]:
    quotes = load_quotes(path)
    if not path.exists():
        save_quotes(path, quotes)
    return quotes


def add_quote(path: Path, text: str) -> dict[str, Any] | None:
    clean = (text or "").strip()
    if not clean:
        return None
    quotes = load_quotes(path)
    entry = {"id": _new_id(), "text": clean, "created_at": _now_iso()}
    quotes.append(entry)
    save_quotes(path, quotes)
    return entry


def delete_quote(path: Path, quote_id: str) -> bool:
    target = (quote_id or "").strip()
    if not target:
        return False
    quotes = load_quotes(path)
    remaining = [item for item in quotes if str(item.get("id")) != target]
    if len(remaining) == len(quotes):
        return False
    save_quotes(path, remaining)
    return True
