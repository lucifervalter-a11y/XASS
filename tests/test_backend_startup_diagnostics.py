from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("startup_diagnostics", Path(__file__).resolve().parents[1] / "deploy/backend_startup_diagnostics.py")
diag = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diag)


def journal(message, **values):
    return json.dumps({"MESSAGE": message, **values}).encode() + b"\n"


class SanitizationTests(unittest.TestCase):
    def test_exception_text_and_unrelated_private_fields_never_exported(self):
        secret = "PRIVATE_TOKEN_SENTINEL=abcdefghijklmnopqrstuvwxyz"
        raw = journal("ValueError: " + secret, DATABASE_URL=secret)
        report = diag.parse_journal(raw, set())
        self.assertEqual(report["exceptions"], ["ValueError"])
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn("DATABASE", json.dumps(report))

    def test_only_tracked_safe_app_frames_survive(self):
        raw = journal('\n'.join((
            '  File "/home/red/serverredus/app/main.py", line 42, in <module>',
            '  File "/home/red/serverredus/app/secret_token.py", line 4, in boot',
            '  File "/home/red/serverredus/.env", line 4, in boot',
            '  File "/home/red/serverredus/app/../secret.py", line 4, in boot',
            '  File "/other/private/app/main.py", line 4, in boot',
        )))
        result = diag.parse_journal(raw, {"app/main.py", "app/../secret.py"})
        self.assertEqual(result["frames"], [{"path": "app/main.py", "line": 42}])
        self.assertNotIn("secret", json.dumps(result))

    def test_known_missing_module_and_restrictive_unknown_fallback(self):
        raw = journal("ModuleNotFoundError: No module named 'multipart'\n"
                      "ModuleNotFoundError: No module named 'app.music_api'\n"
                      "ModuleNotFoundError: No module named 'PRIVATE_TOKEN_SENTINEL'\n"
                      "ModuleNotFoundError: No module named 'https://secret.invalid/token'")
        result = diag.parse_journal(raw, {"app/music_api.py"})
        self.assertEqual(result["missing_modules"], ["app.music_api", "multipart"])
        self.assertIn("MISSING_MODULE_UNLISTED", result["reasons"])
        self.assertNotIn("SENTINEL", json.dumps(result))
        self.assertNotIn("secret.invalid", json.dumps(result))

    def test_unknown_exception_class_cannot_leak(self):
        result = diag.parse_journal(journal("PRIVATE_TOKEN_SENTINEL: private body"), set())
        self.assertEqual(result["exceptions"], [])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_malformed_json_nonobject_and_binary_message(self):
        raw = b"SECRET_NOT_JSON\n[]\nnull\n" + journal([1, 2, 3])
        result = diag.parse_journal(raw, set())
        self.assertEqual(result["reasons"], ["MALFORMED_JOURNAL_ENTRY"])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_fixed_startup_and_systemd_reasons(self):
        raw = journal("INFO:     Application startup complete.\n"
                      "ERROR:    Application startup failed. Exiting.\n"
                      "OSError: [Errno 98] address already in use (secret endpoint)\n"
                      "serverredus-backend.service: start operation timed out. Terminating.\n"
                      "serverredus-backend.service: Scheduled restart job, restart counter is at 3.")
        result = diag.parse_journal(raw, set())
        self.assertEqual(set(result["reasons"]), {"STARTUP_COMPLETE", "STARTUP_FAILED", "ADDRESS_IN_USE", "SERVICE_TIMEOUT", "SERVICE_RESTART"})
        self.assertNotIn("secret endpoint", json.dumps(result))

    def test_other_service_ignored_and_output_bounded(self):
        self.assertEqual(diag.parse_journal(journal("ValueError: private", _SYSTEMD_UNIT="other.service"), set())["exceptions"], [])
        self.assertEqual(diag.parse_journal(b"x" * (diag.MAX_BYTES + 1), set())["reasons"], ["OUTPUT_LIMIT"])

    def test_systemd_manager_unit_record_is_classified(self):
        raw = journal("serverredus-backend.service: Scheduled restart job, restart counter is at 3.",
                      _SYSTEMD_UNIT="init.scope", UNIT=diag.SERVICE)
        self.assertEqual(diag.parse_journal(raw, set())["reasons"], ["SERVICE_RESTART"])

    def test_version_metadata_cannot_export_nonversion(self):
        with patch.object(diag.importlib.metadata, "version", return_value="1.2+SECRET_SENTINEL"):
            result = diag.dependency_versions()
        self.assertEqual(set(result.values()), {"VERSION_UNAVAILABLE"})
        with patch.object(diag.importlib.metadata, "version", return_value="2.0.42"):
            self.assertEqual(diag.dependency_versions()["sqlalchemy"], "2.0.42")

    def test_collect_suppresses_permission_error_and_bad_service_values(self):
        responses = [("OK", b"a" * 40), ("OK", b"app/main.py\n"), ("OK", b"app/music_curation_api.py\n"),
                     ("OK", b"ActiveState=active\nSubState=TOKEN_SENTINEL\nToken=SECRET\n"),
                     ("COMMAND_FAILED", b"Permission denied SECRET_SENTINEL")]
        with patch.object(diag, "bounded_command", side_effect=responses) as command, patch.object(diag, "dependency_versions", return_value={}):
            result = diag.collect()
        self.assertEqual(result["journal_status"], "JOURNAL_PERMISSION_DENIED")
        self.assertEqual(result["service"], {"ActiveState": "active"})
        self.assertNotIn("SENTINEL", json.dumps(result))
        self.assertRegex(result["python"], r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertEqual(command.call_args_list[2].args[0], ["git", "ls-tree", "-r", "--name-only", diag.FAILED_OVERLAY, "--", "app"])
        self.assertEqual(command.call_args_list[-1].args[0], ["journalctl", "-u", diag.SERVICE, "--since", diag.SINCE, "--until", diag.UNTIL, "--output=json", "--no-pager", "--quiet"])

    def test_failed_overlay_frame_survives_after_rollback(self):
        responses = [("OK", b"a" * 40), ("OK", b"app/main.py\n"), ("OK", b"app/music_curation_api.py\n"),
                     ("OK", b"ActiveState=active\nSubState=running\n"),
                     ("OK", journal('  File "/home/red/serverredus/app/music_curation_api.py", line 123, in build_router'))]
        with patch.object(diag, "bounded_command", side_effect=responses), patch.object(diag, "dependency_versions", return_value={}):
            result = diag.collect()
        self.assertEqual(result["journal"]["frames"], [{"path": "app/music_curation_api.py", "line": 123}])

    def test_main_never_prints_traceback(self):
        out = io.StringIO()
        with patch.object(diag, "collect", side_effect=RuntimeError("SECRET_SENTINEL")), redirect_stdout(out):
            diag.main()
        self.assertEqual(json.loads(out.getvalue())["diagnosis_status"], "DIAGNOSTIC_FAILED")
        self.assertNotIn("SECRET", out.getvalue())


class CommandBoundsTests(unittest.TestCase):
    def test_subprocess_output_limit_discards_all_raw_output(self):
        with tempfile.TemporaryDirectory() as folder:
            status, raw = diag.bounded_command([sys.executable, "-c", "print('x' * 10000)"], root=folder, limit=64)
        self.assertEqual(status, "OUTPUT_LIMIT")
        self.assertEqual(raw, b"")

    def test_subprocess_timeout_discards_all_raw_output(self):
        with tempfile.TemporaryDirectory() as folder:
            status, raw = diag.bounded_command([sys.executable, "-c", "import time; time.sleep(5)"], root=folder, timeout=.1)
        self.assertEqual(status, "TIMEOUT")
        self.assertEqual(raw, b"")

    def test_normal_readonly_command(self):
        with tempfile.TemporaryDirectory() as folder:
            status, raw = diag.bounded_command([sys.executable, "-c", "print('safe')"], root=folder)
        self.assertEqual((status, raw.strip()), ("OK", b"safe"))


if __name__ == "__main__":
    unittest.main()
