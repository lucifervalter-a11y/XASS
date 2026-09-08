from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/repair_public_permissions.py"
spec = importlib.util.spec_from_file_location("repair_public_permissions", SCRIPT)
permissions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(permissions)


class PublicPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout"
        self.root.mkdir()

    def add(self, relative):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture")
        return target

    def test_only_tracked_public_files_and_their_internal_parents_are_changed(self):
        public = ["miniapp.php", "projects/index.php", "assets/nested/app.js", "manifest.webmanifest", "sw.js"]
        private = [".env", "data/config.json", "data/avatar.jpg", "backups/site.php", "tests/test_proxy.php", "proxy.config.php"]
        for name in public + private:
            self.add(name)
        # proxy.config.php is untracked and must remain private even at the root.
        listing = "\0".join(public + private[:-1]).encode() + b"\0"
        with patch.object(permissions.subprocess, "run", return_value=SimpleNamespace(stdout=listing)):
            with patch.object(Path, "chmod", autospec=True) as chmod:
                self.assertEqual(permissions.repair_public_permissions(self.root), (5, 3))
        actual = {(call.args[0], call.args[1]) for call in chmod.call_args_list}
        expected = {(self.root / name, 0o644) for name in public}
        expected |= {(self.root / name, 0o755) for name in ["projects", "assets", "assets/nested"]}
        self.assertEqual(actual, expected)

    def test_symlinked_public_parent_is_rejected_before_any_chmod(self):
        external = self.root.parent / "private"
        external.mkdir()
        (external / "secret.js").write_text("secret")
        self.add("miniapp.php")
        try:
            (self.root / "assets").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks needs Windows developer mode or privileges")
        with patch.object(permissions.subprocess, "run", return_value=SimpleNamespace(stdout=b"miniapp.php\0assets/secret.js\0")):
            with patch.object(Path, "chmod", autospec=True) as chmod:
                with self.assertRaises(ValueError):
                    permissions.repair_public_permissions(self.root)
                chmod.assert_not_called()

    def test_public_hardlink_cannot_change_private_target_permissions(self):
        original = self.add(".env")
        (self.root / "assets").mkdir()
        os.link(original, self.root / "assets/config.js")
        with patch.object(permissions.subprocess, "run", return_value=SimpleNamespace(stdout=b"assets/config.js\0")):
            with patch.object(Path, "chmod", autospec=True) as chmod:
                with self.assertRaises(ValueError):
                    permissions.repair_public_permissions(self.root)
                chmod.assert_not_called()


if __name__ == "__main__":
    unittest.main()
