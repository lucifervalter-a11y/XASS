from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from deploy import music_diagnostics as diagnostics


SECRET = "SENTINEL-PRIVATE-https://private.invalid/media?ticket=topsecret-/home/private/config.json"


class RedactionTests(unittest.TestCase):
    def test_only_allowlisted_values_escape_from_untrusted_rows(self):
        report = diagnostics.summarize(
            [{"status": SECRET, "device": "agent:" + SECRET, "failure_code": SECRET, "detail": SECRET,
              "id": SECRET, "source_key": SECRET, "phase": SECRET}],
            [{"command": SECRET, "status": SECRET, "ok": "false", "message": SECRET,
              "player_state": SECRET, "player_error": "Не удалось декодировать аудиотрек " + SECRET}],
            [{"device": "agent:" + SECRET, "state": SECRET, "session_key": SECRET}],
            [{"version": "0.19.0" + SECRET, "player_state": SECRET, "player_error": SECRET, "source_name": SECRET}],
        )
        raw = json.dumps(report)
        for value in (SECRET, "topsecret", "private.invalid", "config.json", "session_key", "source_name", "ticket"):
            self.assertNotIn(value, raw)
        self.assertEqual(report["session"], {"kind": "agent", "state": "unknown"})
        self.assertEqual(report["agent_music_commands"]["groups"][0]["error_category"], "agent_command_failed")
        self.assertEqual(report["recent_online_agents"]["groups"][0]["version"], "unknown")

    def test_exact_decode_errors_and_sql_boolean_representations_are_classified(self):
        commands = [{"command": "music_play", "status": "completed", "ok": value,
                     "message": "Не удалось декодировать аудиотрек"} for value in (False, 0, "0", "false")]
        report = diagnostics.summarize([], commands, [], [])
        self.assertEqual(report["agent_music_commands"]["groups"][0]["error_category"], "agent_decode_failed")
        self.assertEqual(report["agent_music_commands"]["groups"][0]["count"], 4)
        self.assertEqual(diagnostics.category("Не удалось декодировать аудиотрек\n" + SECRET), "unknown")

    def test_transfer_codes_match_historical_phases_and_allowlisted_saved_codes(self):
        report = diagnostics.summarize([
            {"status": "failed", "device": "agent:secret", "detail": "Время переключения истекло", "phase": "waiting"},
            {"status": "failed", "device": "agent:secret", "detail": "Время переключения истекло", "phase": "starting"},
            {"status": "failed", "device": "local", "detail": SECRET, "failure_code": "target_start_failed"},
        ], [], [], [])
        self.assertEqual({row["error_category"] for row in report["transfers"]["groups"]},
                         {"source_timeout", "target_timeout", "target_start_failed"})

    def test_fail_closed_does_not_print_exception_configuration_or_traceback(self):
        out, err = io.StringIO(), io.StringIO()
        with patch("app.config.Settings", side_effect=ValueError(SECRET)), patch.object(diagnostics.logging, "disable"), \
                redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(diagnostics.main(), 1)
        self.assertEqual(json.loads(out.getvalue()), {"ok": False, "error_category": "diagnostics_unavailable"})
        self.assertEqual(err.getvalue(), "")


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_model_columns_support_the_readonly_queries(self):
        from sqlalchemy import create_engine
        from app.models import AgentCommand, HeartbeatSource
        from app.music_models import MusicSession
        from app.music_playback_models import MusicTransfer
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "actual-models.sqlite3"
            engine = create_engine("sqlite:///" + path.as_posix())
            try:
                with engine.begin() as connection:
                    for model in (AgentCommand, HeartbeatSource, MusicSession, MusicTransfer):
                        model.__table__.create(connection)
            finally:
                engine.dispose()
            report = await diagnostics.collect("sqlite+aiosqlite:///" + path.as_posix())
            self.assertTrue(report["ok"])
            self.assertEqual(report["transfers"]["count"], 0)
            self.assertEqual(report["recent_online_agents"]["count"], 0)

    async def test_real_sqlite_is_bounded_recent_readonly_and_excludes_unrelated_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.sqlite3"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            def stamp(minutes):
                return (now - timedelta(minutes=minutes)).replace(tzinfo=None).isoformat(" ")
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE music_transfers(status TEXT, detail TEXT, target JSON, start_command_id INTEGER, created_at DATETIME);
                    CREATE TABLE agent_commands(command TEXT, status TEXT, result JSON, created_at DATETIME);
                    CREATE TABLE music_sessions(id INTEGER, device TEXT, state TEXT);
                    CREATE TABLE heartbeat_sources(is_online BOOLEAN, source_type TEXT, last_seen_at DATETIME, last_payload JSON);
                """)
                for index in range(30):
                    db.execute("INSERT INTO music_transfers VALUES(?,?,?,?,?)", ("failed", "Не удалось декодировать аудиотрек", json.dumps({"device": "agent:" + SECRET, "ticket": SECRET}), 1, stamp(index)))
                db.execute("INSERT INTO music_transfers VALUES(?,?,?,?,?)", ("ready", SECRET, "{}", None, stamp(0)))
                db.execute("INSERT INTO music_transfers VALUES(?,?,?,?,?)", ("failed", SECRET, "{}", None, stamp(3000)))
                for command in ("music_play", "musicXplay", "remote_clipboard"):
                    db.execute("INSERT INTO agent_commands VALUES(?,?,?,?)", (command, "failed", json.dumps({"ok": False, "message": SECRET, "details": {"state": "error", "error": "Не удалось декодировать аудиотрек", "url": SECRET}}), stamp(0)))
                db.execute("INSERT INTO music_sessions VALUES(?,?,?)", (1, "agent:" + SECRET, "loading"))
                for online, minutes in ((True, 0), (False, 0), (True, 3)):
                    db.execute("INSERT INTO heartbeat_sources VALUES(?,?,?,?)", (online, "PC_AGENT", stamp(minutes), json.dumps({"agent_version": "0.17.0", "api_key": SECRET, "music_player": {"state": "error", "error": "Не удалось декодировать аудиотрек"}})))
            db.close()
            before = hashlib.sha256(path.read_bytes()).digest()
            report = await diagnostics.collect("sqlite+aiosqlite:///" + path.as_posix(), now=now)
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)
            self.assertEqual(report["transfers"]["count"], 20)
            self.assertEqual(report["transfers"]["groups"], [{"status": "failed", "target_kind": "agent", "error_category": "agent_decode_failed", "count": 20}])
            self.assertEqual(report["agent_music_commands"]["count"], 1)
            self.assertEqual(report["recent_online_agents"]["count"], 1)
            self.assertEqual(report["recent_online_agents"]["groups"][0]["version"], "0.17.0")
            self.assertNotIn(SECRET, json.dumps(report))
            url = diagnostics.readonly_url("sqlite+aiosqlite:///" + path.as_posix())
            self.assertEqual(dict(url.query), {"mode": "ro", "uri": "true"})
            engine = diagnostics.create_async_engine(url)
            try:
                async with engine.connect() as connection:
                    with self.assertRaises(Exception):
                        await connection.execute(diagnostics.text("DELETE FROM music_sessions"))
            finally:
                await engine.dispose()
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)

    async def test_missing_sqlite_database_is_never_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                await diagnostics.collect("sqlite+aiosqlite:///" + path.as_posix())
            self.assertFalse(path.exists())

    async def test_postgres_starts_readonly_and_sets_short_statement_deadline(self):
        calls = []
        class Result:
            def mappings(self): return []
        class Connection:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return None
            async def execute(self, statement, params=None):
                calls.append(str(statement)); return Result()
        class Engine:
            def connect(self): return Connection()
            async def dispose(self): pass
        with patch.object(diagnostics, "create_async_engine", return_value=Engine()) as create:
            result = await diagnostics.collect("postgresql+asyncpg://fixture:secret@invalid/db")
        self.assertTrue(result["ok"])
        self.assertEqual(calls[:2], ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '5000ms'"])
        self.assertTrue(all(query.startswith("SELECT ") for query in calls[2:]))
        self.assertEqual(create.call_args.kwargs["connect_args"], {"timeout": 5, "command_timeout": 5})


if __name__ == "__main__":
    unittest.main()
