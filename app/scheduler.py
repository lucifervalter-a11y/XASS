import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.bot_api import TelegramBotClient
from app.config import Settings
from app.db import SessionLocal
from app.services.app_config import get_or_create_app_config, log_admin_action
from app.services.heartbeat import is_quiet_hours, list_sources, mark_offline_sources
from app.services.notifications import emit_notification
from app.services.profile_runtime import sync_profile_now_playing_from_heartbeat, sync_profile_weather
from app.services.scenario_runner import execute_scenario, finish_scenario, try_start_scenario
from app.services.scenarios_store import all_scenarios

logger = logging.getLogger(__name__)
_alert_state: dict[str, tuple[str, float]] = {}
_scenario_schedule_state: set[str] = set()


def source_health_alerts(source: object) -> list[tuple[str, str, str]]:
    payload = getattr(source, "last_payload", {})
    if not isinstance(payload, dict):
        return []
    name = str(getattr(source, "source_name", "agent") or "agent")
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    archive = payload.get("archive_status") if isinstance(payload.get("archive_status"), dict) else {}
    alerts: list[tuple[str, str, str]] = []
    load_parts: list[str] = []
    for metric_key, label in (("cpu_percent", "CPU"), ("ram_used_percent", "RAM")):
        try:
            metric_value = float(metrics.get(metric_key) or 0)
        except (TypeError, ValueError):
            metric_value = 0
        if metric_value >= 95:
            load_parts.append(f"{label} {metric_value:.1f}%")
    if load_parts:
        signature = ":".join(load_parts)
        alerts.append((f"{name}:high-load", signature, f"Высокая нагрузка на {name}: {', '.join(load_parts)}."))
    try:
        disk_percent = float(metrics.get("disk_used_percent") or 0)
    except (TypeError, ValueError):
        disk_percent = 0
    if disk_percent >= 92:
        alerts.append((f"{name}:disk", f"{disk_percent:.1f}", f"На агенте {name} заканчивается место: диск занят на {disk_percent:.1f}%."))
    try:
        free_bytes = int(archive.get("free_bytes") or 0)
    except (TypeError, ValueError):
        free_bytes = 0
    if archive.get("enabled") and 0 < free_bytes < 2 * 1024**3:
        free_gb = free_bytes / 1024**3
        alerts.append((f"{name}:archive-space", f"{free_bytes // (64 * 1024**2)}", f"Локальному архиву на {name} осталось {free_gb:.1f} ГБ."))
    archive_error = str(archive.get("last_error") or "").strip()
    if archive_error:
        alerts.append((f"{name}:archive-error", archive_error, f"Ошибка синхронизации архива на {name}: {archive_error[:350]}"))
    agent_error = str(payload.get("last_error") or "").strip()
    if agent_error:
        signature = f"{payload.get('last_error_at') or ''}:{agent_error}"
        alerts.append((f"{name}:agent-error", signature, f"Последняя ошибка агента {name}: {agent_error[:350]}"))
    return alerts


def _new_alerts(alerts: list[tuple[str, str, str]], now_ts: float, cooldown_sec: int = 3600) -> list[str]:
    return [item[2] for item in _new_alert_details(alerts, now_ts, cooldown_sec)]


def _new_alert_details(
    alerts: list[tuple[str, str, str]], now_ts: float, cooldown_sec: int = 3600
) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for key, signature, text in alerts:
        previous = _alert_state.get(key)
        if previous and previous[0] == signature and now_ts - previous[1] < cooldown_sec:
            continue
        _alert_state[key] = (signature, now_ts)
        result.append((key, signature, text))
    return result


def _notification_chat_id(settings: Settings, config_notify_chat_id: int | None) -> int | None:
    if config_notify_chat_id:
        return config_notify_chat_id
    if settings.notify_chat_id:
        return settings.notify_chat_id
    if settings.owner_user_id:
        return settings.owner_user_id
    return None


async def run_due_scenarios(session: object, settings: Settings, config: object) -> list[str]:
    try:
        local_now = datetime.now(timezone.utc).astimezone(ZoneInfo(settings.timezone))
    except ZoneInfoNotFoundError:
        local_now = datetime.now(timezone.utc)
    current_time = local_now.strftime("%H:%M")
    run_ids: list[str] = []
    for scenario in all_scenarios(Path(settings.scenarios_json_path)):
        if not scenario.get("enabled", True) or scenario.get("schedule") != current_time:
            continue
        scenario_id = str(scenario.get("id") or "")
        key = f"{scenario_id}:{local_now.date().isoformat()}:{current_time}"
        if key in _scenario_schedule_state or not try_start_scenario(scenario_id):
            continue
        _scenario_schedule_state.add(key)
        try:
            execution = await execute_scenario(
                session,
                scenario=scenario,
                config=config,
                actor_user_id=int(settings.owner_user_id or 0),
            )
            await log_admin_action(
                session,
                int(settings.owner_user_id or 0),
                "scenario_run",
                {
                    "scenario_id": scenario_id,
                    "actions": scenario.get("actions") or [],
                    "channel": "scheduler",
                    "commands": len(execution["commands"]),
                    "skipped_offline": execution["skipped_offline"],
                    "steps": execution["steps"],
                },
            )
            run_ids.append(scenario_id)
        finally:
            finish_scenario(scenario_id)
    if len(_scenario_schedule_state) > 500:
        today = local_now.date().isoformat()
        _scenario_schedule_state.intersection_update(key for key in _scenario_schedule_state if f":{today}:" in key)
    return run_ids


async def offline_check_loop(
    settings: Settings,
    bot_client: TelegramBotClient | None,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            async with SessionLocal() as session:
                config = await get_or_create_app_config(session, settings)
                stale_sources = await mark_offline_sources(session, config.heartbeat_timeout_minutes)
                sources = await list_sources(session)
                await run_due_scenarios(session, settings, config)
                await sync_profile_now_playing_from_heartbeat(session, settings, config.heartbeat_timeout_minutes)
                if stale_sources:
                    chat_id = _notification_chat_id(settings, config.notify_chat_id)
                    now = datetime.now(timezone.utc).isoformat()
                    for source in stale_sources:
                        text = (
                            f"OFFLINE alert\n"
                            f"source={source.source_name}\n"
                            f"type={source.source_type}\n"
                            f"last_seen={source.last_seen_at.isoformat()}\n"
                            f"server_time={now}"
                        )
                        _, policy = await emit_notification(
                            session,
                            event_type="agent_offline",
                            title=f"{source.source_name} не в сети",
                            message=f"Последний heartbeat: {source.last_seen_at.isoformat()}",
                            device=source.source_name,
                            priority="high",
                            requires_action=True,
                            details={"source_type": source.source_type, "last_seen_at": source.last_seen_at},
                            dedup_key=f"agent-offline:{source.source_name}:{source.went_offline_at}",
                            cooldown_sec=max(60, config.heartbeat_timeout_minutes * 60),
                        )
                        telegram_allowed = "telegram" in policy["channels"] and (
                            not policy["quiet_hours"] or not is_quiet_hours(config, settings)
                        )
                        if bot_client and chat_id and telegram_allowed:
                            text = (
                                f"OFFLINE alert\n"
                                f"source={source.source_name}\n"
                                f"type={source.source_type}\n"
                                f"last_seen={source.last_seen_at.isoformat()}\n"
                                f"server_time={now}"
                            )
                            await bot_client.send_message(chat_id, text)
                chat_id = _notification_chat_id(settings, config.notify_chat_id)
                source_by_name = {source.source_name: source for source in sources}
                alerts = [alert for source in sources if source.is_online for alert in source_health_alerts(source)]
                for key, _signature, text in _new_alert_details(alerts, datetime.now(timezone.utc).timestamp()):
                    source_name = key.split(":", 1)[0]
                    suffix = key.split(":", 1)[1] if ":" in key else "health"
                    event_type = "archive_error" if "error" in suffix else "low_disk" if "disk" in suffix or "space" in suffix else "high_load"
                    _, policy = await emit_notification(
                        session,
                        event_type=event_type,
                        title=f"{source_name}: требуется внимание",
                        message=text,
                        device=source_name,
                        priority="high" if event_type == "archive_error" else "normal",
                        requires_action=event_type == "archive_error",
                        details={"alert": suffix},
                        dedup_key=f"health:{key}",
                    )
                    telegram_allowed = "telegram" in policy["channels"] and (
                        not policy["quiet_hours"] or not is_quiet_hours(config, settings)
                    )
                    if bot_client and chat_id and telegram_allowed:
                        if source_name in source_by_name:
                            await bot_client.send_message(chat_id, text)
            await sync_profile_weather(settings)
        except Exception:
            logger.exception("offline_check_loop error")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.heartbeat_check_interval_sec)
        except asyncio.TimeoutError:
            continue
