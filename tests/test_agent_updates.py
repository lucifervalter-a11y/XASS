from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from app.services.agent_updates import build_agent_package, build_update_manifest


class AgentUpdateTests(unittest.TestCase):
    def test_package_is_versioned_and_excludes_runtime_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            settings = SimpleNamespace(agent_update_cache_dir=cache, agent_updates_enabled=True)
            package = build_agent_package(settings)
            self.assertEqual(package.version, "0.11.0")
            self.assertEqual(len(package.sha256), 64)
            self.assertGreater(package.size, 0)
            with zipfile.ZipFile(package.path, "r") as archive:
                names = set(archive.namelist())
            self.assertIn("desktop_app.py", names)
            self.assertIn("client_agent.py", names)
            self.assertIn("bootstrap_dependencies.py", names)
            self.assertIn("connection_file.py", names)
            self.assertIn("archive_store.py", names)
            self.assertIn(".xass-managed-files.json", names)
            self.assertNotIn("config.json", names)
            self.assertFalse(any(name.startswith(".venv/") for name in names))

    def test_manifest_is_signed_and_detects_outdated_client(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            settings = SimpleNamespace(agent_update_cache_dir=cache, agent_updates_enabled=True)
            manifest = build_update_manifest(
                settings,
                api_key="agent-secret",
                base_url="https://xass.example",
                current_version="0.0.0",
                current_revision="",
            )
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertTrue(manifest["available"])
            self.assertEqual(len(str(manifest["signature"])), 64)
            self.assertTrue(str(manifest["url"]).startswith("https://xass.example/agent/update/package"))
            self.assertTrue(str(manifest["url"]).endswith(f"/{manifest['revision']}.zip"))

            current = build_update_manifest(
                settings,
                api_key="agent-secret",
                base_url="https://xass.example",
                current_version=str(manifest["version"]),
                current_revision=str(manifest["revision"]),
            )
            self.assertIsNotNone(current)
            assert current is not None
            self.assertFalse(current["available"])


if __name__ == "__main__":
    unittest.main()
