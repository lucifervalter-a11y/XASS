from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pc_client.legacy_migration import run_migration


class LegacyMigrationTests(unittest.TestCase):
    def test_migrates_config_and_removes_only_known_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "Documents" / "xass" / "pc_client"
            data = root / "Local" / "XASS"
            startup = root / "Startup"
            legacy.mkdir(parents=True)
            startup.mkdir(parents=True)
            config = {"server_url": "https://xass.example", "api_key": "secret", "source_name": "PC"}
            (legacy / "config.json").write_text(json.dumps(config), encoding="utf-8")
            (legacy / "client_agent.py").write_text("# keep source", encoding="utf-8")
            (legacy / ".command-results.json").write_text("{}", encoding="utf-8")
            (legacy / ".updates").mkdir()
            (legacy / ".updates" / "old.exe").write_bytes(b"old")
            (startup / "ServerredusPCAgent.vbs").write_text("old", encoding="utf-8")

            with patch("pc_client.legacy_migration.stop_legacy_processes", return_value=1):
                result = run_migration(data_root=data, legacy_roots=[legacy], startup_dir=startup)

            self.assertTrue(result.config_migrated)
            self.assertEqual(json.loads((data / "config.json").read_text(encoding="utf-8")), config)
            self.assertFalse((startup / "ServerredusPCAgent.vbs").exists())
            self.assertFalse((legacy / ".updates").exists())
            self.assertTrue((legacy / "client_agent.py").exists())
            self.assertTrue((legacy / "config.json").exists())
            self.assertTrue((data / "migration.json").is_file())

    def test_keeps_valid_installed_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy, data, startup = root / "legacy", root / "data", root / "startup"
            legacy.mkdir(); data.mkdir(); startup.mkdir()
            old = {"server_url": "https://old.example", "api_key": "old"}
            current = {"server_url": "https://new.example", "api_key": "new"}
            (legacy / "config.json").write_text(json.dumps(old), encoding="utf-8")
            (data / "config.json").write_text(json.dumps(current), encoding="utf-8")
            with patch("pc_client.legacy_migration.stop_legacy_processes", return_value=0):
                result = run_migration(data_root=data, legacy_roots=[legacy], startup_dir=startup)
            self.assertFalse(result.config_migrated)
            self.assertEqual(json.loads((data / "config.json").read_text(encoding="utf-8")), current)


if __name__ == "__main__":
    unittest.main()
