from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CLIENT_ROOT = ROOT / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from app.services.agent_commands import ALLOWED_AGENT_COMMANDS
from app.services.agent_installer import (
    build_installer_manifest,
    get_agent_installer,
    issue_installer_ticket,
    verify_installer_ticket,
)
import client_agent
import client_update
from client_update import verify_manifest


class AgentInstallerTests(unittest.TestCase):
    def test_installer_manifest_is_signed_and_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            installer_path = root / "XASS-Setup.exe"
            installer_path.write_bytes(b"MZ" + b"xass-installer" * 64)
            sha256 = hashlib.sha256(installer_path.read_bytes()).hexdigest()
            metadata_path = root / "XASS-Setup.json"
            metadata_path.write_text(
                json.dumps({"version": "0.6.0", "revision": "abc123", "sha256": sha256}),
                encoding="utf-8",
            )
            settings = SimpleNamespace(
                agent_installer_path=str(installer_path),
                agent_installer_metadata_path=str(metadata_path),
            )
            artifact = get_agent_installer(settings)
            self.assertIsNotNone(artifact)
            manifest = build_installer_manifest(
                settings,
                api_key="agent-secret",
                base_url="https://xass.example",
                current_version="0.5.0",
                current_revision="old",
            )
            self.assertIsNotNone(manifest)
            self.assertTrue(manifest["available"])
            self.assertTrue(verify_manifest(manifest, "agent-secret"))
            self.assertTrue(str(manifest["url"]).endswith("/agent/installer/abc123.exe"))

    def test_installer_without_expected_checksum_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            installer_path = root / "XASS-Setup.exe"
            installer_path.write_bytes(b"MZ-test")
            metadata_path = root / "XASS-Setup.json"
            metadata_path.write_text(json.dumps({"version": "0.6.0", "revision": "abc123"}), encoding="utf-8")
            settings = SimpleNamespace(
                agent_installer_path=str(installer_path),
                agent_installer_metadata_path=str(metadata_path),
            )
            self.assertIsNone(get_agent_installer(settings))

    def test_short_lived_download_ticket_is_bound_to_installer_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            installer_path = root / "XASS-Setup.exe"
            installer_path.write_bytes(b"MZ-ticket-test")
            sha256 = hashlib.sha256(installer_path.read_bytes()).hexdigest()
            metadata_path = root / "XASS-Setup.json"
            metadata_path.write_text(
                json.dumps({"version": "0.8.0", "revision": "rev-080", "sha256": sha256}),
                encoding="utf-8",
            )
            settings = SimpleNamespace(
                agent_installer_path=str(installer_path),
                agent_installer_metadata_path=str(metadata_path),
                bot_token="ticket-secret",
                setup_api_key="setup-secret",
                agent_api_key="agent-secret",
            )
            ticket = issue_installer_ticket(settings, user_id=42)
            self.assertTrue(verify_installer_ticket(settings, ticket))
            self.assertFalse(verify_installer_ticket(settings, ticket + "tampered"))

            metadata_path.write_text(
                json.dumps({"version": "0.8.1", "revision": "rev-081", "sha256": sha256}),
                encoding="utf-8",
            )
            self.assertFalse(verify_installer_ticket(settings, ticket))

    @unittest.skipUnless(sys.platform == "win32", "Windows updater test")
    def test_updater_is_copied_outside_the_install_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            installed_exe = root / "installed" / "XASS.exe"
            bundled_helper = installed_exe.with_name("XASSUpdater.exe")
            installer = root / "XASS-Setup.exe"
            bundled_helper.parent.mkdir(parents=True)
            installed_exe.write_bytes(b"app")
            bundled_helper.write_bytes(b"helper")
            installer.write_bytes(b"installer")
            update_root = root / "updates"
            with patch.object(client_update, "is_installer_build", return_value=True), patch.object(
                client_update, "UPDATE_ROOT", update_root
            ), patch.object(client_update.sys, "executable", str(installed_exe)), patch.object(
                client_update, "uuid4", return_value=SimpleNamespace(hex="fixed")
            ), patch.object(client_update.subprocess, "Popen") as popen:
                client_update.launch_installer_update(installer, wait_pid=123)
            detached_helper = update_root / "helpers" / "XASSUpdater-fixed.exe"
            self.assertEqual(detached_helper.read_bytes(), b"helper")
            self.assertEqual(popen.call_args.args[0][0], str(detached_helper))
            self.assertIn("--installer", popen.call_args.args[0])

    def test_remote_lock_is_an_allowed_agent_command(self) -> None:
        self.assertIn("lock", ALLOWED_AGENT_COMMANDS)

    @unittest.skipUnless(sys.platform == "win32", "Windows API test")
    def test_lock_command_uses_windows_lock_workstation(self) -> None:
        class FakeLock:
            argtypes = None
            restype = None

            def __call__(self) -> bool:
                return True

        fake_user32 = SimpleNamespace(LockWorkStation=FakeLock())
        with patch.object(client_agent.ctypes, "WinDLL", return_value=fake_user32), patch.object(
            client_agent, "store_command_result"
        ) as store:
            client_agent._lock_workstation(42)
        store.assert_called_once_with(42, True, "Экран Windows заблокирован")


if __name__ == "__main__":
    unittest.main()
