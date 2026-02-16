from __future__ import annotations

import json
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "Ваше имя",
    "title": "Full-stack разработчик",
    "bio": "Коротко о себе",
    "username": "username",
    "telegram_url": "https://t.me/username",
    "links": [
        {"label": "GitHub", "url": "https://github.com/username"},
    ],
    "stack": ["Python", "FastAPI", "PostgreSQL"],
    "quote": "Делаем просто, надежно и без магии.",
    "now_listening_text": "Не указано",
    "now_listening_auto_enabled": True,
    "now_listening_source": "pc_agent",
    "now_listening_updated_at": "",
    "iphone_hook_key": "",
    "vk_user_id": "",
    "vk_access_token": "",
    "vk_connected_at": "",
    "weather_text": "Не указано",
    "weather_auto_enabled": True,
    "weather_location_name": "Москва",
    "weather_latitude": 55.7558,
    "weather_longitude": 37.6176,
    "weather_timezone": "Europe/Moscow",
    "weather_refresh_minutes": 60,
    "weather_updated_at": "",
    "avatar_url": "",
}

ALLOWED_FIELDS = {
    "name",
    "title",
    "bio",
    "username",
    "telegram_url",
    "links",
    "stack",
    "quote",
    "now_listening_text",
    "now_listening_auto_enabled",
    "now_listening_source",
    "now_listening_updated_at",
    "iphone_hook_key",
    "vk_user_id",
    "vk_access_token",
    "vk_connected_at",
    "weather_text",
    "weather_auto_enabled",
    "weather_location_name",
    "weather_latitude",
    "weather_longitude",
    "weather_timezone",
    "weather_refresh_minutes",
    "weather_updated_at",
    "avatar_url",
}

STRING_FIELDS = {
    "name",
    "title",
    "bio",
    "username",
    "telegram_url",
    "quote",
    "now_listening_text",
    "now_listening_source",
    "now_listening_updated_at",
    "iphone_hook_key",
    "vk_access_token",
    "vk_connected_at",
    "weather_text",
    "weather_location_name",
    "weather_timezone",
    "weather_updated_at",
    "avatar_url",
}

BOOL_FIELDS = {
    "now_listening_auto_enabled",
    "weather_auto_enabled",
}

FLOAT_FIELDS = {
    "weather_latitude",
    "weather_longitude",
}

INT_FIELDS = {
    "vk_user_id",
}


def default_profile() -> dict[str, Any]:
    return deepcopy(DEFAULT_PROFILE)


def _normalize_string(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value).strip()


def _normalize_bool(value: Any, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"1", "true", "yes", "y", "on"}:
            return True
        if raw in {"0", "false", "no", "n", "off"}:
            return False
    return fallback


def _normalize_float(value: Any, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip().replace(",", ".")
        if not raw:
            return fallback
        try:
            return float(raw)
        except ValueError:
            return fallback
    return fallback


def _normalize_int(value: Any, fallback: int, *, min_value: int, max_value: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        parsed = int(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return fallback
        try:
            parsed = int(float(raw))
        except ValueError:
            return fallback
    else:
        return fallback

    if parsed < min_value:
        return min_value
    if parsed > max_value:
        return max_value
    return parsed


def _normalize_links(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return deepcopy(DEFAULT_PROFILE["links"])

    links: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = _normalize_string(item.get("label"))
        url = _normalize_string(item.get("url"))
        if not label or not url:
            continue
        links.append({"label": label, "url": url})
    return links


def _normalize_stack(value: Any) -> list[str]:
    if not isinstance(value, list):
        return deepcopy(DEFAULT_PROFILE["stack"])

    stack: list[str] = []
    for item in value:
        text = _normalize_string(item)
        if text:
            stack.append(text)
    return stack


def normalize_profile(raw: dict[str, Any] | None) -> dict[str, Any]:
    profile = default_profile()
    if not isinstance(raw, dict):
        return profile

    for key in STRING_FIELDS:
        if key in raw:
            profile[key] = _normalize_string(raw.get(key), profile[key])

    for key in BOOL_FIELDS:
        if key in raw:
            profile[key] = _normalize_bool(raw.get(key), bool(profile[key]))

    for key in FLOAT_FIELDS:
        if key in raw:
            profile[key] = _normalize_float(raw.get(key), float(profile[key]))

    for key in INT_FIELDS:
        if key in raw:
            raw_value = raw.get(key)
            if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
                profile[key] = ""
            else:
                parsed = _normalize_int(raw_value, 0, min_value=0, max_value=2_147_483_647)
                profile[key] = parsed if parsed > 0 else ""

    if "weather_refresh_minutes" in raw:
        profile["weather_refresh_minutes"] = _normalize_int(
            raw.get("weather_refresh_minutes"),
            int(profile["weather_refresh_minutes"]),
            min_value=10,
            max_value=720,
        )

    profile["links"] = _normalize_links(raw.get("links", profile["links"]))
    profile["stack"] = _normalize_stack(raw.get("stack", profile["stack"]))
    return profile


def load_profile(profile_path: Path) -> dict[str, Any]:
    if not profile_path.exists():
        return default_profile()

    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return default_profile()
    return normalize_profile(payload)


def save_profile(profile_path: Path, profile_data: dict[str, Any]) -> None:
    normalized = normalize_profile(profile_data)
    profile_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = profile_path.with_suffix(profile_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(profile_path)


def validate_http_url(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return cleaned
    raise ValueError(f"Поле '{field_name}' должно начинаться с http:// или https://")


def parse_link_input(raw_text: str) -> tuple[str, str]:
    parts = [part.strip() for part in raw_text.split("|", maxsplit=1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("Формат: Название | https://example.com")
    label = parts[0]
    url = validate_http_url(parts[1], field_name="url")
    return label, url


def parse_link_rename_input(raw_text: str) -> tuple[int, str, str | None]:
    parts = [part.strip() for part in raw_text.split("|")]
    if len(parts) < 2:
        raise ValueError("Формат: Номер | Новое название | https://new-url (опционально)")

    try:
        index = int(parts[0]) - 1
    except ValueError as exc:
        raise ValueError("Номер ссылки должен быть числом") from exc

    new_label = parts[1]
    if not new_label:
        raise ValueError("Новое название не должно быть пустым")

    new_url: str | None = None
    if len(parts) >= 3 and parts[2]:
        new_url = validate_http_url(parts[2], field_name="url")

    return index, new_label, new_url


def parse_one_based_index(raw_text: str) -> int:
    try:
        idx = int(raw_text.strip()) - 1
    except ValueError as exc:
        raise ValueError("Введите номер в виде числа") from exc
    if idx < 0:
        raise ValueError("Номер должен быть больше нуля")
    return idx


def parse_stack_replace(raw_text: str) -> list[str]:
    values = [item.strip() for item in raw_text.split(",")]
    stack = [item for item in values if item]
    if not stack:
        raise ValueError("Список стека пуст. Пример: Python, FastAPI, PostgreSQL")
    return stack


def parse_weather_location_input(raw_text: str) -> tuple[str, float, float, str]:
    parts = [part.strip() for part in raw_text.split("|")]
    if len(parts) < 3:
        raise ValueError("Формат: Название | Широта | Долгота | Timezone (опционально)")

    location_name = parts[0]
    if not location_name:
        raise ValueError("Название локации не должно быть пустым")

    try:
        latitude = float(parts[1].replace(",", "."))
    except ValueError as exc:
        raise ValueError("Широта должна быть числом") from exc
    if latitude < -90 or latitude > 90:
        raise ValueError("Широта должна быть в диапазоне от -90 до 90")

    try:
        longitude = float(parts[2].replace(",", "."))
    except ValueError as exc:
        raise ValueError("Долгота должна быть числом") from exc
    if longitude < -180 or longitude > 180:
        raise ValueError("Долгота должна быть в диапазоне от -180 до 180")

    timezone_name = parts[3] if len(parts) >= 4 and parts[3] else "Europe/Moscow"
    return location_name, latitude, longitude, timezone_name


def changed_fields(old_profile: dict[str, Any], new_profile: dict[str, Any]) -> list[str]:
    old_normalized = normalize_profile(old_profile)
    new_normalized = normalize_profile(new_profile)

    changed: list[str] = []
    for key in sorted(ALLOWED_FIELDS):
        if old_normalized.get(key) != new_normalized.get(key):
            changed.append(key)
    return changed


def backup_profile_file(profile_path: Path, backup_dir: Path) -> Path | None:
    if not profile_path.exists():
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"profile_{stamp}.json"

    counter = 1
    while backup_path.exists():
        backup_path = backup_dir / f"profile_{stamp}_{counter}.json"
        counter += 1

    shutil.copy2(profile_path, backup_path)
    return backup_path


def append_audit_log(audit_log_path: Path, actor_user_id: int, action: str, payload: dict[str, Any]) -> None:
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor_user_id": actor_user_id,
        "action": action,
        "payload": payload,
    }
    with audit_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def save_profile_with_backup(
    *,
    profile_path: Path,
    backup_dir: Path,
    audit_log_path: Path,
    actor_user_id: int,
    action: str,
    profile_data: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path | None, list[str]]:
    old_profile = load_profile(profile_path)
    new_profile = normalize_profile(profile_data)
    fields = changed_fields(old_profile, new_profile)

    backup_path = backup_profile_file(profile_path, backup_dir)
    save_profile(profile_path, new_profile)

    audit_payload = {
        "changed_fields": fields,
        "backup_path": str(backup_path) if backup_path else None,
    }
    if payload:
        audit_payload.update(payload)

    append_audit_log(audit_log_path, actor_user_id, action, audit_payload)
    return new_profile, backup_path, fields


def rollback_last_profile_version(
    *,
    profile_path: Path,
    backup_dir: Path,
    audit_log_path: Path,
    actor_user_id: int,
) -> tuple[dict[str, Any], Path, Path | None]:
    backups = sorted(backup_dir.glob("profile_*.json"), reverse=True)
    if not backups:
        raise FileNotFoundError("Нет резервных копий для отката")

    restore_from = backups[0]
    previous_backup = backup_profile_file(profile_path, backup_dir)

    payload = json.loads(restore_from.read_text(encoding="utf-8"))
    restored_profile = normalize_profile(payload)
    save_profile(profile_path, restored_profile)

    append_audit_log(
        audit_log_path,
        actor_user_id,
        "profile_rollback",
        {
            "restored_from": str(restore_from),
            "previous_backup": str(previous_backup) if previous_backup else None,
        },
    )
    return restored_profile, restore_from, previous_backup


def ensure_profile_exists(profile_path: Path) -> dict[str, Any]:
    profile = load_profile(profile_path)
    if not profile_path.exists():
        save_profile(profile_path, profile)
    return profile


def profile_preview_text(profile: dict[str, Any], profile_public_url: str | None = None) -> str:
    links = profile.get("links") or []
    stack = profile.get("stack") or []

    lines = [
        "Профиль сайта",
        "------------",
        f"Имя: {profile.get('name') or '-'}",
        f"Заголовок: {profile.get('title') or '-'}",
        f"Описание: {profile.get('bio') or '-'}",
        f"Username: {profile.get('username') or '-'}",
        f"Telegram URL: {profile.get('telegram_url') or '-'}",
        f"Цитата: {profile.get('quote') or '-'}",
        f"Now listening: {profile.get('now_listening_text') or '-'}",
        f"Авто now listening: {'вкл' if profile.get('now_listening_auto_enabled', True) else 'выкл'}",
        f"Источник now listening: {profile.get('now_listening_source') or 'pc_agent'}",
        f"VK user id: {profile.get('vk_user_id') or '-'}",
        f"VK token: {'задан' if profile.get('vk_access_token') else 'не задан'}",
        f"iPhone hook key: {'задан' if profile.get('iphone_hook_key') else 'не задан'}",
        f"Weather: {profile.get('weather_text') or '-'}",
        f"Авто-погода: {'вкл' if profile.get('weather_auto_enabled', True) else 'выкл'}",
        f"Локация погоды: {profile.get('weather_location_name') or '-'} "
        f"({profile.get('weather_latitude')}, {profile.get('weather_longitude')})",
        f"Часовой пояс погоды: {profile.get('weather_timezone') or '-'}",
        f"Интервал погоды (мин): {profile.get('weather_refresh_minutes') or '-'}",
        f"Avatar URL: {profile.get('avatar_url') or '-'}",
        "",
        "Ссылки:",
    ]

    if links:
        for idx, item in enumerate(links, start=1):
            lines.append(f"{idx}. {item.get('label', '-')} -> {item.get('url', '-')}")
    else:
        lines.append("- нет")

    lines.append("")
    lines.append("Стек:")
    if stack:
        lines.append(", ".join(stack))
    else:
        lines.append("- нет")

    if profile_public_url:
        lines.extend(["", f"Ссылка: {profile_public_url}"])

    return "\n".join(lines)
