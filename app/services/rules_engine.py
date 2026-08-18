from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import HeartbeatSource
from app.services.agent_installer import get_agent_installer
from app.services.notifications import emit_notification
from app.services.rules_store import load_rules


_active_since: dict[str, float] = {}
_last_triggered: dict[str, float] = {}


def _version_tuple(value: object) -> tuple[int, ...]:
    result: list[int] = []
    for part in str(value or "0").split(".")[:4]:
        digits = "".join(ch for ch in part if ch.isdigit())
        result.append(int(digits or 0))
    return tuple(result)


def _source_condition(rule: dict[str, Any], source: HeartbeatSource, latest_version: str, now: float) -> tuple[bool, str]:
    payload = source.last_payload if isinstance(source.last_payload, dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    condition, threshold = rule["condition"], float(rule.get("threshold") or 0)
    if condition == "agent_offline":
        last_seen = source.last_seen_at.timestamp() if source.last_seen_at else 0
        minutes = max(0.0, (now - last_seen) / 60)
        return minutes >= (threshold or 5), f"Нет связи {int(minutes)} мин."
    if condition == "cpu_high":
        value = float(metrics.get("cpu_percent") or 0)
        return value >= (threshold or 90), f"CPU {value:.0f}%"
    if condition == "disk_low":
        free = 100 - float(metrics.get("disk_used_percent") or 0)
        return free <= (threshold or 10), f"Свободно {free:.0f}% диска"
    if condition == "agent_outdated":
        current = str(payload.get("agent_version") or "0.0.0")
        return bool(latest_version and _version_tuple(current) < _version_tuple(latest_version)), f"Агент {current}, доступна {latest_version}"
    return False, ""


async def evaluate_rules(
    session: AsyncSession,
    settings: Settings,
    *,
    sources: list[HeartbeatSource],
    services: dict[str, str],
) -> list[str]:
    rules = [item for item in load_rules(Path(settings.rules_json_path)) if item.get("enabled", True)]
    installer = get_agent_installer(settings)
    latest_version = installer.version if installer else ""
    now = time.time()
    triggered: list[str] = []
    live_keys: set[str] = set()
    for rule in rules:
        candidates: list[tuple[str, bool, str]] = []
        if rule["condition"] == "service_down":
            names = [rule["service"]] if rule.get("service") else list(services)
            for name in names:
                value = str(services.get(name) or "unknown")
                candidates.append((name, value != "active", f"Сервис {name}: {value}"))
        else:
            for source in sources:
                if rule.get("device") and rule["device"] != source.source_name:
                    continue
                active, message = _source_condition(rule, source, latest_version, now)
                candidates.append((source.source_name, active, message))
        for target, active, message in candidates:
            key = f"{rule['id']}:{target}"
            live_keys.add(key)
            if not active:
                _active_since.pop(key, None)
                continue
            since = _active_since.setdefault(key, now)
            if now - since < int(rule.get("duration_minutes") or 0) * 60:
                continue
            cooldown = int(rule.get("cooldown_minutes") or 60) * 60
            if now - _last_triggered.get(key, 0) < cooldown:
                continue
            await emit_notification(
                session, event_type="automation_rule", title=rule["name"], message=message,
                device=target if rule["condition"] != "service_down" else None,
                priority=rule["priority"], requires_action=rule["priority"] in {"warning", "critical"},
                details={"rule_id": rule["id"], "condition": rule["condition"], "target": target},
                dedup_key=f"rule:{key}:{int(now // max(60, cooldown))}", cooldown_sec=cooldown,
            )
            _last_triggered[key] = now
            triggered.append(key)
    for key in list(_active_since):
        if key not in live_keys:
            _active_since.pop(key, None)
    return triggered
