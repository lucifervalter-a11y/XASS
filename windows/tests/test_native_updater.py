"""Rollback failure injection uses synthetic folders and mocked installers only."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_updater import NativeUpdater, APP_ID, digest, copy_tree_verified, verify_tree, write_json, update_lock

OLD = "1" * 40; NEW = "2" * 40

class NativeUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA":str(self.local)})
        environment.start(); self.addCleanup(environment.stop)
        self.updates = self.local / "XASS.Native/updates"; self.updates.mkdir(parents=True)
        self.install = self.local / "Programs/XASS-Native-Test"; self.install.mkdir(parents=True)
        self.job = self.updates / ("job-" + "a"*32); self.job.mkdir()
        self.installer = self.updates / ("XASS-Native-Test-" + NEW + ".exe"); self.installer.write_bytes(b"X"*2048)
        self.make_install(OLD)
        self.request = {"schema":1,"job_id":self.job.name,"install_root":str(self.install),"installer":str(self.installer),
            "sha256":digest(self.installer),"size":2048,"version":"1.0.0","revision":NEW,"parent_pid":123456,
            "parent_created":500.5,"automatic":False}
        write_json(self.job / "request.json", self.request)
        self.updater = NativeUpdater(self.job / "request.json", run=Mock(return_value=SimpleNamespace(returncode=0,stdout=b'{"ok":true}')))
        # Unit fixtures must never inspect or rewrite a real user's installed registry.
        self.updater.read_uninstall_registration = Mock(return_value={})
        self.updater.restore_uninstall_registration = Mock()
    def make_install(self, revision):
        (self.install / "runtime").mkdir(exist_ok=True)
        (self.install / "Xass.Native.exe").write_bytes(("native"+revision).encode())
        (self.install / "runtime/XASS.NativeHelper.exe").write_bytes(b"helper")
        write_json(self.install / "native-install.json", {"app_id":APP_ID,"distribution":"native-test","revision":revision,"version":"1.0.0"})
        files = [{"path":p.relative_to(self.install).as_posix(),"bytes":p.stat().st_size,"sha256":digest(p)} for p in self.install.rglob("*") if p.is_file() and p.name != "payload-manifest.json"]
        write_json(self.install / "payload-manifest.json", {"schema":1,"revision":revision,"files":files})
    def test_rejects_tampered_installer_before_mutation(self):
        self.installer.write_bytes(b"tampered")
        with self.assertRaises(ValueError): NativeUpdater(self.job / "request.json")
        self.assertFalse((self.job / "backup").exists())
    def test_rejects_unknown_schema_fields(self):
        self.request["shell"] = "untrusted"; write_json(self.job / "request.json", self.request)
        with self.assertRaises(ValueError): NativeUpdater(self.job / "request.json")
    def test_rejects_non_native_installation(self):
        write_json(self.install / "native-install.json", {"app_id":"legacy","distribution":"stable"})
        with self.assertRaises(ValueError): NativeUpdater(self.job / "request.json")
    def test_rejects_installer_from_other_folder(self):
        other = self.local / self.installer.name; other.write_bytes(b"X"*2048)
        self.request["installer"] = str(other); write_json(self.job / "request.json", self.request)
        with self.assertRaises(ValueError): NativeUpdater(self.job / "request.json")
    def test_preparation_creates_verified_backup_before_ready(self):
        self.updater.prepare()
        self.assertTrue((self.job / "ready.json").is_file())
        verify_tree(self.job / "backup", self.updater.inventory)
        self.assertEqual((self.job / "backup/Xass.Native.exe").read_bytes(), (self.install / "Xass.Native.exe").read_bytes())
    def test_backup_failure_does_not_start_installer(self):
        with patch("native_updater.copy_tree_verified", side_effect=OSError("disk full")):
            code = self.updater.execute()
        self.assertEqual(code,1); self.updater.run.assert_not_called(); self.assertFalse(self.updater.applied)
        self.assertEqual(json.loads((self.job / "state.json").read_text())["phase"], "failed")
    def test_cancel_before_install_preserves_current_runtime(self):
        (self.job / "cancel").touch(); original=(self.install / "Xass.Native.exe").read_bytes()
        self.assertEqual(self.updater.execute(),2); self.updater.run.assert_not_called()
        self.assertEqual((self.install / "Xass.Native.exe").read_bytes(), original)
    def test_hash_verification_detects_corrupted_backup(self):
        self.updater.prepare(); (self.job / "backup/Xass.Native.exe").write_text("bad")
        with self.assertRaises(ValueError): verify_tree(self.job / "backup", self.updater.inventory)
    def test_symlinks_are_never_copied_as_installed_payload(self):
        (self.install / "escape").symlink_to(self.local, target_is_directory=True)
        with self.assertRaises(ValueError): self.updater.prepare()
    def test_restore_swaps_verified_old_tree_and_quarantines_failed_runtime(self):
        self.updater.prepare(); original=(self.install / "Xass.Native.exe").read_bytes()
        (self.install / "Xass.Native.exe").write_text("bad-update")
        self.updater.stop_install_processes = Mock(); self.updater.verify_installed=Mock()
        self.updater.restore()
        self.assertEqual((self.install / "Xass.Native.exe").read_bytes(), original)
        quarantines=list(self.install.parent.glob("XASS-Native-Test.failed-*")); self.assertEqual(len(quarantines),1)
        self.assertEqual((quarantines[0] / "Xass.Native.exe").read_text(), "bad-update")
        self.updater.verify_installed.assert_called_once_with(OLD)
    def test_failed_installer_is_rolled_back(self):
        self.updater.wait_for_parent=Mock(); self.updater.stop_install_processes=Mock(); self.updater.verify_installed=Mock()
        self.updater.run.return_value=SimpleNamespace(returncode=1)
        self.assertEqual(self.updater.execute(),3)
        state=json.loads((self.job / "state.json").read_text()); self.assertEqual(state["phase"],"rolled-back");self.assertTrue(state["rollback_ok"])
    def test_failed_health_is_rolled_back(self):
        self.updater.wait_for_parent=Mock(); self.updater.stop_install_processes=Mock()
        self.updater.verify_installed=Mock(side_effect=[RuntimeError("unhealthy new"),None])
        self.assertEqual(self.updater.execute(),3)
        self.assertEqual(self.updater.verify_installed.call_args_list[0].args,(NEW,)); self.assertEqual(self.updater.verify_installed.call_args_list[1].args,(OLD,))
    def test_failed_rollback_health_is_honest_and_keeps_backup(self):
        self.updater.wait_for_parent=Mock(); self.updater.stop_install_processes=Mock()
        self.updater.verify_installed=Mock(side_effect=RuntimeError("unhealthy"))
        self.assertEqual(self.updater.execute(),4); self.assertTrue((self.job / "backup").is_dir())
        self.assertEqual(json.loads((self.job / "state.json").read_text())["phase"],"rollback-failed")
    def test_payload_integrity_failure_prevents_launch(self):
        (self.install / "Xass.Native.exe").write_text("altered")
        self.updater.popen=Mock()
        with self.assertRaises(ValueError):self.updater.verify_installed(OLD)
        self.updater.popen.assert_not_called();self.updater.run.assert_not_called()
    def test_native_ready_ack_requires_expected_nonce_pid_revision(self):
        nonce="b"*32; child=Mock(pid=99);child.poll.return_value=None
        def launch(*args,**kwargs):
            write_json(self.job / ("health-"+nonce+".json"),{"ready":True,"nonce":nonce,"pid":99,"revision":OLD})
            return child
        self.updater.popen=launch
        with patch("native_updater.uuid.uuid4",return_value=SimpleNamespace(hex=nonce)):
            self.updater.verify_installed(OLD)
    def test_parent_pid_reuse_does_not_wait_for_unrelated_process(self):
        process=Mock();process.create_time.return_value=900
        self.updater.sleep=Mock()
        with patch("psutil.Process",return_value=process): self.updater.wait_for_parent()
        self.updater.sleep.assert_not_called()
    def test_update_lock_is_exclusive_and_released(self):
        lock=self.updates/"lock"
        with update_lock(lock):
            with self.assertRaises(OSError):
                with update_lock(lock):pass
        with update_lock(lock):pass
    def test_installer_command_has_no_shell_and_forces_original_directory(self):
        self.updater.stop_install_processes=Mock();self.updater.install_release()
        args=self.updater.run.call_args.args[0]
        self.assertIn("/DIR="+str(self.install),args);self.assertNotIn("shell",self.updater.run.call_args.kwargs)
        self.assertIn("/NORESTART",args)

if __name__=="__main__":unittest.main()

class NativeUpdaterUiContracts(unittest.TestCase):
    def test_auto_update_requires_separate_explicit_opt_in(self):
        root=Path(__file__).resolve().parents[1]/"Xass.Native"
        preferences=(root/"Services/NativeUpdatePreferences.cs").read_text()
        self.assertIn("automatic_enabled",preferences);self.assertIn("return false",preferences)
        window=(root/"MainWindow.Desktop.cs").read_text()
        self.assertIn("Включить нативное автообновление?",window)
        self.assertIn("AppWindow.IsVisible",window);self.assertIn('musicState is "playing"',window)
    def test_ui_health_is_backend_acknowledged_before_file_write(self):
        root=Path(__file__).resolve().parents[1]/"Xass.Native"
        window=(root/"MainWindow.Desktop.cs").read_text()
        section=window[window.index('arguments.Contains("--native-update-health"'):]
        self.assertLess(section.index('action = "host_status"'),section.index("WriteHealthAcknowledgmentAsync"))
        coordinator=(root/"Services/NativeUpdateCoordinator.cs").read_text()
        self.assertIn("CancellationTokenSource.CreateLinkedTokenSource",coordinator)
        self.assertIn('Path.Combine(job, "cancel")',coordinator)
