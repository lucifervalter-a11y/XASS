from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4


def _paths(cache_dir: Path, chat_id: int) -> tuple[Path, Path, Path]:
    stem = str(int(chat_id))
    return cache_dir / f"{stem}.bin", cache_dir / f"{stem}.json", cache_dir / f"{stem}.miss"


def load_cached_avatar(cache_dir: Path, chat_id: int, *, ttl_seconds: int) -> tuple[bytes, str] | None:
    body_path, meta_path, _ = _paths(cache_dir, chat_id)
    try:
        age = time.time() - min(body_path.stat().st_mtime, meta_path.stat().st_mtime)
        if age > max(1, int(ttl_seconds)):
            return None
        body = body_path.read_bytes()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        media_type = str(meta.get("media_type") or "image/jpeg")
        if not body or not media_type.startswith("image/"):
            return None
        return body, media_type
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def missing_cache_is_fresh(cache_dir: Path, chat_id: int, *, ttl_seconds: int) -> bool:
    _, _, missing_path = _paths(cache_dir, chat_id)
    try:
        return time.time() - missing_path.stat().st_mtime <= max(1, int(ttl_seconds))
    except OSError:
        return False


def store_cached_avatar(cache_dir: Path, chat_id: int, body: bytes, media_type: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    body_path, meta_path, missing_path = _paths(cache_dir, chat_id)
    suffix = uuid4().hex
    body_tmp = body_path.with_name(f"{body_path.name}.{suffix}.tmp")
    meta_tmp = meta_path.with_name(f"{meta_path.name}.{suffix}.tmp")
    body_tmp.write_bytes(body)
    meta_tmp.write_text(json.dumps({"media_type": media_type}, ensure_ascii=False), encoding="utf-8")
    os.replace(body_tmp, body_path)
    os.replace(meta_tmp, meta_path)
    missing_path.unlink(missing_ok=True)


def mark_avatar_missing(cache_dir: Path, chat_id: int) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _, _, missing_path = _paths(cache_dir, chat_id)
    missing_path.touch()
