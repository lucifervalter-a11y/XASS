"""Interrupt a real Inno installer mid-write, using synthetic data in temp only.

The production Job Object supervisor and NativeUpdater backup/restore are used.
Native GUI health is intentionally a separate installed-app smoke; this fixture
checks exact file recovery rather than pretending its synthetic EXEs can launch.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from installer_process import run_installer
from native_updater import APP_ID, NativeUpdater, digest, verify_tree, write_json

OLD, NEW = "1" * 40, "2" * 40


def find_inno():
    candidates = [os.environ.get("XASS_TEST_ISCC"), shutil.which("ISCC.exe")]
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            candidates.extend((str(Path(base) / "Inno Setup 6/ISCC.exe"),
                               str(Path(base) / "Programs/Inno Setup 6/ISCC.exe")))
    return next((path for path in candidates if path and Path(path).is_file()), None)


@unittest.skipUnless(os.name == "nt" and find_inno(), "Real interruption requires Windows and Inno Setup")
class RealInstallerRollback(unittest.TestCase):
    def test_interrupted_install_restores_payload_and_preserves_user_data(self):
        with tempfile.TemporaryDirectory(prefix="xass-real-rollback-") as temporary:
            root = Path(temporary).resolve()
            source, output = root / "fixture-source", root / "fixture-output"
            source.mkdir(); output.mkdir()
            (source / "first.bin").write_bytes(b"new first executable" * 100)
            (source / "second.bin").write_bytes(b"new second executable" * 100)
            fixture = Path(__file__).parent / "fixtures/interrupted_installer.iss"
            result = subprocess.run([find_inno(), "/Q", "/DFixtureSource=" + str(source),
                                     "/DFixtureOutput=" + str(output), str(fixture)],
                                    capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            with patch.dict(os.environ, {"LOCALAPPDATA": str(root)}):
                updates = root / "XASS.Native/updates"; updates.mkdir(parents=True)
                job = updates / ("job-" + "a" * 32); job.mkdir()
                install = root / "Programs/XASS-Native-Test"; (install / "runtime").mkdir(parents=True)
                (install / "Xass.Native.exe").write_bytes(b"working old native")
                (install / "runtime/XASS.NativeHelper.exe").write_bytes(b"working old helper")
                write_json(install / "native-install.json", {"app_id": APP_ID, "distribution": "native-test",
                                                            "revision": OLD, "version": "1.0.0"})
                inventory = {p.relative_to(install).as_posix(): {"sha256": digest(p), "size": p.stat().st_size}
                             for p in install.rglob("*") if p.is_file()}
                write_json(install / "payload-manifest.json", {"schema": 1, "revision": OLD,
                           "files": [{"path": p, "bytes": r["size"], "sha256": r["sha256"]} for p, r in inventory.items()]})
                installer = updates / ("XASS-Native-Test-" + NEW + ".exe")
                shutil.copyfile(output / "interrupted-fixture.exe", installer)
                protected = {}
                for relative, data in {"XASS/config.json": b'{"synthetic":true}',
                                       "XASS/.e2e-key": b"synthetic-e2e-key-not-a-secret",
                                       "XASS/archive/rare-live.flac": b"synthetic-rare-recording",
                                       "XASS.Native/preferences.json": b'{"theme":"dark"}'}.items():
                    path = root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
                    protected[relative] = digest(path)
                write_json(job / "request.json", {"schema": 1, "job_id": job.name, "install_root": str(install),
                           "installer": str(installer), "sha256": digest(installer), "size": installer.stat().st_size,
                           "version": "2.0.0", "revision": NEW, "parent_pid": os.getpid(),
                           "parent_created": time.time(), "automatic": True})

                class IsolatedUpdater(NativeUpdater):
                    # Never inspect or modify this workstation's real registry.
                    def read_uninstall_registration(self): return {}
                    def restore_uninstall_registration(self): pass
                    def wait_for_parent(self): pass
                    def verify_installed(self, revision):
                        if revision != OLD: raise AssertionError("Incomplete update reached health check")
                        verify_tree(self.install, inventory)
                        self.rollback_verified = True

                def interrupted(arguments, **kwargs):
                    # Inno waits after replacing the first of two
                    # payload files; the real supervisor terminates its entire job.
                    return run_installer(arguments, cwd=kwargs["cwd"], timeout=10, cleanup_timeout=5)

                updater = IsolatedUpdater(job / "request.json", installer_run=interrupted)
                self.assertEqual(updater.execute(), 3)
                self.assertTrue(updater.installer_shutdown_confirmed)
                self.assertTrue(updater.rollback_verified)
                quarantines = list(install.parent.glob("XASS-Native-Test.failed-*"))
                self.assertEqual(len(quarantines), 1)
                failed = quarantines[0]
                self.assertEqual((failed / "halfway.txt").read_text(), "first-file-replaced")
                self.assertEqual((failed / "Xass.Native.exe").read_bytes(), (source / "first.bin").read_bytes())
                self.assertEqual((failed / "runtime/XASS.NativeHelper.exe").read_bytes(), b"working old helper")
                self.assertFalse((install / "halfway.txt").exists())
                verify_tree(install, inventory)
                self.assertEqual(protected, {relative: digest(root / relative) for relative in protected})
                state = json.loads((updates / "last-result.json").read_text())
                self.assertEqual(state["phase"], "rolled-back")
                self.assertTrue(state["rollback_ok"])
                self.assertEqual(state["rejected_sha256"], digest(installer))
                # Ensure no abandoned Inno child resumes writing into restored data.
                time.sleep(0.3)
                verify_tree(install, inventory)
                self.assertFalse((install / "halfway.txt").exists())


if __name__ == "__main__": unittest.main()
