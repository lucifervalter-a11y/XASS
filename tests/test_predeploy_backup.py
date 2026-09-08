from __future__ import annotations

from contextlib import closing, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy/predeploy_backup.py"
spec = importlib.util.spec_from_file_location("predeploy_backup", SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class PredeployBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "server"
        self.root.mkdir()
        self.git("init", "-q")
        (self.root / "index.php").write_text("<?php echo 'tracked';")
        self.git("add", "index.php")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        (self.root / ".env").write_text("BOT_TOKEN=private-sentinel\nDATABASE_URL=sqlite+aiosqlite:///./live.db\n")
        (self.root / "media").mkdir()
        (self.root / "media/music.mp3").write_bytes(b"untracked-media-sentinel")
        (self.root / "Archive").mkdir()
        (self.root / "Archive/history").write_text("untracked-history-sentinel")
        self.live = sqlite3.connect(self.root / "live.db")
        self.addCleanup(self.live.close)
        self.live.execute("PRAGMA journal_mode=WAL")
        self.live.execute("CREATE TABLE entries(value TEXT)")
        self.live.execute("INSERT INTO entries VALUES ('committed-in-wal')")
        self.live.commit()
        self.environment = patch.dict(os.environ, {
            "PYTHONPATH": str(REPO),
            "DATABASE_URL": "sqlite+aiosqlite:///./live.db",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout

    def test_small_snapshot_wal_integrity_permissions_and_no_secret_stdout(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            destination = backup.create_backup(self.root, "run-123-1")
        self.assertEqual(destination.parent, self.base / ".xass-predeploy")
        self.assertEqual((destination / ".env").read_bytes(), (self.root / ".env").read_bytes())
        self.assertEqual((destination / "before-revision").read_bytes(), self.git("rev-parse", "HEAD"))
        self.assertEqual(json.loads((destination / "COMPLETE.json").read_text())["database"], "sqlite")
        with tarfile.open(destination / "code.tar.gz") as archive:
            self.assertEqual(archive.getnames(), ["index.php"])
        with closing(sqlite3.connect(destination / "database/sqlite.db")) as restored:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT value FROM entries").fetchone()[0], "committed-in-wal")
        self.assertIn("READY", stdout.getvalue())
        self.assertNotIn("private-sentinel", stdout.getvalue())
        self.assertFalse((destination / "media").exists())
        if os.name == "posix":
            for path in [destination.parent, destination, destination / "database"]:
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            for path in destination.rglob("*"):
                if path.is_file():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stdin_invocation_does_not_need_new_script_on_server(self):
        result = subprocess.run([sys.executable, "-", "stdin-run"], cwd=self.root,
                                input=SCRIPT.read_text(), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("READY", result.stdout)
        self.assertNotIn("private-sentinel", result.stdout + result.stderr)
        self.assertTrue((self.base / ".xass-predeploy/stdin-run/COMPLETE.json").is_file())

    def test_invalid_id_and_existing_destination_never_overwrite(self):
        for invalid in ("../escape", "/root", "a/b", "a\\b", "..", "", "with space"):
            with self.subTest(invalid=invalid), self.assertRaises(backup.BackupError):
                backup.create_backup(self.root, invalid)
        destination = self.base / ".xass-predeploy/existing"
        destination.mkdir(parents=True)
        (destination / "keep").write_text("existing data")
        with self.assertRaises(FileExistsError):
            backup.create_backup(self.root, "existing")
        self.assertEqual((destination / "keep").read_text(), "existing data")

    def test_dirty_checkout_and_database_failure_never_mark_complete(self):
        (self.root / "index.php").write_text("changed")
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.root, "dirty")
        self.assertFalse((self.base / ".xass-predeploy/dirty/COMPLETE.json").exists())
        (self.root / "index.php").write_text("<?php echo 'tracked';")
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite+aiosqlite:///./missing.db"}):
            with self.assertRaises(backup.BackupError):
                backup.create_backup(self.root, "bad-db")
        self.assertFalse((self.base / ".xass-predeploy/bad-db/COMPLETE.json").exists())

    def test_subprocess_timeout_is_bounded_and_sanitized(self):
        started = time.monotonic()
        with self.assertRaisesRegex(backup.BackupError, "time limit"):
            backup._run([sys.executable, "-c", "import time; time.sleep(30)"], self.root, backup.Deadline(0.1))
        self.assertLess(time.monotonic() - started, 5)

    def test_interrupted_child_process_is_killed_and_reaped(self):
        process = MagicMock()
        process.__enter__.return_value = process
        process.communicate.side_effect = [KeyboardInterrupt(), (b"", b"")]
        with patch.object(backup.subprocess, "Popen", return_value=process):
            with patch.object(backup.os, "name", "nt"), self.assertRaises(KeyboardInterrupt):
                backup._run(["fixture"], self.root, backup.Deadline())
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)

    def test_symlink_backup_parent_is_rejected(self):
        target = self.base / "elsewhere"
        target.mkdir()
        try:
            (self.base / ".xass-predeploy").symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks needs Windows developer mode or elevated privileges")
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.root, "linked")
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
