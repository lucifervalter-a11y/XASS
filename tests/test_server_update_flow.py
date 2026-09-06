from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import updater


class ServerUpdateFlowTests(unittest.TestCase):
    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()

    def commit(self, cwd: Path, filename: str, content: str) -> str:
        (cwd / filename).write_text(content)
        self.git(cwd, "add", filename)
        self.git(cwd, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", content)
        return self.git(cwd, "rev-parse", "HEAD")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote, self.seed, self.app = (self.root / name for name in ("remote.git", "seed", "app"))
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.seed))
        self.initial = self.commit(self.seed, "version.txt", "initial")
        self.git(self.seed, "push", "origin", "main")
        self.git(self.root, "clone", str(self.remote), str(self.app))
        self.settings = SimpleNamespace(update_branch="main", service_restart_mode="none", update_log_path="", update_state_path="")
        patcher = patch.object(updater, "_repo_root", return_value=self.app)
        patcher.start()
        self.addCleanup(patcher.stop)

    def publish(self, content: str = "remote update") -> str:
        revision = self.commit(self.seed, "version.txt", content)
        self.git(self.seed, "push", "origin", "main")
        return revision

    def status(self):
        return updater.get_update_status(self.settings, include_release_notes=False)

    def test_remote_update_fast_forwards_and_reports_actual_changed_files(self) -> None:
        target = self.publish()
        self.assertTrue(self.status().has_updates)
        result = updater.run_update(self.settings, execute_restart=False)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.after.full_hash, target)
        self.assertEqual(result.changed_files, ["version.txt"])
        self.assertFalse(self.status().has_updates)

    def test_local_ahead_is_not_mistaken_for_an_update(self) -> None:
        local = self.commit(self.app, "local.txt", "local change")
        self.assertFalse(self.status().has_updates)
        result = updater.run_update(self.settings, execute_restart=False)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.changed_files, [])
        self.assertEqual(self.git(self.app, "rev-parse", "HEAD"), local)

    def test_diverged_history_is_reported_and_left_untouched(self) -> None:
        self.publish()
        local = self.commit(self.app, "local.txt", "local change")
        status = self.status()
        self.assertFalse(status.has_updates)
        self.assertTrue(status.errors)
        result = updater.run_update(self.settings, execute_restart=False)
        self.assertFalse(result.ok)
        self.assertEqual(self.git(self.app, "rev-parse", "HEAD"), local)

    def test_failed_fetch_does_not_install_stale_update_or_report_success(self) -> None:
        self.publish()
        self.status()  # Populate the local remote ref before losing connectivity.
        with patch.object(updater, "fetch_remote", return_value=False):
            status = self.status()
            result = updater.run_update(self.settings, execute_restart=False)
        self.assertFalse(status.has_updates)
        self.assertTrue(status.errors)
        self.assertFalse(result.ok)
        self.assertEqual(self.git(self.app, "rev-parse", "HEAD"), self.initial)

    def test_new_remote_commit_during_install_is_left_for_next_update(self) -> None:
        target = self.publish()
        original_diff = updater.get_changed_files_between

        def publish_during_inspection(*args, **kwargs):
            changed = original_diff(*args, **kwargs)
            self.publish("later update")
            return changed

        with patch.object(updater, "get_changed_files_between", side_effect=publish_during_inspection):
            result = updater.run_update(self.settings, execute_restart=False)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.after.full_hash, target)
        self.assertTrue(self.status().has_updates)


if __name__ == "__main__":
    unittest.main()
