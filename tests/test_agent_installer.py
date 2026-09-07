from __future__ import annotations

import hashlib
import json
import os
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
import installer_helper
from client_update import verify_manifest
from deploy import publish_installer


class AgentInstallerTests(unittest.TestCase):
    def test_installer_runtime_backup_can_restore_previous_binary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            installed = root / "Programs" / "XASS"
            updates = root / "data" / ".updates"
            installed.mkdir(parents=True)
            (installed / "XASS.exe").write_bytes(b"previous")
            backup = installer_helper._backup_installed_runtime(installed, updates)
            self.assertIsNotNone(backup)
            (installed / "XASS.exe").write_bytes(b"broken")
            self.assertTrue(installer_helper._restore_installed_runtime(installed, backup))
            self.assertEqual((installed / "XASS.exe").read_bytes(), b"previous")

    def test_failed_installer_restores_and_launches_previous_binary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            installed = root / "Programs" / "XASS"
            updates = root / "data" / ".updates"
            installer = root / "XASS-Setup.exe"
            installed.mkdir(parents=True)
            (installed / "XASS.exe").write_bytes(b"previous")
            installer.write_bytes(b"broken-installer")
            with patch.object(installer_helper.os, "name", "nt"), patch.object(
                installer_helper, "_wait_for_process"
            ), patch.object(installer_helper, "_install_paths", return_value=(installed, updates)), patch.object(
                installer_helper.subprocess, "run", return_value=SimpleNamespace(returncode=5)
            ), patch.object(installer_helper.subprocess, "Popen") as popen:
                result = installer_helper.install_update(installer, 123)
            self.assertEqual(result, 5)
            self.assertEqual((installed / "XASS.exe").read_bytes(), b"previous")
            popen.assert_called_once_with(
                [str(installed / "XASS.exe")], cwd=str(installed), close_fds=True
            )

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

            newer = build_installer_manifest(
                settings,
                api_key="agent-secret",
                base_url="https://xass.example",
                current_version="0.7.0",
                current_revision="newer-client",
            )
            self.assertIsNotNone(newer)
            assert newer is not None
            self.assertFalse(newer["available"])

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
            ), patch.object(
                client_update, "UPDATE_MARKER", update_root / ".in-progress"
            ), patch.object(
                client_update, "UPDATE_RESULT", update_root / ".last-result.json"
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
        self.assertIn("migration_download", ALLOWED_AGENT_COMMANDS)

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


class ImmutableInstallerPublicationTests(unittest.TestCase):
    REVISION = "a" * 40

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "pc_client").mkdir()
        (self.root / "pc_client" / "version.json").write_text(
            json.dumps({"version": "0.14.0"}), encoding="utf-8"
        )
        self.stage = self.root / "data" / "releases" / f".incoming-{self.REVISION}"
        self.stage.mkdir(parents=True)
        self.binary = self.root / "custom" / "downloads" / "XASS-Setup.exe"
        self.metadata_path = self.root / "custom" / "metadata" / "installer.json"
        self.binary.parent.mkdir(parents=True)
        self.metadata_path.parent.mkdir(parents=True)
        self.settings = SimpleNamespace(
            agent_installer_path=str(self.binary),
            agent_installer_metadata_path=str(self.metadata_path),
        )

    def stage_installer(self, data: bytes = b"MZ-new-installer", **overrides) -> dict:
        (self.stage / "XASS-Setup.exe").write_bytes(data)
        metadata = {
            "version": "0.14.0", "revision": self.REVISION,
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "local_build": False,
        }
        metadata.update(overrides)
        (self.stage / "XASS-Setup.json").write_text(json.dumps(metadata), encoding="utf-8")
        return metadata

    def publish(self, revision: str | None = None) -> dict:
        return publish_installer.publish_installer(
            self.root, self.stage, revision or self.REVISION, settings=self.settings
        )

    def install_legacy(self) -> bytes:
        self.binary.write_bytes(b"MZ-legacy-installer")
        metadata = json.dumps({
            "version": "0.13.0", "revision": "legacy-revision",
            "sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
        }).encode("utf-8")
        self.metadata_path.write_bytes(metadata)
        return metadata

    def test_immutable_metadata_does_not_require_legacy_binary(self) -> None:
        staged = self.stage_installer()
        result = self.publish()
        self.assertFalse(self.binary.exists())
        artifact = get_agent_installer(self.settings)
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.path, self.binary.parent / f"XASS-Setup-{staged['sha256']}.exe")
        self.assertTrue(artifact.path.is_absolute())
        self.assertEqual(result["artifact_file"], artifact.path.name)

    def test_invalid_artifact_names_and_sizes_are_not_served(self) -> None:
        self.install_legacy()
        metadata = json.loads(self.metadata_path.read_bytes())
        for name in ("../XASS-Setup.exe", "sub/XASS-Setup.exe", "sub\\XASS-Setup.exe",
                     str(self.binary), "XASS-Setup.exe", "", None,
                     f"XASS-Setup-{'f' * 64}.exe"):
            with self.subTest(name=name):
                self.metadata_path.write_text(json.dumps({**metadata, "artifact_file": name}), encoding="utf-8")
                self.assertIsNone(get_agent_installer(self.settings))
        for size in (1, True, "18"):
            self.metadata_path.write_text(json.dumps({**metadata, "size": size}), encoding="utf-8")
            self.assertIsNone(get_agent_installer(self.settings))
        self.metadata_path.write_text("[]", encoding="utf-8")
        self.assertIsNone(get_agent_installer(self.settings))

    def test_publications_keep_captured_paths_and_previous_metadata_unchanged(self) -> None:
        legacy_metadata = self.install_legacy()
        legacy = get_agent_installer(self.settings)
        self.stage_installer()
        self.publish()
        previous_path = self.metadata_path.with_name(self.metadata_path.name + ".previous")
        self.assertEqual(previous_path.read_bytes(), legacy_metadata)
        first_metadata = self.metadata_path.read_bytes()
        first = get_agent_installer(self.settings)
        first_bytes = first.path.read_bytes()
        self.stage_installer(b"MZ-second-installer", revision="b" * 40)
        atomic_bytes = publish_installer._atomic_bytes

        def inspect_before_switch(target: Path, content: bytes) -> None:
            if target == self.metadata_path:
                pending = json.loads(content)
                self.assertTrue((self.binary.parent / pending["artifact_file"]).is_file())
                self.assertEqual(get_agent_installer(self.settings), first)
            atomic_bytes(target, content)

        with patch.object(publish_installer, "_atomic_bytes", side_effect=inspect_before_switch):
            self.publish("b" * 40)
        second = get_agent_installer(self.settings)
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(first.path.read_bytes(), first_bytes)
        self.assertEqual(legacy.path.read_bytes(), b"MZ-legacy-installer")
        self.assertEqual(previous_path.read_bytes(), first_metadata)
        self.publish("b" * 40)
        self.assertEqual(previous_path.read_bytes(), first_metadata, "Idempotent publish must retain rollback")

    def test_invalid_staging_preserves_current_installer_and_metadata(self) -> None:
        self.stage_installer()
        self.publish()
        current = get_agent_installer(self.settings)
        metadata_before = self.metadata_path.read_bytes()
        cases = (
            {"sha256": "f" * 64}, {"size": 12345}, {"size": True},
            {"revision": "b" * 40}, {"revision": self.REVISION + "-dirty"},
            {"local_build": True}, {"version": "0.15.0"},
        )
        for values in cases:
            with self.subTest(values=values):
                self.stage_installer(b"MZ-invalid-staging", **values)
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.metadata_path.read_bytes(), metadata_before)
                self.assertEqual(get_agent_installer(self.settings), current)
        self.stage_installer(b"not-an-exe")
        with self.assertRaisesRegex(ValueError, "Windows executable"):
            self.publish()
        (self.stage / "XASS-Setup.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.metadata_path.read_bytes(), metadata_before)
        self.assertEqual(get_agent_installer(self.settings), current)

    def test_existing_different_immutable_bytes_are_never_overwritten(self) -> None:
        original = self.install_legacy()
        metadata = self.stage_installer()
        target = self.binary.parent / f"XASS-Setup-{metadata['sha256']}.exe"
        target.write_bytes(b"conflicting-existing-file")
        with self.assertRaisesRegex(ValueError, "not overwritten"):
            self.publish()
        self.assertEqual(target.read_bytes(), b"conflicting-existing-file")
        self.assertEqual(self.metadata_path.read_bytes(), original)

    def test_failed_metadata_switch_keeps_current_and_allows_retry(self) -> None:
        original = self.install_legacy()
        self.stage_installer()
        atomic_bytes = publish_installer._atomic_bytes

        def fail_switch(target: Path, content: bytes) -> None:
            if target == self.metadata_path:
                raise OSError("simulated replace failure")
            atomic_bytes(target, content)

        with patch.object(publish_installer, "_atomic_bytes", side_effect=fail_switch):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual(self.metadata_path.read_bytes(), original)
        self.assertEqual(get_agent_installer(self.settings).revision, "legacy-revision")
        self.publish()
        self.assertEqual(get_agent_installer(self.settings).revision, self.REVISION)

    def test_expected_revision_rejects_dirty_and_short_builds(self) -> None:
        self.stage_installer()
        for revision in ("abc123", self.REVISION + "-dirty", "../x", "f" * 41):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.publish(revision)
        self.assertFalse(self.metadata_path.exists())

    def test_settings_are_loaded_from_root_and_relative_paths_stay_there(self) -> None:
        self.stage_installer()
        (self.root / ".env").write_text(
            "AGENT_INSTALLER_PATH=custom/downloads/XASS-Setup.exe\n"
            "AGENT_INSTALLER_METADATA_PATH=custom/metadata/installer.json\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            publish_installer.publish_installer(self.root, self.stage.relative_to(self.root), self.REVISION)
        self.assertEqual(get_agent_installer(self.settings).revision, self.REVISION)

    def test_staging_change_during_copy_does_not_publish(self) -> None:
        original = self.install_legacy()
        metadata = self.stage_installer()
        digest = publish_installer._file_digest

        def change_after_initial_hash(path: Path) -> tuple[str, int]:
            result = digest(path)
            if path == self.stage / "XASS-Setup.exe":
                path.write_bytes(b"MZ-changed-upload")
            return result

        with patch.object(publish_installer, "_file_digest", side_effect=change_after_initial_hash):
            with self.assertRaisesRegex(ValueError, "changed during publication"):
                self.publish()
        self.assertEqual(self.metadata_path.read_bytes(), original)
        self.assertFalse((self.binary.parent / f"XASS-Setup-{metadata['sha256']}.exe").exists())


if __name__ == "__main__":
    unittest.main()
