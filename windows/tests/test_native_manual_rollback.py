"""Synthetic local snapshots only: no Windows process, registry or user data."""
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_updater import NativeUpdater, NativeRollback, APP_ID, digest, write_json, rollback_candidate, verify_tree

OLD = '1' * 40; NEW = '2' * 40

class ManualRollbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.local = Path(temp.name).resolve()
        env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.local)})
        env.start(); self.addCleanup(env.stop)
        self.updates = self.local / 'XASS.Native/updates'; self.updates.mkdir(parents=True)
        self.install = self.local / 'Programs/XASS'; self.install.mkdir(parents=True)
        self.profile = self.local / 'XASS/config.json'; self.profile.parent.mkdir(); self.profile.write_bytes(b'private-profile-marker')
        self.original = self.updates / ('job-' + 'a' * 32); self.original.mkdir()
        self.make_install(OLD, '1.0.0')
        installer = self.updates / ('XASS-Native-Test-' + NEW + '.exe'); installer.write_bytes(b'X' * 2048)
        request = dict(schema=2, job_id=self.original.name, install_root=str(self.install), installer=str(installer),
                       sha256=digest(installer), size=2048, version='1.1.0', revision=NEW, parent_pid=99999,
                       parent_created=1.5, automatic=False, release_distribution='native-test', allow_channel_change=False)
        write_json(self.original / 'request.json', request)
        updater = NativeUpdater(self.original / 'request.json'); updater.read_uninstall_registration = Mock(return_value={})
        updater.prepare(); self.make_install(NEW, '1.1.0'); updater.state('completed', 'test', ok=True)
        self.target = rollback_candidate(self.updates, self.install)
        self.job = self.updates / ('job-' + 'b' * 32); self.job.mkdir()
        self.request = dict(schema=3, job_id=self.job.name, install_root=str(self.install), parent_pid=99999,
                            parent_created=1.5, automatic=False, release_distribution=self.target['distribution'],
                            **{k: v for k, v in self.target.items() if k != 'distribution'})
        self.save()

    def make_install(self, revision, version):
        (self.install / 'runtime').mkdir(exist_ok=True)
        (self.install / 'Xass.Native.exe').write_text('native-' + revision)
        (self.install / 'runtime/XASS.NativeHelper.exe').write_bytes(b'helper')
        write_json(self.install / 'native-install.json', dict(app_id=APP_ID, revision=revision, version=version, distribution='native-test'))
        rows = [dict(path=p.relative_to(self.install).as_posix(), bytes=p.stat().st_size, sha256=digest(p))
                for p in self.install.rglob('*') if p.is_file() and p.name != 'payload-manifest.json']
        write_json(self.install / 'payload-manifest.json', dict(schema=1, revision=revision, files=rows))

    def save(self): write_json(self.job / 'request.json', self.request)
    def instance(self):
        updater = NativeRollback(self.job / 'request.json')
        updater.read_uninstall_registration = Mock(return_value={})
        updater.restore_uninstall_registration = Mock()
        updater.wait_for_parent = Mock(); updater.stop_install_processes = Mock()
        updater.verify_installed = Mock()
        return updater
    def revision(self): return json.loads((self.install / 'native-install.json').read_text())['revision']

    def test_manual_rollback_preserves_profile_and_both_snapshots(self):
        updater = self.instance(); self.assertEqual(updater.execute(), 0)
        self.assertEqual(self.revision(), OLD)
        self.assertEqual(self.profile.read_bytes(), b'private-profile-marker')
        self.assertTrue((self.original / 'backup/Xass.Native.exe').is_file())
        self.assertIn(NEW, (self.job / 'backup/Xass.Native.exe').read_text())
        updater.verify_installed.assert_called_once_with(OLD, distribution='native-test')
        self.assertEqual(rollback_candidate(self.updates, self.install)['revision'], NEW)

    def test_failed_old_runtime_restores_current_version(self):
        updater = self.instance(); updater.verify_installed.side_effect = [RuntimeError('old health failed'), None]
        self.assertEqual(updater.execute(), 3)
        self.assertEqual(self.revision(), NEW)
        self.assertEqual(self.profile.read_bytes(), b'private-profile-marker')
        state = json.loads((self.updates / 'last-result.json').read_text())
        self.assertTrue(state['rollback_ok']); self.assertNotIn('rejected_sha256', state)

    def test_cancel_does_not_replace_program(self):
        updater = self.instance(); (self.job / 'cancel').touch()
        self.assertEqual(updater.execute(), 2); self.assertEqual(self.revision(), NEW)

    def test_automatic_rollback_is_rejected_before_backup(self):
        self.request['automatic'] = True; self.save()
        with self.assertRaises(ValueError): NativeRollback(self.job / 'request.json')
        self.assertFalse((self.job / 'backup').exists())

    def test_changed_snapshot_hash_cannot_replace_reviewed_target(self):
        manifest = self.original / 'backup-manifest.json'; manifest.write_text(manifest.read_text() + ' ')
        with self.assertRaises(ValueError): NativeRollback(self.job / 'request.json')

    def test_corrupt_backup_is_rejected_before_shutdown(self):
        (self.original / 'backup/Xass.Native.exe').write_bytes(b'corrupt')
        with self.assertRaises(ValueError): self.instance()
        self.assertEqual(self.revision(), NEW)

    def test_snapshot_changed_after_preparation_is_rejected_without_replacement(self):
        updater = self.instance(); updater.prepare()
        (self.original / 'backup/Xass.Native.exe').write_bytes(b'corrupt')
        with self.assertRaises(ValueError): updater.install_release()
        updater.stop_install_processes.assert_not_called(); self.assertEqual(self.revision(), NEW)

    def test_unexpected_backup_file_is_rejected(self):
        (self.original / 'backup/unexpected.exe').write_bytes(b'bad')
        with self.assertRaises(ValueError): self.instance()

    def test_other_installation_cannot_reuse_backup(self):
        request = json.loads((self.original / 'request.json').read_text()); request['install_root'] = str(self.local / 'other')
        write_json(self.original / 'request.json', request)
        self.assertIsNone(rollback_candidate(self.updates, self.install))

    def test_only_last_completed_transaction_is_offered(self):
        state = json.loads((self.updates / 'last-result.json').read_text()); state['phase'] = 'failed'
        write_json(self.updates / 'last-result.json', state)
        self.assertIsNone(rollback_candidate(self.updates, self.install))
        with self.assertRaises(ValueError): self.instance()

    def test_changed_current_installation_rejects_old_preview(self):
        self.make_install('3' * 40, '1.2.0')
        with self.assertRaises(ValueError): self.instance()

    def test_unknown_schema_fields_and_channel_are_rejected(self):
        for key, value in [('schema', 2), ('release_distribution', 'native'), ('source_job', '../escape'), ('extra', True)]:
            saved = self.request.copy(); self.request[key] = value; self.save()
            with self.subTest(key=key), self.assertRaises(ValueError): self.instance()
            self.request = saved

    def test_symlink_snapshot_is_rejected(self):
        backup = self.original / 'backup'; moved = self.original / 'moved'; backup.rename(moved); backup.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError): self.instance()

    def test_cross_platform_inventory_paths_are_bounded(self):
        for relative in ['../outside', '/outside', 'C:/outside', 'C:\\outside', 'runtime/../bad', 'runtime//bad']:
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                verify_tree(self.install, {relative: {'sha256': '0' * 64, 'size': 0}})
