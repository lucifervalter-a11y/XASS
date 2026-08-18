from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsBuildScriptTests(unittest.TestCase):
    def test_installer_build_pins_python_and_runs_frozen_health_check(self) -> None:
        script = (ROOT / "pc_client" / "build_installer.ps1").read_text(encoding="utf-8")
        self.assertIn("sys.version_info[:2] == (3, 12)", script)
        self.assertIn("--health-check --expected-version $Version", script)
        self.assertIn("installer will not be published", script)


if __name__ == "__main__":
    unittest.main()
