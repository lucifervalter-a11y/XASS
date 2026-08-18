from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from app.config import Settings


ALLOWED_ROOTS = {"desktop", "downloads", "documents", "xass_files"}
ASSET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
SCREENSHOT_TYPES = {"image/jpeg", "image/png", "image/webp"}
DEFAULT_TTL_SECONDS = 30 * 60


def _workspace_root(settings: Settings) -> Path:
    root = Path(settings.agent_workspace_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _metadata_path(root: Path, token: str) -> Path:
    if not ASSET_TOKEN_RE.fullmatch(token):
        raise ValueError("Invalid asset token")
    return root / f"{token}.json"


def _content_path(root: Path, token: str) -> Path:
    if not ASSET_TOKEN_RE.fullmatch(token):
        raise ValueError("Invalid asset token")
    return root / f"{token}.bin"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _safe_filename(value: object, fallback: str) -> str:
    name = Path(str(value or "").replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name).strip(" .")
    return (name or fallback)[:180]


def normalize_remote_location(root: object, relative_path: object = "") -> tuple[str, str]:
    root_name = str(root or "").strip().lower()
    if root_name not in ALLOWED_ROOTS:
        raise ValueError("Недоступная папка")
    raw = str(relative_path or "").replace("\\", "/").strip().strip("/")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." or "\x00" in part for part in parts):
        raise ValueError("Выход за пределы разрешённой папки запрещён")
    if any(":" in part for part in parts):
        raise ValueError("Абсолютные пути запрещены")
    return root_name, "/".join(parts)


def cleanup_expired_assets(settings: Settings, *, now: float | None = None) -> int:
    root = _workspace_root(settings)
    current = float(now or time.time())
    removed = 0
    for metadata_path in root.glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expired = float(metadata.get("expires_at") or 0) <= current
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            expired = True
        if not expired:
            continue
        token = metadata_path.stem
        _content_path(root, token).unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        removed += 1
    return removed


def _remove_previous_screenshot(root: Path, source_name: str) -> None:
    for metadata_path in root.glob("*.json"):
        try:
            item = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if item.get("kind") == "screenshot" and item.get("source_name") == source_name:
            _content_path(root, metadata_path.stem).unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)


def store_asset(
    settings: Settings,
    *,
    source_name: str,
    kind: str,
    filename: str,
    content_type: str,
    body: bytes,
    command_id: int = 0,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {"screenshot", "file_download", "file_upload"}:
        raise ValueError("Unsupported workspace asset")
    limit = settings.agent_screenshot_max_bytes if normalized_kind == "screenshot" else settings.agent_file_max_bytes
    if not body or len(body) > int(limit):
        raise ValueError(f"Файл пустой или превышает лимит {int(limit) // (1024 * 1024)} МБ")
    media_type = str(content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    if normalized_kind == "screenshot" and media_type not in SCREENSHOT_TYPES:
        raise ValueError("Screenshot должен быть JPEG, PNG или WebP")
    root = _workspace_root(settings)
    cleanup_expired_assets(settings)
    if normalized_kind == "screenshot":
        _remove_previous_screenshot(root, source_name)
    token = token_urlsafe(24)
    content_path = _content_path(root, token)
    temporary = content_path.with_suffix(".tmp")
    temporary.write_bytes(body)
    os.replace(temporary, content_path)
    created_at = time.time()
    safe_name = _safe_filename(filename, "screenshot.jpg" if normalized_kind == "screenshot" else "xass-file.bin")
    metadata = {
        "token": token,
        "source_name": str(source_name)[:128],
        "kind": normalized_kind,
        "filename": safe_name,
        "content_type": media_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "command_id": int(command_id or 0),
        "created_at": created_at,
        "expires_at": created_at + max(60, min(int(ttl_seconds), 24 * 60 * 60)),
    }
    _atomic_json(_metadata_path(root, token), metadata)
    return metadata


def load_asset(settings: Settings, token: str, *, source_name: str = "") -> tuple[dict[str, Any], Path] | None:
    root = _workspace_root(settings)
    try:
        metadata_path = _metadata_path(root, token)
        content_path = _content_path(root, token)
    except ValueError:
        return None
    if not metadata_path.is_file() or not content_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if float(metadata.get("expires_at") or 0) <= time.time():
            content_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            return None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if source_name and str(metadata.get("source_name") or "") != source_name:
        return None
    return metadata, content_path


def latest_screenshot(settings: Settings, source_name: str) -> dict[str, Any] | None:
    root = _workspace_root(settings)
    cleanup_expired_assets(settings)
    newest: dict[str, Any] | None = None
    for metadata_path in root.glob("*.json"):
        try:
            item = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if item.get("kind") != "screenshot" or item.get("source_name") != source_name:
            continue
        if newest is None or float(item.get("created_at") or 0) > float(newest.get("created_at") or 0):
            newest = item
    return newest


def delete_asset(settings: Settings, token: str) -> None:
    root = _workspace_root(settings)
    try:
        _content_path(root, token).unlink(missing_ok=True)
        _metadata_path(root, token).unlink(missing_ok=True)
    except ValueError:
        return
