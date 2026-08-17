from __future__ import annotations

import json
import hashlib
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

try:
    from client_update import DATA_ROOT
except ImportError:  # Package import in tests and developer tooling.
    from pc_client.client_update import DATA_ROOT


DEFAULT_ARCHIVE_ROOT = DATA_ROOT / "Archive"
STATE_FILE = ".xass-archive-state.json"
DB_FILE = "xass-archive.sqlite3"


def archive_root(config: dict[str, Any]) -> Path:
    raw = str(config.get("archive_folder") or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_ARCHIVE_ROOT


def _safe_part(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" .")
    return text[:100] or fallback


def _state_path(config: dict[str, Any]) -> Path:
    return archive_root(config) / STATE_FILE


def _read_state(config: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(_state_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def archive_cursor(config: dict[str, Any]) -> int:
    try:
        payload = _read_state(config)
        return max(0, int(payload.get("cursor") or 0))
    except (ValueError, TypeError):
        return 0


def archive_status(config: dict[str, Any]) -> dict[str, Any]:
    root = archive_root(config)
    cursor = archive_cursor(config)
    state = _read_state(config)
    try:
        database_size = (root / DB_FILE).stat().st_size
    except OSError:
        database_size = 0
    disk_root = root if root.exists() else root.parent
    try:
        free_bytes = shutil.disk_usage(disk_root).free
    except OSError:
        free_bytes = 0
    media_root = root / "media"
    media_bytes = 0
    media_files = 0
    if media_root.is_dir():
        for path in media_root.rglob("*"):
            try:
                if path.is_file():
                    media_bytes += path.stat().st_size
                    media_files += 1
            except OSError:
                continue
    return {
        "folder": str(root),
        "cursor": cursor,
        "database_size": database_size,
        "media_size": media_bytes,
        "media_files": media_files,
        "free_bytes": free_bytes,
        "enabled": bool(config.get("archive_enabled", False)),
        "last_sync_at": str(state.get("last_sync_at") or ""),
        "last_error": str(state.get("last_error") or "")[:500],
        "errors": max(0, int(state.get("errors") or 0)),
        "pending_retry": bool(state.get("pending_retry")),
    }


def _connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / DB_FILE, timeout=15)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            telegram_message_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            chat_type TEXT,
            chat_title TEXT,
            from_user_id INTEGER,
            from_username TEXT,
            direction TEXT,
            reply_to_message_id INTEGER,
            forwarded_from TEXT,
            text_content TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            message_date TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_archive_chat ON messages(chat_id, id);
        CREATE TABLE IF NOT EXISTS revisions (
            event_id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            text_content TEXT,
            event_date TEXT
        );
        CREATE TABLE IF NOT EXISTS media (
            asset_id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL,
            media_type TEXT,
            mime_type TEXT,
            file_size INTEGER,
            local_path TEXT,
            checksum TEXT,
            file_unique_id TEXT,
            saved_at TEXT,
            saved INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        """
    )
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")}
    if "forwarded_from" not in columns:
        connection.execute("ALTER TABLE messages ADD COLUMN forwarded_from TEXT")
    media_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(media)")}
    if "checksum" not in media_columns:
        connection.execute("ALTER TABLE media ADD COLUMN checksum TEXT")
    if "file_unique_id" not in media_columns:
        connection.execute("ALTER TABLE media ADD COLUMN file_unique_id TEXT")
    if "saved_at" not in media_columns:
        connection.execute("ALTER TABLE media ADD COLUMN saved_at TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_archive_media_checksum ON media(checksum)")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_archive_media_unique ON media(file_unique_id)")
    return connection


def _write_state(
    root: Path,
    cursor: int,
    *,
    errors: int = 0,
    pending_retry: bool = False,
    last_error: str = "",
) -> None:
    target = root / STATE_FILE
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "cursor": int(cursor),
                "last_sync_at": datetime.now(timezone.utc).isoformat(),
                "errors": max(0, int(errors)),
                "pending_retry": bool(pending_retry),
                "last_error": str(last_error)[:500],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(target)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_media(
    client: httpx.Client,
    *,
    server_url: str,
    headers: dict[str, str],
    root: Path,
    event: dict[str, Any],
    media: dict[str, Any],
) -> tuple[str, str, str]:
    chat_dir = root / "media" / _safe_part(event.get("chat_id"), "chat")
    chat_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_part(media.get("file_name"), f"media-{media.get('id') or 'file'}")
    target = chat_dir / f"{event.get('telegram_message_id') or event.get('message_id')}_{filename}"
    if target.is_file() and target.stat().st_size > 0:
        return str(target), "", _sha256_file(target)
    url = f"{server_url.rstrip('/')}{str(media.get('download_path') or '')}"
    temporary = target.with_suffix(target.suffix + ".download")
    try:
        with client.stream("GET", url, headers=headers, timeout=60) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 256):
                    handle.write(chunk)
        expected_size = int(media.get("file_size") or 0)
        actual_size = temporary.stat().st_size
        if expected_size > 0 and actual_size != expected_size:
            raise ValueError(f"media size mismatch: expected {expected_size}, got {actual_size}")
        checksum = _sha256_file(temporary)
        expected_checksum = str(media.get("sha256") or "").strip().lower()
        if expected_checksum and checksum != expected_checksum:
            raise ValueError("media checksum mismatch")
        temporary.replace(target)
        return str(target), "", checksum
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return "", str(exc)[:500], ""


def cleanup_archive(config: dict[str, Any], *, force: bool = False) -> dict[str, int]:
    """Apply local age/size policy without touching message text or the central server."""
    root = archive_root(config)
    path = root / DB_FILE
    if not path.is_file():
        return {"removed_files": 0, "freed_bytes": 0}
    try:
        retention_days = max(0, int(config.get("archive_retention_days") or 0))
    except (TypeError, ValueError):
        retention_days = 0
    try:
        max_bytes = max(0, int(float(config.get("archive_max_gb") or 0) * 1024**3))
    except (TypeError, ValueError):
        max_bytes = 0
    if not force and retention_days <= 0 and max_bytes <= 0:
        return {"removed_files": 0, "freed_bytes": 0}

    now = datetime.now(timezone.utc)
    connection = _connect(root)
    removed_files = 0
    freed_bytes = 0
    try:
        rows = connection.execute(
            "SELECT asset_id,local_path,saved_at FROM media WHERE saved=1 AND local_path<>'' ORDER BY saved_at ASC,asset_id ASC"
        ).fetchall()
        existing: list[tuple[int, Path, int, datetime | None]] = []
        for asset_id, local_path, saved_at in rows:
            file_path = Path(str(local_path or ""))
            if not file_path.is_file():
                connection.execute("UPDATE media SET saved=0,error='local file missing' WHERE asset_id=?", (asset_id,))
                continue
            try:
                size = file_path.stat().st_size
                parsed = datetime.fromisoformat(str(saved_at).replace("Z", "+00:00")) if saved_at else None
                if parsed and parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except (OSError, ValueError):
                size, parsed = 0, None
            existing.append((int(asset_id), file_path, size, parsed))

        total = sum(item[2] for item in existing)
        for asset_id, file_path, size, saved_at in existing:
            expired = bool(retention_days and saved_at and (now - saved_at).days >= retention_days)
            over_limit = bool(max_bytes and total > max_bytes)
            if not force and not expired and not over_limit:
                continue
            shared = int(
                connection.execute(
                    "SELECT COUNT(*) FROM media WHERE saved=1 AND local_path=?",
                    (str(file_path),),
                ).fetchone()[0]
            )
            if shared > 1:
                connection.execute(
                    "UPDATE media SET saved=0,local_path='',error='removed by local retention policy' WHERE asset_id=?",
                    (asset_id,),
                )
                continue
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                continue
            connection.execute(
                "UPDATE media SET saved=0,local_path='',error='removed by local retention policy' WHERE asset_id=?",
                (asset_id,),
            )
            removed_files += 1
            freed_bytes += size
            total = max(0, total - size)
        connection.commit()
    finally:
        connection.close()
    return {"removed_files": removed_files, "freed_bytes": freed_bytes}


def apply_archive_events(
    config: dict[str, Any],
    response: dict[str, Any],
    *,
    client: httpx.Client,
    headers: dict[str, str],
) -> dict[str, Any]:
    enabled = bool(response.get("archive_enabled"))
    config["archive_enabled"] = enabled
    events = response.get("archive_events") if isinstance(response.get("archive_events"), list) else []
    if not enabled or not events:
        return {"saved": 0, "cursor": archive_cursor(config), "errors": 0}

    root = archive_root(config)
    connection = _connect(root)
    cursor = archive_cursor(config)
    contiguous_cursor = cursor
    retry_gap = False
    saved = 0
    errors = 0
    last_error = ""
    media_headers = dict(headers)
    source_name = str(config.get("source_name") or "").strip()
    if source_name:
        media_headers["X-XASS-Source"] = source_name
    try:
        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = int(event.get("event_id") or 0)
            if event_id <= cursor:
                continue
            message_id = int(event.get("message_id") or 0)
            connection.execute(
                """
                INSERT INTO messages(id, telegram_message_id, chat_id, chat_type, chat_title, from_user_id,
                    from_username, direction, reply_to_message_id, forwarded_from, text_content, deleted, message_date, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    text_content=excluded.text_content, deleted=excluded.deleted, chat_title=excluded.chat_title,
                    from_username=excluded.from_username, updated_at=excluded.updated_at
                """,
                (
                    message_id,
                    int(event.get("telegram_message_id") or 0),
                    int(event.get("chat_id") or 0),
                    str(event.get("chat_type") or ""),
                    str(event.get("chat_title") or ""),
                    event.get("from_user_id"),
                    str(event.get("from_username") or ""),
                    str(event.get("direction") or "incoming"),
                    event.get("reply_to_message_id"),
                    str(event.get("forwarded_from") or ""),
                    str(event.get("text") or ""),
                    int(bool(event.get("deleted"))),
                    event.get("message_date"),
                    event.get("event_date"),
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO revisions(event_id,message_id,event_type,text_content,event_date) VALUES(?,?,?,?,?)",
                (event_id, message_id, str(event.get("event") or ""), str(event.get("text") or ""), event.get("event_date")),
            )
            event_failed = False
            for media in event.get("media") or []:
                if not isinstance(media, dict):
                    continue
                asset_id = int(media.get("id") or 0)
                previous = connection.execute("SELECT local_path,saved,checksum FROM media WHERE asset_id=?", (asset_id,)).fetchone()
                local_path = str(previous[0] or "") if previous else ""
                is_saved = bool(previous and previous[1] and local_path and Path(local_path).is_file())
                checksum = str(previous[2] or "") if previous else ""
                file_unique_id = str(media.get("file_unique_id") or "").strip()
                error = ""
                if not is_saved and file_unique_id:
                    duplicate = connection.execute(
                        "SELECT local_path,checksum FROM media WHERE file_unique_id=? AND saved=1 AND local_path<>'' LIMIT 1",
                        (file_unique_id,),
                    ).fetchone()
                    if duplicate and Path(str(duplicate[0])).is_file():
                        local_path, checksum, is_saved = str(duplicate[0]), str(duplicate[1] or ""), True
                if not is_saved:
                    local_path, error, checksum = _download_media(
                        client,
                        server_url=str(config.get("server_url") or ""),
                        headers=media_headers,
                        root=root,
                        event=event,
                        media=media,
                    )
                    is_saved = bool(local_path)
                    errors += int(not is_saved)
                    event_failed = event_failed or not is_saved
                    if error:
                        last_error = error
                saved_at = datetime.now(timezone.utc).isoformat() if is_saved else None
                connection.execute(
                    """INSERT OR REPLACE INTO media(asset_id,message_id,media_type,mime_type,file_size,local_path,
                    checksum,file_unique_id,saved_at,saved,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        asset_id, message_id, media.get("type"), media.get("mime_type"), media.get("file_size"),
                        local_path, checksum, file_unique_id, saved_at, int(is_saved), error,
                    ),
                )
            if event_failed:
                retry_gap = True
            elif not retry_gap:
                contiguous_cursor = event_id
            saved += 1
        connection.commit()
        cursor = contiguous_cursor
        _write_state(
            root,
            cursor,
            errors=errors,
            pending_retry=retry_gap,
            last_error=last_error,
        )
        cleanup_archive(config)
    finally:
        connection.close()
    return {"saved": saved, "cursor": cursor, "errors": errors, "folder": str(root)}


def conversation_rows(config: dict[str, Any], limit: int = 200) -> list[dict[str, Any]]:
    path = archive_root(config) / DB_FILE
    if not path.is_file():
        return []
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT m.*, (SELECT COUNT(*) FROM media x WHERE x.message_id=m.id AND x.saved=1) AS media_count
            FROM messages m ORDER BY m.id DESC LIMIT ?""",
            (max(1, min(int(limit), 2000)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()
