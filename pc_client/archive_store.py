from __future__ import annotations

import json
import re
import sqlite3
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


def archive_cursor(config: dict[str, Any]) -> int:
    try:
        payload = json.loads(_state_path(config).read_text(encoding="utf-8"))
        return max(0, int(payload.get("cursor") or 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def archive_status(config: dict[str, Any]) -> dict[str, Any]:
    root = archive_root(config)
    cursor = archive_cursor(config)
    try:
        database_size = (root / DB_FILE).stat().st_size
    except OSError:
        database_size = 0
    return {
        "folder": str(root),
        "cursor": cursor,
        "database_size": database_size,
        "enabled": bool(config.get("archive_enabled", False)),
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
            saved INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        """
    )
    return connection


def _write_state(root: Path, cursor: int) -> None:
    target = root / STATE_FILE
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"cursor": int(cursor)}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def _download_media(
    client: httpx.Client,
    *,
    server_url: str,
    headers: dict[str, str],
    root: Path,
    event: dict[str, Any],
    media: dict[str, Any],
) -> tuple[str, str]:
    chat_dir = root / "media" / _safe_part(event.get("chat_id"), "chat")
    chat_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_part(media.get("file_name"), f"media-{media.get('id') or 'file'}")
    target = chat_dir / f"{event.get('telegram_message_id') or event.get('message_id')}_{filename}"
    if target.is_file() and target.stat().st_size > 0:
        return str(target), ""
    url = f"{server_url.rstrip('/')}{str(media.get('download_path') or '')}"
    temporary = target.with_suffix(target.suffix + ".download")
    try:
        with client.stream("GET", url, headers=headers, timeout=60) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 256):
                    handle.write(chunk)
        temporary.replace(target)
        return str(target), ""
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return "", str(exc)[:500]


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
                    from_username, direction, reply_to_message_id, text_content, deleted, message_date, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                previous = connection.execute("SELECT local_path,saved FROM media WHERE asset_id=?", (asset_id,)).fetchone()
                local_path = str(previous[0] or "") if previous else ""
                is_saved = bool(previous and previous[1] and local_path and Path(local_path).is_file())
                error = ""
                if not is_saved:
                    local_path, error = _download_media(
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
                connection.execute(
                    """INSERT OR REPLACE INTO media(asset_id,message_id,media_type,mime_type,file_size,local_path,saved,error)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (asset_id, message_id, media.get("type"), media.get("mime_type"), media.get("file_size"), local_path, int(is_saved), error),
                )
            if event_failed:
                retry_gap = True
            elif not retry_gap:
                contiguous_cursor = event_id
            saved += 1
        connection.commit()
        cursor = contiguous_cursor
        _write_state(root, cursor)
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
