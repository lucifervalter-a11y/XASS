from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ALLOWED_ACTIONS = {
    "lock_all",
    "away_on",
    "away_off",
    "quiet_on",
    "quiet_off",
    "update_all",
    "check_all",
}
BUILTIN_SCENARIOS = [
    {"id": "away", "name": "Ушёл", "icon": "door", "color": "#376dff", "actions": ["away_on", "lock_all"]},
    {"id": "returned", "name": "Вернулся", "icon": "home", "color": "#38c986", "actions": ["away_off", "quiet_off", "check_all"]},
    {"id": "night", "name": "Ночь", "icon": "moon", "color": "#745cff", "actions": ["quiet_on", "lock_all"]},
    {"id": "work", "name": "Работа", "icon": "briefcase", "color": "#2e91ff", "actions": ["away_off", "quiet_off", "check_all"]},
    {"id": "lock-all", "name": "Заблокировать все ПК", "icon": "lock", "color": "#ff6673", "actions": ["lock_all"]},
    {"id": "update-all", "name": "Обновить все агенты", "icon": "update", "color": "#4e82ff", "actions": ["update_all"]},
    {"id": "check-all", "name": "Проверить все устройства", "icon": "pulse", "color": "#38c986", "actions": ["check_all"]},
]
for _builtin in BUILTIN_SCENARIOS:
    _builtin.update(
        {
            "builtin": True,
            "devices": [],
            "steps": [{"action": action} for action in _builtin["actions"]],
            "delay_sec": 0,
            "schedule": "",
            "enabled": True,
        }
    )


def _scenario_id(value: object, fallback: str = "scenario") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9а-яё_-]+", "-", text, flags=re.IGNORECASE).strip("-")
    return text[:64] or fallback


def normalize_scenario(raw: Any, *, fallback_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:120]
    if not name:
        return None
    scenario_id = _scenario_id(raw.get("id"), fallback_id)
    raw_steps = raw.get("steps") if isinstance(raw.get("steps"), list) else []
    raw_actions = raw.get("actions") if isinstance(raw.get("actions"), list) else []
    if raw_steps:
        raw_actions = [item.get("action") for item in raw_steps if isinstance(item, dict)]
    actions = []
    for action in raw_actions:
        clean = str(action or "").strip().lower()
        if clean in ALLOWED_ACTIONS and clean not in actions:
            actions.append(clean)
    if not actions:
        return None
    devices: list[str] = []
    for device in raw.get("devices") if isinstance(raw.get("devices"), list) else []:
        clean_device = str(device or "").strip()[:128]
        if clean_device and clean_device not in devices:
            devices.append(clean_device)
    icon = re.sub(r"[^a-z0-9_-]", "", str(raw.get("icon") or "bolt").lower())[:32] or "bolt"
    color = str(raw.get("color") or "#376dff").strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", color):
        color = "#376dff"
    try:
        delay_sec = max(0, min(int(raw.get("delay_sec") or 0), 3600))
    except (TypeError, ValueError):
        delay_sec = 0
    schedule = str(raw.get("schedule") or "").strip()[:64]
    if schedule and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule):
        schedule = ""
    return {
        "id": scenario_id,
        "name": name,
        "icon": icon,
        "color": color,
        "devices": devices,
        "actions": actions,
        "steps": [{"action": action} for action in actions],
        "delay_sec": delay_sec,
        "schedule": schedule,
        "enabled": bool(raw.get("enabled", True)),
        "builtin": False,
    }


def normalize_scenarios(raw: Any) -> list[dict[str, Any]]:
    items = raw.get("scenarios") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    builtin_ids = {item["id"] for item in BUILTIN_SCENARIOS}
    result: list[dict[str, Any]] = []
    used = set(builtin_ids)
    for index, item in enumerate(items, start=1):
        normalized = normalize_scenario(item, fallback_id=f"scenario-{index}")
        if normalized is None:
            continue
        base = normalized["id"]
        suffix = 2
        while normalized["id"] in used:
            normalized["id"] = f"{base}-{suffix}"
            suffix += 1
        used.add(normalized["id"])
        result.append(normalized)
    return result


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []
    return normalize_scenarios(raw)


def save_scenarios(path: Path, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = normalize_scenarios(scenarios)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "scenarios": normalized}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return normalized


def all_scenarios(path: Path) -> list[dict[str, Any]]:
    return [dict(item) for item in BUILTIN_SCENARIOS] + load_scenarios(path)


def upsert_scenario(path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    existing = load_scenarios(path)
    candidate = dict(raw)
    if not str(candidate.get("id") or "").strip():
        candidate["id"] = _scenario_id(candidate.get("name"), f"scenario-{len(existing) + 1}")
    normalized = normalize_scenario(candidate, fallback_id=f"scenario-{len(existing) + 1}")
    if normalized is None:
        raise ValueError("Укажите название и хотя бы одно действие")
    builtin_ids = {item["id"] for item in BUILTIN_SCENARIOS}
    if normalized["id"] in builtin_ids:
        raise ValueError("Встроенный сценарий нельзя заменить")
    replaced = False
    for index, item in enumerate(existing):
        if item["id"] == normalized["id"]:
            existing[index] = normalized
            replaced = True
            break
    if not replaced:
        used = builtin_ids | {item["id"] for item in existing}
        base = normalized["id"]
        suffix = 2
        while normalized["id"] in used:
            normalized["id"] = f"{base}-{suffix}"
            suffix += 1
        existing.append(normalized)
    save_scenarios(path, existing)
    return normalized


def delete_scenario(path: Path, scenario_id: str) -> bool:
    if scenario_id in {item["id"] for item in BUILTIN_SCENARIOS}:
        return False
    current = load_scenarios(path)
    remaining = [item for item in current if item["id"] != scenario_id]
    if len(remaining) == len(current):
        return False
    save_scenarios(path, remaining)
    return True


def find_scenario(path: Path, scenario_id: str) -> dict[str, Any] | None:
    return next((item for item in all_scenarios(path) if item["id"] == scenario_id), None)
