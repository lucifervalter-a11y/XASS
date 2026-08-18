from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


CONDITIONS = {"agent_offline", "cpu_high", "disk_low", "service_down", "agent_outdated"}
PRIORITIES = {"info", "success", "warning", "critical"}


def _rule_id(value: object, fallback: str) -> str:
    clean = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    return clean[:64] or fallback


def normalize_rule(raw: Any, fallback: str = "rule") -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:120]
    condition = str(raw.get("condition") or "").strip().lower()
    if not name or condition not in CONDITIONS:
        return None
    try:
        threshold = max(0.0, min(float(raw.get("threshold") or 0), 10000.0))
        duration = max(0, min(int(raw.get("duration_minutes") or 0), 1440))
        cooldown = max(1, min(int(raw.get("cooldown_minutes") or 60), 10080))
    except (TypeError, ValueError):
        return None
    priority = str(raw.get("priority") or "warning").strip().lower()
    if priority not in PRIORITIES:
        priority = "warning"
    return {
        "id": _rule_id(raw.get("id"), fallback), "name": name, "condition": condition,
        "device": str(raw.get("device") or "").strip()[:128], "service": str(raw.get("service") or "").strip()[:128],
        "threshold": threshold, "duration_minutes": duration, "cooldown_minutes": cooldown,
        "priority": priority, "enabled": bool(raw.get("enabled", True)),
    }


def load_rules(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []
    rows = payload.get("rules") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, item in enumerate(rows, start=1):
        normalized = normalize_rule(item, f"rule-{index}")
        if normalized is None:
            continue
        base, suffix = normalized["id"], 2
        while normalized["id"] in used:
            normalized["id"] = f"{base}-{suffix}"
            suffix += 1
        used.add(normalized["id"])
        result.append(normalized)
    return result


def save_rules(path: Path, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [item for index, raw in enumerate(rules, start=1) if (item := normalize_rule(raw, f"rule-{index}"))]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "rules": normalized}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return normalized


def upsert_rule(path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    rules = load_rules(path)
    candidate = dict(raw)
    if not str(candidate.get("id") or "").strip():
        candidate["id"] = _rule_id(candidate.get("name"), f"rule-{len(rules) + 1}")
    normalized = normalize_rule(candidate, f"rule-{len(rules) + 1}")
    if normalized is None:
        raise ValueError("Проверьте название и условие правила")
    for index, item in enumerate(rules):
        if item["id"] == normalized["id"]:
            rules[index] = normalized
            break
    else:
        used = {item["id"] for item in rules}
        base, suffix = normalized["id"], 2
        while normalized["id"] in used:
            normalized["id"] = f"{base}-{suffix}"
            suffix += 1
        rules.append(normalized)
    save_rules(path, rules)
    return normalized


def delete_rule(path: Path, rule_id: str) -> bool:
    rules = load_rules(path)
    remaining = [item for item in rules if item["id"] != rule_id]
    if len(remaining) == len(rules):
        return False
    save_rules(path, remaining)
    return True
