"""Bounded read-only music diagnostics; stdout is an allowlisted aggregate only.

Run from the existing server checkout with its Python via stdin. This module
does not import app.main, initialize the schema, call an API or read media bytes.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import sys

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

WINDOW_MINUTES = 48 * 60
ROW_LIMIT = 20
AGENT_LIMIT = 100
TIMEOUT_SECONDS = 25
STATES = frozenset({"idle", "playing", "paused", "stopped", "ended", "loading", "error", "unavailable"})
TRANSFER_STATES = frozenset({"waiting", "stopping", "stopped", "starting", "ready", "failed", "cancelled"})
COMMAND_STATES = frozenset({"pending", "delivered", "completed", "failed", "cancelled", "awaiting_media"})
COMMANDS = frozenset({"music_play", "music_pause", "music_resume", "music_stop", "music_seek",
                      "music_volume", "music_outputs", "music_status", "music_storage_sync"})

# Exact matching only: neither an error string nor part of it is ever emitted.
ERROR_MESSAGES = {
    "Не удалось декодировать аудиотрек": "agent_decode_failed",
    "Не удалось прочитать трек. Поддерживаются MP3, WAV, FLAC и OGG Vorbis": "agent_decode_failed",
    "Формат не поддерживается на ПК. Используйте MP3, WAV, FLAC или OGG Vorbis": "agent_format_unsupported",
    "Для этого ПК нужен файл MP3, WAV, FLAC или OGG Vorbis": "agent_format_unsupported",
    "Не удалось открыть аудиовыход Windows": "agent_output_failed",
    "Аудиовыход отключён. Выберите доступное устройство": "agent_output_failed",
    "Ссылка на музыку истекла или доступ отозван. Запустите трек снова": "agent_media_access_failed",
    "Сервер не смог передать аудиотрек": "agent_download_failed",
    "Не удалось загрузить музыку. Проверьте подключение и повторите запуск": "agent_download_failed",
    "Загрузка музыки заняла слишком много времени": "agent_download_timeout",
    "Аудиофайл загружен не полностью": "agent_download_incomplete",
    "Сервер вернул неподдерживаемое сжатие аудиотрека": "agent_encoding_unsupported",
    "Аудиофайл больше допустимых 256 МБ": "agent_media_too_large",
    "Начальная позиция больше длительности трека": "agent_position_invalid",
    "Компьютер не в сети": "agent_offline",
    "ПК не подтвердил остановку звука": "source_stop_failed",
    "Не удалось остановить прежний ПК": "source_stop_failed",
    "Запуск был прерван. Повторите переключение": "target_start_failed",
    "ПК не подтвердил запуск музыки": "target_start_failed",
    "ПК не запустил музыку": "target_start_failed",
    "Переключение заменено новым действием": "replaced",
    "Переключение отменено": "transfer_cancelled",
}
TIMEOUT_MESSAGES = frozenset({"Время переключения истекло", "Время переключения истекло. Повторите действие",
    "Предыдущее устройство не подтвердило переключение. Музыка не запущена повторно; попробуйте ещё раз"})
UNAVAILABLE_MESSAGES = frozenset({"Компьютер не в сети", "ПК недоступен для музыки. Проверьте подключение и версию агента",
    "Перепривяжите ПК с индивидуальным ключом", "Обновите агент XASS до версии 0.16.0 или новее"})
ERROR_CODES = frozenset(ERROR_MESSAGES.values()) | {"source_timeout", "target_timeout", "target_unavailable",
    "agent_command_failed", "unknown", "none"}


def category(value, *, empty="none", fallback="unknown") -> str:
    if not isinstance(value, str) or not value.strip():
        return empty
    return ERROR_MESSAGES.get(value.strip(), fallback)


def allowed(value, options) -> str:
    return value if isinstance(value, str) and value in options else "unknown"


def device_kind(value) -> str:
    if value == "local":
        return "local"
    if isinstance(value, str) and value.startswith("agent:"):
        return "agent"
    return "unknown"


def version(value) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", value) else "unknown"


def grouped(rows, keys):
    counts = Counter(tuple(row[key] for key in keys) for row in rows)
    return [{**dict(zip(keys, values)), "count": count} for values, count in sorted(counts.items())]


def summarize(transfers, commands, sessions, agents) -> dict:
    transfer_rows = []
    for row in transfers[:ROW_LIMIT]:
        stored_code = row.get("failure_code")
        if isinstance(stored_code, str) and stored_code in ERROR_CODES:
            error = stored_code
        else:
            detail = row.get("detail")
            phase = row.get("phase")
            if isinstance(detail, str) and detail in TIMEOUT_MESSAGES:
                error = "target_timeout" if phase == "starting" else "source_timeout"
            elif isinstance(detail, str) and detail in UNAVAILABLE_MESSAGES:
                error = "target_unavailable" if phase == "starting" else "source_stop_failed"
            else:
                error = category(detail, empty="unknown" if row.get("status") == "failed" else "none",
                                 fallback="agent_command_failed")
        transfer_rows.append({"status": allowed(row.get("status"), TRANSFER_STATES),
                              "target_kind": device_kind(row.get("device")), "error_category": error})
    command_rows = []
    for row in commands[:ROW_LIMIT]:
        result_ok = row.get("ok")
        failure = row.get("status") == "failed" or result_ok is False or result_ok == 0 or (
            isinstance(result_ok, str) and result_ok.lower() in {"false", "0"})
        error = category(row.get("player_error"), empty="none", fallback="agent_command_failed")
        if error == "none" and failure:
            error = category(row.get("message"), empty="agent_command_failed", fallback="agent_command_failed")
        command_rows.append({"command": allowed(row.get("command"), COMMANDS),
                             "status": allowed(row.get("status"), COMMAND_STATES),
                             "player_state": allowed(row.get("player_state"), STATES), "error_category": error})
    session = sessions[0] if sessions else {}
    agent_rows = [{"version": version(row.get("version")), "player_state": allowed(row.get("player_state"), STATES),
                   "error_category": category(row.get("player_error"))} for row in agents[:AGENT_LIMIT]]
    return {"ok": True, "window_minutes": WINDOW_MINUTES,
            "limits": {"transfers": ROW_LIMIT, "commands": ROW_LIMIT, "agents": AGENT_LIMIT},
            "transfers": {"count": len(transfer_rows), "groups": grouped(transfer_rows, ("status", "target_kind", "error_category"))},
            "agent_music_commands": {"count": len(command_rows), "groups": grouped(command_rows, ("command", "status", "player_state", "error_category"))},
            "session": {"kind": device_kind(session.get("device")), "state": allowed(session.get("state"), STATES)},
            "recent_online_agents": {"count": len(agent_rows), "window_seconds": 120,
                                     "groups": grouped(agent_rows, ("version", "player_state", "error_category"))}}


def readonly_url(database_url: str):
    value = make_url(database_url)
    if value.get_backend_name() == "sqlite":
        if not value.database or value.database == ":memory:" or value.database.startswith("file:"):
            raise ValueError("Unsupported database")
        path = Path(value.database).resolve(strict=True)
        if not path.is_file():
            raise ValueError("Unsupported database")
        return value.set(drivername="sqlite+aiosqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"})
    if value.get_backend_name() in {"postgresql", "postgres"}:
        return value.set(drivername="postgresql+asyncpg")
    raise ValueError("Unsupported database")


def json_value(column: str, *keys: str, postgres: bool) -> str:
    # Identifiers and keys below are internal constants, never external input.
    if postgres:
        parts = "".join((" ->> " if index == len(keys) - 1 else " -> ") + "'" + key + "'" for index, key in enumerate(keys))
        value = column + parts
    else:
        value = "json_extract(" + column + ", '$." + ".".join(keys) + "')"
    return "substr(CAST((" + value + ") AS TEXT), 1, 2000)"


async def collect(database_url: str, *, now=None) -> dict:
    url = readonly_url(database_url)
    postgres = url.get_backend_name() == "postgresql"
    options = {"timeout": 5, "command_timeout": 5} if postgres else {"timeout": 5}
    engine = create_async_engine(url, echo=False, connect_args=options)
    now = now or datetime.now(timezone.utc)
    def field(column, *keys):
        return json_value(column, *keys, postgres=postgres)
    async def rows(connection, query, cutoff=None):
        statement = text(query)
        params = {}
        if cutoff is not None:
            statement = statement.bindparams(bindparam("cutoff", type_=DateTime(timezone=True)))
            params["cutoff"] = cutoff
        result = await connection.execute(statement, params)
        return [dict(item) for item in result.mappings()]
    try:
        async with engine.connect() as connection:
            if postgres:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                await connection.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            else:
                await connection.execute(text("PRAGMA query_only = ON"))
            cutoff = now - timedelta(minutes=WINDOW_MINUTES)
            transfers = await rows(connection, f"SELECT status, substr(detail,1,2000) AS detail, {field('target','device')} AS device, {field('target','failure_code')} AS failure_code, CASE WHEN start_command_id IS NULL THEN 'waiting' ELSE 'starting' END AS phase FROM music_transfers WHERE status = 'failed' AND created_at >= :cutoff ORDER BY created_at DESC LIMIT {ROW_LIMIT}", cutoff)
            commands = await rows(connection, f"SELECT command, status, {field('result','ok')} AS ok, {field('result','message')} AS message, {field('result','details','state')} AS player_state, {field('result','details','error')} AS player_error FROM agent_commands WHERE substr(command,1,6) = 'music_' AND created_at >= :cutoff ORDER BY created_at DESC LIMIT {ROW_LIMIT}", cutoff)
            sessions = await rows(connection, "SELECT device, state FROM music_sessions WHERE id=1 LIMIT 1")
            agents = await rows(connection, f"SELECT {field('last_payload','agent_version')} AS version, {field('last_payload','music_player','state')} AS player_state, {field('last_payload','music_player','error')} AS player_error FROM heartbeat_sources WHERE is_online = TRUE AND source_type = 'PC_AGENT' AND last_seen_at >= :cutoff ORDER BY last_seen_at DESC LIMIT {AGENT_LIMIT}", now - timedelta(seconds=120))
            return summarize(transfers, commands, sessions, agents)
    finally:
        await engine.dispose()


def main() -> int:
    # Framework/driver exceptions can contain DSNs, SQL parameters and secrets.
    logging.disable(logging.CRITICAL)
    try:
        sys.path.insert(0, str(Path.cwd()))
        from app.config import Settings
        settings = Settings()
        async def bounded():
            return await asyncio.wait_for(collect(settings.database_url), timeout=TIMEOUT_SECONDS)
        report = asyncio.run(bounded())
        print(json.dumps(report, ensure_ascii=True, sort_keys=True), flush=True)
        return 0
    except BaseException:
        print('{"ok":false,"error_category":"diagnostics_unavailable"}', flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
