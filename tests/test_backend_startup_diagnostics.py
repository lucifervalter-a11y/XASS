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
    def test_event_offsets_distinguish_deploy_and_rollback_startup(self):
        raw = journal("INFO: Application startup complete.",
                      __REALTIME_TIMESTAMP=str(diag.START_US + 12_345_678), _PID="100")
        raw += journal("INFO: Application startup complete.",
                       __REALTIME_TIMESTAMP=str(diag.START_US + 98_765_432), _PID="200")
        events = diag.parse_journal(raw, set())["events"]
        self.assertEqual(events, [
            {"kind": "STARTUP_COMPLETE", "offset_ms": 12_345, "pid": 100},
            {"kind": "STARTUP_COMPLETE", "offset_ms": 98_765, "pid": 200},
        ])

    def test_timestamp_rejection_never_exports_arbitrary_values(self):
        invalid = (None, True, diag.START_US, "SECRET_TIMESTAMP", "1" * 10000,
                   str(diag.START_US - 1), str(diag.END_US + 1), " " + str(diag.START_US),
                   str(diag.START_US) + "\n", [str(diag.START_US)])
        for value in invalid:
            with self.subTest(value=type(value)):
                result = diag.parse_journal(journal("Application startup complete.", __REALTIME_TIMESTAMP=value), set())
                self.assertEqual(result["events"], [])
                self.assertNotIn("SECRET", json.dumps(result))

    def test_pid_rejection_does_not_drop_valid_event(self):
        for value in ("SECRET_PID", "-10", "2147483648", 123, True, "1\n"):
            result = diag.parse_journal(journal("Application startup complete.",
                                               __REALTIME_TIMESTAMP=str(diag.START_US), _PID=value), set())
            self.assertEqual(result["events"], [{"kind": "STARTUP_COMPLETE", "offset_ms": 0}])

    def test_only_fixed_http_routes_and_status_without_address_or_parameters(self):
        raw = journal('INFO:  127.0.0.1:50123 - "GET /health HTTP/1.1" 200 OK',
                      __REALTIME_TIMESTAMP=str(diag.START_US + 1000))
        raw += journal('INFO:  PRIVATE_ADDRESS_SENTINEL - "POST /api/discord/interactions HTTP/1.1" 401 Unauthorized',
                       __REALTIME_TIMESTAMP=str(diag.START_US + 2000))
        result = diag.parse_journal(raw, set())
        self.assertEqual(result["events"], [
            {"kind": "HTTP_ACCESS", "offset_ms": 1, "method": "GET", "route": "/health", "status": 200},
            {"kind": "HTTP_ACCESS", "offset_ms": 2, "method": "POST", "route": "/api/discord/interactions", "status": 401},
        ])
        self.assertNotIn("127.0.0.1", json.dumps(result))
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_http_wrong_routes_params_methods_and_status_rejected(self):
        for request in ('GET /health?token=SECRET HTTP/1.1" 200 OK',
                        'GET /api/private/SECRET HTTP/1.1" 200 OK',
                        'POST /health HTTP/1.1" 200 OK',
                        'GET /api/discord/interactions HTTP/1.1" 200 OK',
                        'GET /health HTTP/1.1" 999 SECRET',
                        'GET /health HTTP/1.1" 200 OK token=SECRET'):
            result = diag.parse_journal(journal('INFO: 127.0.0.1:123 - "' + request,
                                               __REALTIME_TIMESTAMP=str(diag.START_US)), set())
            self.assertEqual(result["events"], [])
            self.assertNotIn("SECRET", json.dumps(result))

    def test_event_count_bounded_preserves_last_rollback(self):
        raw = b"".join(journal("Application startup complete.", __REALTIME_TIMESTAMP=str(diag.START_US + i * 1000))
                       for i in range(60))
        result = diag.parse_journal(raw, set())
        self.assertEqual(len(result["events"]), 40)
        self.assertEqual([event["offset_ms"] for event in result["events"]], list(range(20)) + list(range(40, 60)))
        self.assertIn("EVENT_LIMIT_REACHED", result["reasons"])

    def test_service_stop_events_export_only_enum_and_timing(self):
        raw = journal("Stopping PRIVATE_DESCRIPTION_SENTINEL...\nStopped PRIVATE_DESCRIPTION_SENTINEL.",
                      __REALTIME_TIMESTAMP=str(diag.END_US), UNIT=diag.SERVICE, _SYSTEMD_UNIT="init.scope")
        result = diag.parse_journal(raw, set())
        self.assertEqual([event["kind"] for event in result["events"]], ["SERVICE_STOPPING", "SERVICE_STOPPED"])
        self.assertEqual(result["events"][0]["offset_ms"], 110000)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_systemd_durations_are_numeric_bounded_and_sanitized(self):
        for value, expected in (("1min 30s", 90000), ("90s", 90000), ("500ms", 500),
                                ("1.5s", 1500), ("1000us", 1), ("1d", 86400000)):
            self.assertEqual(diag.timeout_milliseconds(value), expected)
        for value in ("infinity", "SECRET_SENTINEL", "99d", "90s\nSECRET", "-1s", "nanms", "1s  SECRET", None, True):
            self.assertEqual(diag.timeout_milliseconds(value), "TIMEOUT_UNAVAILABLE")

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
                     ("OK", b"ActiveState=active\nSubState=TOKEN_SENTINEL\nToken=SECRET\nTimeoutStartUSec=1min 30s\nTimeoutStopUSec=SECRET_SENTINEL\n"),
                     ("COMMAND_FAILED", b"Permission denied SECRET_SENTINEL")]
        with patch.object(diag, "bounded_command", side_effect=responses) as command, patch.object(diag, "dependency_versions", return_value={}):
            result = diag.collect()
        self.assertEqual(result["journal_status"], "JOURNAL_PERMISSION_DENIED")
        self.assertEqual(result["service"], {"ActiveState": "active"})
        self.assertEqual(result["service_timeouts_ms"], {"start": 90000, "stop": "TIMEOUT_UNAVAILABLE"})
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
