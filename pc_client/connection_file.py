from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_CONNECTION_FILE_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    server_url: str
    pair_code: str
    source_name: str
    expires_at: datetime
    auto_update: bool


def _server_origin(value: Any) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("В конфиге указан неверный адрес сервера")
    if parsed.username or parsed.password:
        raise ValueError("Адрес сервера не должен содержать логин или пароль")
    return f"{parsed.scheme}://{parsed.netloc}"


def parse_connection_payload(payload: Any, *, now: datetime | None = None) -> ConnectionProfile:
    if not isinstance(payload, dict):
        raise ValueError("Файл подключения должен содержать JSON-объект")
    if payload.get("format") != "xass-connect" or int(payload.get("version") or 0) != 1:
        raise ValueError("Это не файл подключения XASS или его версия не поддерживается")

    pair_code = str(payload.get("pair_code") or "").strip()
    if not 4 <= len(pair_code) <= 64:
        raise ValueError("В файле отсутствует корректный одноразовый ключ")

    raw_expiry = str(payload.get("expires_at") or "").strip()
    try:
        expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("В файле указано неверное время действия ключа") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if expires_at.astimezone(timezone.utc) <= reference.astimezone(timezone.utc):
        raise ValueError("Срок действия файла подключения истёк. Скачайте новый в Mini App")

    return ConnectionProfile(
        server_url=_server_origin(payload.get("server_url")),
        pair_code=pair_code,
        source_name=str(payload.get("source_name") or "").strip()[:128],
        expires_at=expires_at,
        auto_update=bool(payload.get("auto_update", True)),
    )


def parse_connection_text(text: str, *, now: datetime | None = None) -> ConnectionProfile:
    if len(text.encode("utf-8")) > MAX_CONNECTION_FILE_SIZE:
        raise ValueError("Файл подключения слишком большой")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Не удалось прочитать JSON-файл подключения") from exc
    return parse_connection_payload(payload, now=now)


def load_connection_file(path: Path, *, now: datetime | None = None) -> ConnectionProfile:
    if not path.is_file():
        raise ValueError("Файл подключения не найден")
    if path.stat().st_size > MAX_CONNECTION_FILE_SIZE:
        raise ValueError("Файл подключения слишком большой")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"Не удалось открыть файл подключения: {exc}") from exc
    return parse_connection_text(text, now=now)
