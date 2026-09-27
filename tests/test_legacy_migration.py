from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pc_client.legacy_migration import migrate_config, run_migration


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

    def test_keeps_sealed_installed_config_and_key_untouched(self) -> None:
        for envelope in (
            {"cipher": "dpapi", "data": "synthetic-encrypted-fixture"},
            {"cipher": "aes-256-gcm", "nonce": "synthetic-nonce", "data": "synthetic-encrypted-fixture"},
        ):
            with self.subTest(cipher=envelope["cipher"]), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                legacy, data, startup = root / "legacy", root / "data", root / "startup"
                legacy.mkdir(); data.mkdir(); startup.mkdir()
                (legacy / "config.json").write_text(json.dumps({
                    "server_url": "https://old.example", "api_key": "synthetic-stale-key",
                }), encoding="utf-8")
                current = json.dumps({
                    "server_url": "https://new.example", "source_name": "Current PC",
                    "format": "xass-config", "version": 2, "sealed": envelope,
                }, indent=2).encode("utf-8")
                (data / "config.json").write_bytes(current)
                key = data / ".xass-master.key"
                key.write_bytes(b"synthetic-key-must-not-change")
                with patch("pc_client.legacy_migration.stop_legacy_processes", return_value=0), \
                        patch("pc_client.legacy_migration.shutil.copy2") as copy:
                    result = run_migration(data_root=data, legacy_roots=[legacy], startup_dir=startup)
                self.assertFalse(result.config_migrated)
                copy.assert_not_called()
                self.assertEqual((data / "config.json").read_bytes(), current)
                self.assertEqual(key.read_bytes(), b"synthetic-key-must-not-change")
                self.assertFalse((data / "config.json.migrating").exists())

    def test_does_not_blindly_copy_sealed_source_without_its_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "config.json").write_text(json.dumps({
                "server_url": "https://old.example", "format": "xass-config", "version": 2,
                "sealed": {"cipher": "aes-256-gcm", "nonce": "fixture", "data": "fixture"},
            }), encoding="utf-8")
            destination = root / "new" / "config.json"
            self.assertEqual(migrate_config([legacy], destination), "")
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
