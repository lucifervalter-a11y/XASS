from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.agent_updates import build_agent_package, build_update_manifest, update_is_available


class AgentUpdateTests(unittest.TestCase):
    def test_runtime_secrets_never_enter_zip_or_change_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = root / "pc_client"
            client.mkdir()
            sources = {
                "client_agent.py": b"# source",
                "desktop_app.py": b"# desktop source",
                "version.json": b'{"version":"1.2.3"}',
                "assets/xass.ico": b"icon",
                "assets/data/theme.json": b'{"color":"purple"}',
                "templates/config.example.json": b'{"api_key":""}',
                "templates/config.json.example": b'{"api_key":""}',
                "templates/.env.example": b"AGENT_API_KEY=",
            }
            for name, data in sources.items():
                path = client / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            settings = SimpleNamespace(agent_update_cache_dir=str(root / "cache-clean"), agent_updates_enabled=True)
            with patch("app.services.agent_updates._client_root", return_value=client):
                clean = build_agent_package(settings)
                secrets = [
                    "config.json", "config.json.bak", "CONFIG.JSON.BAK.2", ".config.json.12.34.tmp",
                    ".xass-master.key", ".xass-master.key.bak", ".command-results.json",
                    "..command-results.json.12.34.tmp", ".agent-status.json.bak", ".update-result.json",
                    ".installed-revision", ".xass-managed-files.json", ".xass-archive-state.json",
                    "xass-archive.sqlite3-wal", "migration.json", "xass.log", "xass.log.1", ".env.local",
                    "data/.xass-master.key", "Archive/private-photo.jpg", "logs/debug.txt",
                    ".venv312/Lib/installed.py", "venv-test/Lib/installed.py", ".build-venv/secret",
                    ".updates/previous/config.json", "build/stale.exe", "dist/stale.exe",
                ]
                sentinel = b"SENTINEL_PRIVATE_AGENT_CREDENTIAL_NEVER_PUBLISH"
                for name in secrets:
                    path = client / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(sentinel)
                settings.agent_update_cache_dir = str(root / "cache-with-secrets")
                packaged = build_agent_package(settings)
                self.assertEqual(packaged.revision, clean.revision)
                with zipfile.ZipFile(packaged.path) as archive:
                    self.assertEqual(set(archive.namelist()), set(sources) | {".xass-managed-files.json"})
                    for name in archive.namelist():
                        self.assertNotIn(sentinel, archive.read(name))
                    managed = json.loads(archive.read(".xass-managed-files.json"))["files"]
                    self.assertEqual(set(managed), set(sources))
                for name in secrets:
                    (client / name).write_bytes(sentinel + b" changed")
                settings.agent_update_cache_dir = str(root / "cache-secrets-changed")
                self.assertEqual(build_agent_package(settings).revision, clean.revision)

    def test_update_comparison_never_downgrades_newer_client(self) -> None:
        self.assertFalse(
            update_is_available(
                current_version="0.13.2",
                current_revision="local-newer",
                published_version="0.13.1",
                published_revision="published-older",
            )
        )
        self.assertTrue(
            update_is_available(
                current_version="0.13.2",
                current_revision="old-build",
                published_version="0.13.2",
                published_revision="new-build",
            )
        )

    def test_package_is_versioned_and_excludes_runtime_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            settings = SimpleNamespace(agent_update_cache_dir=cache, agent_updates_enabled=True)
            package = build_agent_package(settings)
            expected_version = json.loads((Path(__file__).resolve().parents[1] / "pc_client" / "version.json").read_text(encoding="utf-8"))["version"]
            self.assertEqual(package.version, expected_version)
            self.assertEqual(len(package.sha256), 64)
            self.assertGreater(package.size, 0)
            with zipfile.ZipFile(package.path, "r") as archive:
                names = set(archive.namelist())
            self.assertIn("desktop_app.py", names)
            self.assertIn("client_agent.py", names)
            self.assertIn("bootstrap_dependencies.py", names)
            self.assertIn("connection_file.py", names)
            self.assertIn("archive_store.py", names)
            self.assertIn("remote_tools.py", names)
            self.assertIn("network_client.py", names)
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
