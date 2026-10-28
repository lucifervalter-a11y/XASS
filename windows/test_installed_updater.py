"""Ephemeral Actions-only real installer/recovery smoke; no user machine or data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_updater import APP_ID, NativeUpdater, digest, write_json


class ForcedPostInstallHealthFailure(NativeUpdater):
    """Injection belongs to this CI driver, never the shipped updater role."""
    def verify_installed(self, revision):
        checks = getattr(self, '_smoke_health_checks', 0) + 1
        self._smoke_health_checks = checks
        if checks == 1:
            import winreg
            key_path = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{' + APP_ID + '}_is1'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, 'DisplayName', 0, winreg.REG_SZ, 'XASS Native CI injected health failure')
            raise RuntimeError('CI-injected post-install health failure')
        super().verify_installed(revision)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed', type=Path, required=True)
    parser.add_argument('--installer', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--target-metadata', type=Path)
    args = parser.parse_args()
    if os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true' or not os.environ.get('RUNNER_TEMP'):
        raise SystemExit('Requires an ephemeral GitHub Actions Windows runner')
    installed = args.installed.resolve(strict=True)
    if not installed.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve(strict=True)):
        raise SystemExit('Smoke installation must be within this ephemeral runner temp folder')
    identity = json.loads((installed / 'native-install.json').read_text())
    target = json.loads(args.target_metadata.read_text()) if args.target_metadata else identity
    revision = target['revision']
    updates = Path(os.environ['LOCALAPPDATA']) / 'XASS.Native' / 'updates'
    updates.mkdir(parents=True, exist_ok=True)
    prefixes = {'native': 'XASS-Native-', 'native-test': 'XASS-Native-Test-'}
    installer = updates / (prefixes[target['distribution']] + revision + '.exe')
    if installer.exists():
        if digest(installer) != digest(args.installer.resolve(strict=True)):
            raise SystemExit('Do not overwrite a different updater download')
    else:
        shutil.copyfile(args.installer.resolve(strict=True), installer)
    if args.target_metadata and (digest(installer) != target['sha256'] or installer.stat().st_size != target['size']):
        raise SystemExit('Target installer does not match its metadata')
    marker = updates.parent / ('ci-preserve-' + uuid.uuid4().hex + '.txt')
    marker.write_text('ephemeral-ci-marker')
    report = {'schema': 1, 'revision': revision, 'same_version': identity['revision'] == revision,
              'from_distribution': identity['distribution'], 'to_distribution': target['distribution']}
    try:
        for label, kind, expected in (
            ('injected_failure_and_actual_rollback', ForcedPostInstallHealthFailure, 3),
            ('verified_install_and_native_readiness', NativeUpdater, 0),
        ):
            job = updates / ('job-' + uuid.uuid4().hex)
            job.mkdir()
            request = {'schema': 1, 'job_id': job.name, 'install_root': str(installed), 'installer': str(installer),
                       'sha256': digest(installer), 'size': installer.stat().st_size, 'version': target['version'],
                       'revision': revision, 'parent_pid': os.getpid(), 'parent_created': 1.0, 'automatic': False}
            write_json(job / 'request.json', request)
            updater = kind(job / 'request.json')
            # The test driver replaces the parent UI. Real readiness is still
            # required from the actually installed WinUI and backend process.
            updater.wait_for_parent = lambda: None
            try:
                code = updater.execute()
                if code != expected:
                    phase = json.loads((job / 'state.json').read_text()).get('phase', 'unknown')
                    raise RuntimeError(f'{label}: expected exit {expected}, got {code}, phase {phase}')
                if marker.read_text() != 'ephemeral-ci-marker':
                    raise RuntimeError('Updater changed an out-of-install user-data marker')
                registration = updater.read_uninstall_registration()
                if not registration:
                    raise RuntimeError('Per-user uninstall registration was lost')
                if expected == 3 and any(registration.get(key) != value for key, value in updater.registry.items()):
                    raise RuntimeError('Per-user uninstall registration was not restored')
                if registration.get('InstallLocation') != updater.registry.get('InstallLocation'):
                    raise RuntimeError('Update changed the registered installation directory')
                report[label] = 'passed'
            finally:
                # Stop only executables under this exact ephemeral install root.
                updater.stop_install_processes()
            actual = json.loads((installed / 'native-install.json').read_text())
            wanted = identity if expected == 3 else target
            if any(actual[key] != wanted[key] for key in ('revision', 'distribution', 'version')):
                raise RuntimeError('Installed identity does not match the expected update/rollback result')
        args.report.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.report, report)
        print('Real installed update, native readiness, channel identity and injected-failure rollback passed.')
    finally:
        marker.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
