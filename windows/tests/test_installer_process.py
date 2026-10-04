"""Portable failure contracts plus real Windows parent/descendant supervision."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import installer_process as process


class FakeClock:
    def __init__(self):
        self.now = 0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class InstallerProcessContracts(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.job = Mock()
        self.job.poll_parent.return_value = 0
        self.job.empty.return_value = True
        self.job.stopped.return_value = True
        patched = patch("installer_process._WindowsJob", return_value=self.job)
        patched.start(); self.addCleanup(patched.stop)

    def run_installer(self, **kwargs):
        return process.run_installer([sys.executable, "test fixture.py"], cwd=Path.cwd(),
                                     clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def test_parent_exit_does_not_end_wait_for_descendant(self):
        self.job.empty.side_effect = [False, False, True]
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertAlmostEqual(self.clock.now, 0.1)
        self.assertEqual(self.job.poll_parent.call_count, 3)
        self.job.terminate.assert_not_called(); self.job.close.assert_called_once()

    def test_timeout_terminates_whole_job_and_waits_for_proof(self):
        self.job.empty.return_value = False
        self.job.stopped.side_effect = [False, False, True]
        with self.assertRaises(process.InstallerTimeoutError) as error:
            self.run_installer(timeout=0.1, cleanup_timeout=0.5)
        self.assertTrue(error.exception.shutdown_confirmed)
        self.job.terminate.assert_called_once()
        self.assertAlmostEqual(self.clock.now, 0.2)
        self.job.close.assert_called_once()

    def test_cleanup_deadline_leaves_shutdown_unconfirmed(self):
        self.job.empty.return_value = False
        self.job.stopped.return_value = False
        with self.assertRaises(process.InstallerProcessError) as error:
            self.run_installer(timeout=0.1, cleanup_timeout=0.2)
        self.assertFalse(error.exception.shutdown_confirmed)
        self.assertAlmostEqual(self.clock.now, 0.3)
        self.job.close.assert_called_once()

    def test_query_errors_never_establish_shutdown(self):
        self.job.empty.side_effect = OSError("query failed")
        self.job.stopped.side_effect = OSError("query failed")
        with self.assertRaises(process.InstallerProcessError) as error:
            self.run_installer(cleanup_timeout=0.2)
        self.assertFalse(error.exception.shutdown_confirmed)
        self.assertAlmostEqual(self.clock.now, 0.2)

    def test_terminate_success_is_not_shutdown_proof(self):
        self.job.empty.return_value = False
        self.job.terminate.return_value = True
        self.job.stopped.return_value = False
        with self.assertRaises(process.InstallerProcessError) as error:
            self.run_installer(timeout=0.1, cleanup_timeout=0.2)
        self.assertFalse(error.exception.shutdown_confirmed)

    def test_start_failure_still_requires_confirmed_cleanup(self):
        self.job.start.side_effect = OSError("assignment failed")
        with self.assertRaises(process.InstallerProcessError) as error:
            self.run_installer()
        self.assertTrue(error.exception.shutdown_confirmed)
        self.job.terminate.assert_called_once(); self.job.stopped.assert_called_once()

    def test_invalid_deadlines_rejected_before_launch(self):
        for invalid in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.run_installer(cleanup_timeout=invalid)
        self.job.start.assert_not_called()


class Win32LaunchOrdering(unittest.TestCase):
    def test_process_supervisor_is_included_with_published_updater(self):
        project = (Path(__file__).resolve().parents[1] / "Xass.Native/Xass.Native.csproj").read_text()
        self.assertIn('Include="..\\installer_process.py" Link="installer_process.py"', project)

    def test_suspended_creation_and_job_assignment_precede_resume(self):
        job = process._WindowsJob.__new__(process._WindowsJob)
        job.api = Mock()
        job.job = job.process = job.thread = None
        job.assigned = False; job.returncode = None
        job.api.CreateJobObjectW.return_value = 101
        job.api.ResumeThread.return_value = 1
        def create(*args):
            info = ctypes.cast(args[-1], ctypes.POINTER(process._ProcessInfo)).contents
            info.hProcess, info.hThread = 102, 103
            self.assertFalse(args[4])  # Never inherit the job handle.
            self.assertEqual(args[5] & 0x00000004, 0x00000004)
            self.assertEqual(args[5] & 0x01000000, 0)  # No CREATE_BREAKAWAY_FROM_JOB.
            self.assertEqual(args[0], sys.executable)
            return True
        job.api.CreateProcessW.side_effect = create
        def limits(*args):
            info = ctypes.cast(args[2], ctypes.POINTER(process._ExtendedLimitInfo)).contents
            self.assertEqual(info.BasicLimitInformation.LimitFlags, 0x2000)
            return True
        job.api.SetInformationJobObject.side_effect = limits
        job.start([sys.executable, "fixture with spaces.py"], Path.cwd())
        calls = [call[0] for call in job.api.mock_calls]
        self.assertLess(calls.index("SetInformationJobObject"), calls.index("CreateProcessW"))
        self.assertLess(calls.index("CreateProcessW"), calls.index("AssignProcessToJobObject"))
        self.assertLess(calls.index("AssignProcessToJobObject"), calls.index("ResumeThread"))
        self.assertTrue(job.assigned)

    def test_failed_assignment_never_resumes_suspended_loader(self):
        job = process._WindowsJob.__new__(process._WindowsJob)
        job.api = Mock(); job.api.CreateJobObjectW.return_value = 101
        job.job = job.process = job.thread = None
        job.assigned = False; job.returncode = None
        def create(*args):
            info = ctypes.cast(args[-1], ctypes.POINTER(process._ProcessInfo)).contents
            info.hProcess, info.hThread = 102, 103
            return True
        job.api.CreateProcessW.side_effect = create
        job.api.AssignProcessToJobObject.side_effect = OSError("denied")
        with self.assertRaises(OSError):
            job.start([sys.executable], Path.cwd())
        job.api.ResumeThread.assert_not_called()
        job.terminate()
        job.api.TerminateProcess.assert_called_once_with(102, 1)
        job.api.TerminateJobObject.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Real Job Objects require Windows")
class WindowsInstallerProcessTree(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xass-job-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "synthetic-install"
        self.root.mkdir()
        self.fixture = Path(__file__).parent / "fixtures" / "installer_tree_writer.py"

    def command(self, parent_seconds, child_seconds):
        return [sys.executable, str(self.fixture), "parent", str(self.root),
                str(parent_seconds), str(child_seconds)]

    def assert_writers_stopped(self):
        self.assertTrue((self.root / "parent.pid").is_file())
        self.assertTrue((self.root / "child.pid").is_file())
        before = {p.name: p.read_bytes() for p in self.root.glob("*.writes")}
        self.assertEqual(set(before), {"parent.writes", "child.writes"})
        self.assertTrue(all(before.values()))
        time.sleep(0.3)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.glob("*.writes")})
        # A rollback-style rename/replacement must not race or revive a writer.
        quarantine = self.root.with_name("synthetic-quarantine")
        self.root.rename(quarantine)
        self.root.mkdir()
        (self.root / "restored").write_bytes(b"verified-old-payload")
        time.sleep(0.3)
        self.assertEqual(list(p.name for p in self.root.iterdir()), ["restored"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in quarantine.glob("*.writes")})

    def test_success_waits_for_child_after_loader_exit(self):
        result = process.run_installer(self.command(0.1, 1.2), cwd=self.root, timeout=15, cleanup_timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.root / "parent.done").is_file())
        self.assertTrue((self.root / "child.done").is_file())
        self.assert_writers_stopped()

    def test_timeout_stops_live_loader_and_child_before_rollback(self):
        with self.assertRaises(process.InstallerTimeoutError) as error:
            process.run_installer(self.command(60, 60), cwd=self.root, timeout=4, cleanup_timeout=5)
        self.assertTrue(error.exception.shutdown_confirmed)
        self.assertFalse((self.root / "parent.done").exists())
        self.assertFalse((self.root / "child.done").exists())
        self.assert_writers_stopped()

    def test_timeout_stops_child_even_when_loader_already_exited(self):
        with self.assertRaises(process.InstallerTimeoutError) as error:
            process.run_installer(self.command(0.1, 60), cwd=self.root, timeout=4, cleanup_timeout=5)
        self.assertTrue(error.exception.shutdown_confirmed)
        self.assertTrue((self.root / "parent.done").is_file())
        self.assertFalse((self.root / "child.done").exists())
        self.assert_writers_stopped()


if __name__ == "__main__":
    unittest.main()
