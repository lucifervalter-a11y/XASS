from __future__ import annotations

from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from app.services.server_migration import (
    MANIFEST, MigrationError, _env_text, _postgres_env, _restore_postgres,
    export_server_archive, inspect_server_archive, restore_server_archive,
)


class ServerMigrationTests(unittest.TestCase):
    def fixture(self, directory: Path) -> tuple[Path, dict]:
        root = directory / "old"
        (root / "app").mkdir(parents=True)
        (root / "data" / "avatars").mkdir(parents=True)
        (root / "assets" / "projects").mkdir(parents=True)
        (root / "data" / "media").mkdir()
        (root / "data" / "migration_exports").mkdir()
        (root / "pc_client").mkdir()
        (root / "requirements.txt").write_text("", encoding="utf-8")
        (root / "app" / "main.py").write_text("# fixture", encoding="utf-8")
        (root / "proxy.config.php").write_text("<?php // private local frontend config", encoding="utf-8")
        (root / "pc_client" / "config.json").write_text('{"api_key":"not-server-state"}', encoding="utf-8")
        (root / "data" / "profile-custom.json").write_text('{"name":"Owner","quote":"My quote"}', encoding="utf-8")
        (root / "data" / "avatars" / "owner.png").write_bytes(b"avatar")
        (root / "assets" / "projects" / "cover.png").write_bytes(b"cover")
        (root / "data" / "media" / "music.mp3").write_bytes(b"music")
        (root / "data" / "migration_exports" / "old-secret.tar.gz").write_bytes(b"exclude recursive archives")
        (root / "data" / "pwa_session_generation").write_text("9", encoding="utf-8")
        external = directory / "external-quotes.json"
        external.write_text('[{"text":"Keep this"}]', encoding="utf-8")
        settings = {
            "database_url": "sqlite+aiosqlite:///./data/live.db",
            "bot_token": "original-secret-token", "setup_api_key": "original-session-secret",
            "agent_api_key": "agent-secret", "owner_user_id": 42,
            "pwa_vapid_private_key": "original-push-key", "pwa_session_generation_path": "./data/pwa_session_generation",
            "profile_json_path": "./data/profile-custom.json", "quotes_json_path": str(external),
            "profile_avatars_dir": "./data/avatars", "projects_assets_dir": "./assets/projects",
            "media_root": "./data/media", "agent_migration_export_dir": "./data/migration_exports",
        }
        with closing(sqlite3.connect(root / "data" / "live.db")) as database, database:
            database.executescript("""
                CREATE TABLE media_assets (id INTEGER PRIMARY KEY, local_path TEXT);
                CREATE TABLE agent_commands (id INTEGER PRIMARY KEY, status TEXT, result TEXT);
                CREATE TABLE heartbeat_sources (id INTEGER PRIMARY KEY, is_online BOOLEAN);
                CREATE TABLE agent_credentials (id INTEGER PRIMARY KEY, api_key_hash TEXT);
            """)
            database.execute("INSERT INTO media_assets VALUES (?, ?)", (1, str(root / "data" / "media" / "music.mp3")))
            database.execute("INSERT INTO agent_commands VALUES (1, 'pending', '{}')")
            database.execute("INSERT INTO agent_commands VALUES (2, 'completed', '{}')")
            database.execute("INSERT INTO heartbeat_sources VALUES (1, 1)")
            database.execute("INSERT INTO agent_credentials VALUES (1, 'device-hash')")
        return root, settings

    def mutate(self, source: Path, target: Path, change) -> None:
        with tarfile.open(source, "r:gz") as archive:
            members = [(member, archive.extractfile(member).read()) for member in archive]
        members = change(members)
        with tarfile.open(target, "w:gz") as archive:
            for member, body in members:
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))

    def test_complete_snapshot_round_trip_rebases_paths_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, settings = self.fixture(directory)
            # Keep a WAL writer connected: committed pages must still be present.
            database = sqlite3.connect(root / "data" / "live.db")
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("INSERT INTO agent_credentials VALUES (2, 'committed-in-wal')")
            database.commit()
            archive = directory / "backup.tar.gz"
            try:
                exported = export_server_archive(root, archive, settings)
            finally:
                database.close()
            self.assertEqual(exported["database"], "sqlite")
            self.assertNotIn("original-secret", json.dumps(inspect_server_archive(archive)))
            with tarfile.open(archive, "r:gz") as packaged:
                names = packaged.getnames()
            self.assertNotIn("pc_client/config.json", names)
            self.assertNotIn("data/migration_exports/old-secret.tar.gz", names)
            self.assertFalse(any(name.endswith(("-wal", "-shm")) for name in names))
            new = directory / "new"
            restore_server_archive(archive, new)
            self.assertEqual((new / "data" / "avatars" / "owner.png").read_bytes(), b"avatar")
            self.assertEqual((new / "assets" / "projects" / "cover.png").read_bytes(), b"cover")
            self.assertTrue((new / "proxy.config.php").is_file())
            self.assertEqual((new / "data" / "pwa_session_generation").read_text(), "9")
            env = (new / ".env").read_text()
            self.assertIn("original-session-secret", env)
            self.assertIn("original-push-key", env)
            self.assertIn("QUOTES_JSON_PATH='./data/imported/quotes_json_path/external-quotes.json'", env)
            self.assertNotIn(str(root), env)
            self.assertEqual(json.loads((new / "data" / "imported" / "quotes_json_path" / "external-quotes.json").read_text())[0]["text"], "Keep this")
            with closing(sqlite3.connect(new / "data" / "serverredus.db")) as restored:
                self.assertEqual(restored.execute("SELECT local_path FROM media_assets").fetchone()[0], "./data/media/music.mp3")
                self.assertEqual(restored.execute("SELECT count(*) FROM agent_credentials").fetchone()[0], 2)
                self.assertEqual(restored.execute("SELECT status FROM agent_commands WHERE id=1").fetchone()[0], "failed")
                self.assertEqual(restored.execute("SELECT status FROM agent_commands WHERE id=2").fetchone()[0], "completed")
                self.assertEqual(restored.execute("SELECT is_online FROM heartbeat_sources").fetchone()[0], 0)
            with closing(sqlite3.connect(root / "data" / "live.db")) as original:
                self.assertEqual(original.execute("SELECT status FROM agent_commands WHERE id=1").fetchone()[0], "pending")

    def test_corruption_and_existing_target_leave_destination_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, settings = self.fixture(directory)
            good, bad, target = directory / "good.tar.gz", directory / "bad.tar.gz", directory / "new"
            export_server_archive(root, good, settings)
            self.mutate(good, bad, lambda members: [(member, b"broken" if member.name == "data/avatars/owner.png" else body) for member, body in members])
            with self.assertRaises(MigrationError):
                restore_server_archive(bad, target)
            self.assertFalse(target.exists())
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("user data")
            with self.assertRaisesRegex(MigrationError, "empty"):
                restore_server_archive(good, target)
            self.assertEqual(marker.read_text(), "user data")
            with self.assertRaisesRegex(MigrationError, "already exists"):
                export_server_archive(root, good, settings)

    def test_traversal_links_duplicates_and_size_limit_are_rejected_before_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, settings = self.fixture(directory)
            good = directory / "good.tar.gz"
            export_server_archive(root, good, settings)
            cases = [("../escape", tarfile.REGTYPE), ("C:/escape", tarfile.REGTYPE), ("data\\escape", tarfile.REGTYPE), ("linked", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE), (".env", tarfile.REGTYPE)]
            for index, (name, kind) in enumerate(cases):
                with self.subTest(name=name):
                    bad = directory / f"bad-{index}.tar.gz"
                    member = tarfile.TarInfo(name)
                    member.type, member.linkname = kind, "../outside"
                    self.mutate(good, bad, lambda members: members + [(member, b"")])
                    with self.assertRaises(MigrationError):
                        restore_server_archive(bad, directory / "new")
                    self.assertFalse((directory / "new").exists())
            with self.assertRaisesRegex(MigrationError, "limit"):
                restore_server_archive(good, directory / "new", max_bytes=20)

    def test_missing_database_cannot_produce_misleading_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, settings = self.fixture(directory)
            settings["database_url"] = "sqlite+aiosqlite:///./data/missing.db"
            archive = directory / "backup.tar.gz"
            with self.assertRaisesRegex(MigrationError, "does not exist"):
                export_server_archive(root, archive, settings)
            self.assertFalse(archive.exists())

    def test_postgres_restore_is_explicit_empty_and_transactional(self):
        with tempfile.TemporaryDirectory() as temporary:
            dump = Path(temporary) / "database.dump"
            dump.write_bytes(b"fixture")
            with patch("app.services.server_migration._pg_run", return_value="1") as run:
                with self.assertRaisesRegex(MigrationError, "empty"):
                    _restore_postgres(dump, "postgresql+asyncpg://user:password@localhost/new", {})
                self.assertEqual(run.call_count, 1)
            with patch("app.services.server_migration._pg_run", return_value="0") as run:
                _restore_postgres(dump, "postgresql+asyncpg://user:password@localhost/new", {"/old/$xass$/'media": "data/media"})
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(commands[1][0], "pg_restore")
                self.assertIn("--single-transaction", commands[-1])
                self.assertEqual(commands[-1].count("-f"), 2)
                self.assertNotIn("password", json.dumps(commands))
            env = _postgres_env("postgresql+asyncpg://user:p%40ss@localhost:5433/new?sslmode=require")
            self.assertEqual(env["PGPASSWORD"], "p@ss")
            self.assertEqual(env["PGSSLMODE"], "require")

    def test_dotenv_round_trip_and_literal_interpolation_cannot_mutate_a_secret(self):
        from dotenv import dotenv_values
        values = {"bot_token": "ordinary-secret", "setup_api_key": "a'b\\c\nd", "pwa_cookie_secure": True, "owner_user_id": 42, "authorized_user_ids": [1, 2]}
        restored = dotenv_values(stream=io.StringIO(_env_text(values)))
        self.assertEqual(restored["SETUP_API_KEY"], values["setup_api_key"])
        self.assertEqual(restored["AUTHORIZED_USER_IDS"], "1,2")
        with patch.dict("os.environ", {"XASS_TEST_EXPANSION": "different-secret"}):
            with self.assertRaisesRegex(MigrationError, "literal"):
                _env_text({"setup_api_key": "literal ${XASS_TEST_EXPANSION}"})
            with patch("app.services.server_migration._restore_postgres") as restore:
                with self.assertRaisesRegex(MigrationError, "literal"):
                    restore_server_archive(Path("unused.tar.gz"), Path("unused-target"), postgres_url="postgresql://user:${SECRET}@host/database")
                restore.assert_not_called()

    def test_cli_export_inspect_restore_on_fixtures(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, settings = self.fixture(directory)
            (root / ".env").write_text(_env_text(settings), encoding="utf-8")
            archive, target = directory / "cli.tar.gz", directory / "restored"
            script = Path(__file__).resolve().parents[1] / "deploy" / "migrate.py"
            commands = [
                ["export", "--root", str(root), "--output", str(archive)],
                ["inspect", str(archive)],
                ["restore", str(archive), "--target", str(target)],
            ]
            for arguments in commands:
                completed = subprocess.run([sys.executable, str(script), *arguments], capture_output=True, text=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertNotIn("original-session-secret", completed.stdout)
                self.assertIsInstance(json.loads(completed.stdout), dict)
            self.assertTrue((target / "data" / "serverredus.db").is_file())


if __name__ == "__main__":
    unittest.main()
