from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from pc_client.updater_helper import _requirements_need_install


class UpdaterHelperTests(unittest.TestCase):
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
