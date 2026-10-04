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
    args = parser.parse_args()
    if os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true' or not os.environ.get('RUNNER_TEMP'):
        raise SystemExit('Requires an ephemeral GitHub Actions Windows runner')
    installed = args.installed.resolve(strict=True)
    if not installed.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve(strict=True)):
        raise SystemExit('Smoke installation must be within this ephemeral runner temp folder')
    identity = json.loads((installed / 'native-install.json').read_text())
    revision = identity['revision']
    updates = Path(os.environ['LOCALAPPDATA']) / 'XASS.Native' / 'updates'
    updates.mkdir(parents=True, exist_ok=True)
    installer = updates / ('XASS-Native-Test-' + revision + '.exe')
    if installer.exists():
        raise SystemExit('Do not overwrite an existing updater download')
    shutil.copyfile(args.installer.resolve(strict=True), installer)
    marker = updates.parent / ('ci-preserve-' + uuid.uuid4().hex + '.txt')
    marker.write_text('ephemeral-ci-marker')
    report = {'schema': 1, 'revision': revision, 'same_version': True}
    try:
        for label, kind, expected in (
            ('verified_install_and_native_readiness', NativeUpdater, 0),
            ('injected_failure_and_actual_rollback', ForcedPostInstallHealthFailure, 3),
        ):
            job = updates / ('job-' + uuid.uuid4().hex)
            job.mkdir()
            request = {'schema': 1, 'job_id': job.name, 'install_root': str(installed), 'installer': str(installer),
                       'sha256': digest(installer), 'size': installer.stat().st_size, 'version': identity['version'],
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
                if not registration or any(registration.get(key) != value for key, value in updater.registry.items()):
                    raise RuntimeError('Per-user uninstall registration was not preserved/restored')
                report[label] = 'passed'
            finally:
                # Stop only executables under this exact ephemeral install root.
                updater.stop_install_processes()
            if json.loads((installed / 'native-install.json').read_text())['revision'] != revision:
                raise RuntimeError('Installed revision changed unexpectedly')
        args.report.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.report, report)
        print('Real same-version update, native readiness and injected-failure rollback passed. Cross-version acceptance remains separate.')
    finally:
        marker.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
