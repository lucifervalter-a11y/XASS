from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_STATUS_VALUES = {
    "working",
    "testing",
    "dev",
    "unstable",
    "archived",
    "stable",
}


DEFAULT_PROJECTS = [
    {
        "id": "demo-project",
        "title": "Demo Project",
        "subtitle": "Example",
        "description": "Заполните проекты через Telegram-бота.",
        "url": "",
        "status": "dev",
        "years": {"from": datetime.now(timezone.utc).year, "to": datetime.now(timezone.utc).year},
        "tags": ["python", "fastapi"],
        "featured": True,
        "cover": {"type": "image", "src": ""},
        "sort": 100,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
]

DEFAULT_SITE_CONFIG = {
    "projects_background": {"type": "image", "src": ""},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        text = value.strip()
        return text if text else fallback
    text = str(value).strip()
    return text if text else fallback


def _to_int(value: Any, fallback: int, *, min_value: int = -999_999, max_value: int = 999_999) -> int:
    try:
        parsed = int(value)
    except Exception:
        return fallback
    if parsed < min_value:
        return min_value
    if parsed > max_value:
        return max_value
    return parsed


def _to_bool(value: Any, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return fallback


def _normalize_status(value: Any) -> str:
    text = _to_text(value, "dev").lower()
    return text if text in PROJECT_STATUS_VALUES else "dev"


def _normalize_url(value: Any) -> str:
    text = _to_text(value)
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return ""


def _slugify(value: str) -> str:
    text = value.lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-_")


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        items = []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _normalize_cover(value: Any) -> dict[str, str]:
    cover_type = "image"
    src = ""
    if isinstance(value, dict):
        raw_type = _to_text(value.get("type"), "image").lower()
        cover_type = raw_type if raw_type in {"image", "video"} else "image"
        src = _to_text(value.get("src"))
    return {"type": cover_type, "src": src}


def normalize_project(raw: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    title = _to_text(raw.get("title"), "Untitled")
    project_id = _slugify(_to_text(raw.get("id"), fallback_id)) or fallback_id

    years_raw = raw.get("years") if isinstance(raw.get("years"), dict) else {}
    year_from = _to_int((years_raw or {}).get("from"), datetime.now(timezone.utc).year, min_value=1970, max_value=2100)
    year_to = _to_int((years_raw or {}).get("to"), year_from, min_value=1970, max_value=2100)
    if year_to < year_from:
        year_to = year_from

    result = {
        "id": project_id,
        "title": title,
        "subtitle": _to_text(raw.get("subtitle")),
        "description": _to_text(raw.get("description")),
        "url": _normalize_url(raw.get("url")),
        "status": _normalize_status(raw.get("status")),
        "years": {"from": year_from, "to": year_to},
        "tags": _normalize_tags(raw.get("tags")),
        "featured": _to_bool(raw.get("featured"), False),
        "cover": _normalize_cover(raw.get("cover")),
        "sort": _to_int(raw.get("sort"), 100, min_value=-999_999, max_value=999_999),
        "updated_at": _to_text(raw.get("updated_at"), _now_iso()),
    }
    return result


def normalize_projects(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raw = DEFAULT_PROJECTS
    projects: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        fallback_id = f"project-{index}"
        project = normalize_project(item, fallback_id=fallback_id)
        base_id = project["id"]
        suffix = 2
        while project["id"] in used_ids:
            project["id"] = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(project["id"])
        projects.append(project)
    if not projects:
        projects = [normalize_project(DEFAULT_PROJECTS[0], fallback_id="project-1")]
    projects.sort(key=lambda item: (int(item.get("sort") or 0), str(item.get("title") or "").lower()))
    return projects


def normalize_site_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    bg_raw = raw.get("projects_background") if isinstance(raw.get("projects_background"), dict) else {}
    bg_type = _to_text((bg_raw or {}).get("type"), "image").lower()
    if bg_type not in {"image", "video"}:
        bg_type = "image"
    bg_src = _to_text((bg_raw or {}).get("src"))
    return {"projects_background": {"type": bg_type, "src": bg_src}}


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def load_projects(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return normalize_projects(DEFAULT_PROJECTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return normalize_projects(DEFAULT_PROJECTS)
    return normalize_projects(raw)


def save_projects(path: Path, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = normalize_projects(projects)
    _atomic_write_json(path, normalized)
    return normalized


def ensure_projects_exists(path: Path) -> list[dict[str, Any]]:
    projects = load_projects(path)
    if not path.exists():
        save_projects(path, projects)
    return projects


def load_site_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return normalize_site_config(DEFAULT_SITE_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return normalize_site_config(DEFAULT_SITE_CONFIG)
    return normalize_site_config(raw)


def save_site_config(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_site_config(config)
    _atomic_write_json(path, normalized)
    return normalized


def ensure_site_config_exists(path: Path) -> dict[str, Any]:
    config = load_site_config(path)
    if not path.exists():
        save_site_config(path, config)
    return config


def create_project_id(title: str, existing_ids: set[str]) -> str:
    base = _slugify(title) or "project"
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def find_project(projects: list[dict[str, Any]], project_id: str) -> dict[str, Any] | None:
    target = (project_id or "").strip()
    for item in projects:
        if str(item.get("id")) == target:
            return item
    return None


def set_featured(projects: list[dict[str, Any]], project_id: str) -> list[dict[str, Any]]:
    target = (project_id or "").strip()
    updated = normalize_projects(projects)
    matched = False
    for item in updated:
        is_target = str(item.get("id")) == target
        item["featured"] = is_target
        if is_target:
            matched = True
        item["updated_at"] = _now_iso()
    if not matched and updated:
        updated[0]["featured"] = True
    return updated


def move_sort(projects: list[dict[str, Any]], project_id: str, direction: str) -> list[dict[str, Any]]:
    items = normalize_projects(projects)
    index = -1
    target = (project_id or "").strip()
    for idx, item in enumerate(items):
        if str(item.get("id")) == target:
            index = idx
            break
    if index < 0:
        return items
    if direction == "up" and index > 0:
        items[index], items[index - 1] = items[index - 1], items[index]
    if direction == "down" and index < len(items) - 1:
        items[index], items[index + 1] = items[index + 1], items[index]
    base = 100
    step = 10
    for idx, item in enumerate(items):
        item["sort"] = base + idx * step
        item["updated_at"] = _now_iso()
    return normalize_projects(items)


def backup_json_file(path: Path, backup_dir: Path, prefix: str) -> Path | None:
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{prefix}_{stamp}.json"
    suffix = 2
    while backup_path.exists():
        backup_path = backup_dir / f"{prefix}_{stamp}_{suffix}.json"
        suffix += 1
    shutil.copy2(path, backup_path)
    return backup_path


def append_audit_log(path: Path, actor_user_id: int, action: str, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": _now_iso(),
        "actor_user_id": actor_user_id,
        "action": action,
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def project_card_text(project: dict[str, Any]) -> str:
    years = project.get("years") if isinstance(project.get("years"), dict) else {}
    y_from = years.get("from") or "-"
    y_to = years.get("to") or y_from
    tags = project.get("tags") if isinstance(project.get("tags"), list) else []
    tags_text = ", ".join(str(item) for item in tags) if tags else "-"
    url = _to_text(project.get("url"))
    return (
        f"Название: {project.get('title') or '-'}\n"
        f"ID: {project.get('id') or '-'}\n"
        f"Статус: {project.get('status') or '-'}\n"
        f"Годы: {y_from}-{y_to}\n"
        f"Ссылка: {url if url else 'нет'}\n"
        f"Теги: {tags_text}\n"
        f"Обновлено: {project.get('updated_at') or '-'}"
    )

