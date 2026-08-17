from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from pc_client.updater_helper import _health_check, _managed_files, _requirements_need_install


class UpdaterHelperTests(unittest.TestCase):
    def test_managed_cleanup_manifest_cannot_target_user_data(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / ".xass-managed-files.json"
            manifest.write_text(
                '{"files":["desktop_app.py","config.json",".venv/secret","../outside.txt"]}',
                encoding="utf-8",
            )
            self.assertEqual(_managed_files(manifest), {Path("desktop_app.py")})

    def test_health_check_rejects_version_mismatch_before_restart(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)
            (target / "version.json").write_text('{"version":"1.0.0"}', encoding="utf-8")
            for name in ("client_agent.py", "desktop_app.py", "client_update.py"):
                (target / name).write_text("value = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "version mismatch"):
                _health_check(target, "2.0.0")

    def test_unchanged_requirements_skip_dependency_install(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)
            requirements = target / "requirements.txt"
            requirements.write_text("httpx==0.28.1\n", encoding="utf-8")
            stamp = target / ".venv" / ".requirements.sha256"
            stamp.parent.mkdir(parents=True)
            stamp.write_text(hashlib.sha256(requirements.read_bytes()).hexdigest(), encoding="utf-8")

            self.assertFalse(_requirements_need_install(target, requirements))

            requirements.write_text("httpx==0.28.2\n", encoding="utf-8")
            self.assertTrue(_requirements_need_install(target, requirements))


if __name__ == "__main__":
    unittest.main()
