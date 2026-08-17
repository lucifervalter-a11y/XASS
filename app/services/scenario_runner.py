from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppConfig
from app.services.agent_commands import enqueue_agent_command
from app.services.heartbeat import list_sources


_running_scenarios: set[str] = set()


def try_start_scenario(scenario_id: str) -> bool:
    if scenario_id in _running_scenarios:
        return False
    _running_scenarios.add(scenario_id)
    return True


def finish_scenario(scenario_id: str) -> None:
    _running_scenarios.discard(scenario_id)


def scenario_is_dangerous(scenario: dict[str, Any]) -> bool:
    return any(str(item) in {"lock_all", "update_all"} for item in scenario.get("actions") or [])


async def execute_scenario(
    session: AsyncSession,
    *,
    scenario: dict[str, Any],
    config: AppConfig,
    actor_user_id: int,
) -> dict[str, Any]:
    actions = [str(item) for item in scenario.get("actions") or []]
    selected_devices = {str(item) for item in scenario.get("devices") or [] if str(item).strip()}
    delay_sec = max(0, min(int(scenario.get("delay_sec") or 0), 3600))
    sources = [source for source in await list_sources(session) if "PC" in str(source.source_type or "").upper()]
    if selected_devices:
        sources = [source for source in sources if source.source_name in selected_devices]

    changed_config = False
    step_results: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    skipped_offline: list[str] = []
    command_by_action = {"update_all": "update", "lock_all": "lock", "check_all": "ping"}
    scheduled_at = datetime.now(timezone.utc)

    for index, action in enumerate(actions):
        result: dict[str, Any] = {"index": index, "action": action, "status": "completed"}
        if action == "away_on":
            config.away_mode_enabled = True
            config.away_until_at = None
            changed_config = True
        elif action == "away_off":
            config.away_mode_enabled = False
            config.away_until_at = None
            changed_config = True
        elif action == "quiet_on":
            config.quiet_hours_enabled = True
            changed_config = True
        elif action == "quiet_off":
            config.quiet_hours_enabled = False
            changed_config = True
        elif action in command_by_action:
            command_name = command_by_action[action]
            queued: list[dict[str, Any]] = []
            offline: list[str] = []
            for source in sources:
                if not source.is_online:
                    offline.append(source.source_name)
                    if source.source_name not in skipped_offline:
                        skipped_offline.append(source.source_name)
                    continue
                not_before = scheduled_at + timedelta(seconds=delay_sec * index) if delay_sec else None
                command = await enqueue_agent_command(
                    session,
                    source_name=source.source_name,
                    command=command_name,
                    payload={"scenario_id": scenario.get("id"), "step": index},
                    actor_user_id=actor_user_id,
                    not_before_at=not_before,
                )
                row = {
                    "id": command.id,
                    "source_name": command.source_name,
                    "command": command.command,
                    "not_before_at": not_before.isoformat() if not_before else None,
                }
                queued.append(row)
                commands.append(row)
            result.update({"status": "queued" if queued else "skipped", "commands": queued, "offline": offline})
        else:
            result.update({"status": "failed", "error": "unsupported_action"})
        step_results.append(result)

    if changed_config:
        config.updated_by_user_id = actor_user_id
        await session.commit()
    return {
        "commands": commands,
        "steps": step_results,
        "skipped_offline": skipped_offline,
    }
