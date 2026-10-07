import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("telegram_diagnostics", Path(__file__).resolve().parents[1] / "deploy/telegram_network_diagnostics.py")
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class TelegramDiagnosticsTests(unittest.TestCase):
    def test_secrets_and_other_environment_are_excluded(self):
        raw = b"BOT_TOKEN=sentinel-secret\0HTTPS_PROXY=http://proxy.invalid\0PATH=private\0SSL_CERT_FILE=/ca.pem\0"
        self.assertEqual(diagnostics.selected_environment(raw, diagnostics.NETWORK_KEYS),
                         {"HTTPS_PROXY": "http://proxy.invalid", "SSL_CERT_FILE": "/ca.pem"})

    def test_error_details_cannot_emit_secret_url(self):
        exc = OSError(101, "https://api.telegram.org/botsentinel-secret/getMe")
        self.assertEqual(diagnostics.error_fields(exc), {"error_type": "OSError", "errno": 101})

    def test_journal_summary_does_not_return_messages_or_tokens(self):
        entries = [{"MESSAGE": "telegram_polling_loop Telegram API error: botsentinel-secret", "PRIORITY": "4", "__REALTIME_TIMESTAMP": "123"},
                   {"MESSAGE": "HTTP Request: POST https://api.telegram.org/botsentinel-secret/getMe HTTP/1.1 200 OK", "PRIORITY": "6"}]
        result = diagnostics.journal_summary(entries)
        self.assertEqual(result["polling_error_entries"], 1)
        self.assertEqual(result["telegram_http_success_entries"], 1)
        self.assertNotIn("sentinel-secret", json.dumps(result))

    def test_connect_failure_is_identified_without_attempting_tls(self):
        connection = MagicMock()
        connection.connect.side_effect = TimeoutError("secret text")
        with patch.object(diagnostics.socket, "socket", return_value=connection), patch.object(diagnostics.ssl, "create_default_context") as context, redirect_stdout(io.StringIO()) as output:
            result = diagnostics.probe("api.telegram.org", socket.AF_INET, "192.0.2.1")
        self.assertEqual(result["failed_stage"], "tcp_connect")
        context.assert_not_called()
        self.assertNotIn("secret text", output.getvalue())
        connection.close.assert_called_once()

    def test_recent_transport_failures_are_distinct_from_polling_conflicts(self):
        entries = [
            {"MESSAGE": "telegram_polling_loop Telegram API error: Telegram API request failed | method=getUpdates", "__REALTIME_TIMESTAMP": "9900000000"},
            {"MESSAGE": "Polling conflict (409): private-sentinel", "__REALTIME_TIMESTAMP": "9950000000"},
            {"MESSAGE": "telegram_polling_loop Telegram API error: Telegram API request failed", "__REALTIME_TIMESTAMP": "1000000000"},
        ]
        result = diagnostics.journal_summary(entries, now=10000)
        self.assertEqual(result["telegram_transport_errors"], 2)
        self.assertEqual(result["telegram_transport_errors_last_30_minutes"], 1)
        self.assertEqual(result["telegram_conflicts_last_30_minutes"], 1)
        self.assertNotIn("private-sentinel", json.dumps(result))

    def test_iptables_retains_policy_match_counters_and_drops_private_comments(self):
        raw = b'*filter\n:OUTPUT DROP [5:300]\n[2:100] -A OUTPUT -d 149.154.166.110/32 -p tcp --dport 443 -m comment --comment "private-sentinel" -j ACCEPT\nCOMMIT\n'
        result = diagnostics.summarize_iptables(raw)
        self.assertEqual(result[0]["chains"][0]["policy"], "DROP")
        self.assertIn("149.154.166.110/32", result[0]["rules"][0]["mechanics"])
        self.assertIn("ACCEPT", result[0]["rules"][0]["mechanics"])
        self.assertNotIn("private-sentinel", json.dumps(result))

    def test_nft_summary_keeps_hooks_and_verdicts_without_payload_or_comment(self):
        value = {"nftables": [{"chain": {"name": "out", "hook": "output", "policy": "drop", "comment": "secret"}},
            {"rule": {"chain": "out", "comment": "secret", "expr": [{"match": {"right": "private-sentinel"}}, {"drop": None}, {"counter": {"packets": 3, "bytes": 9}}]}}]}
        result = diagnostics.nft_summary(value)
        self.assertEqual(result["chains"][0]["policy"], "drop")
        self.assertEqual(result["rules"][0]["verdicts"][0]["kind"], "drop")
        self.assertNotIn("private-sentinel", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_kernel_summary_is_target_specific_and_never_emits_log_text(self):
        entries = [{"MESSAGE": "[UFW BLOCK] DST=149.154.166.110 DPT=443 private-sentinel"},
                   {"MESSAGE": "[UFW BLOCK] DST=192.0.2.5 DPT=443 unrelated"}]
        result = diagnostics.kernel_filter_summary(entries, "149.154.166.110")
        self.assertEqual(result["target_block_entries"], 1)
        self.assertNotIn("private-sentinel", json.dumps(result))

    def test_privileged_read_stops_if_existing_permission_is_missing(self):
        responses = [subprocess.CompletedProcess([], 1, b"", b"Permission denied"),
                     subprocess.CompletedProcess([], 1, b"", b"password required")]
        with patch.object(diagnostics.shutil, "which", side_effect=lambda name: "/usr/bin/"+name), patch.object(diagnostics.os, "geteuid", return_value=1000, create=True), patch.object(diagnostics.subprocess, "run", side_effect=responses) as run:
            data, status = diagnostics.policy_read(["nft", "-j", "list", "ruleset"], privileged=True)
        self.assertIsNone(data)
        self.assertEqual(status, "existing_sudo_permission_unavailable")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][:3], ["/usr/bin/sudo", "-n", "-l"])

    def test_partial_journal_access_is_not_reported_as_complete(self):
        responses = [subprocess.CompletedProcess([], 0, b"", b"You are currently not seeing messages from other users"),
                     subprocess.CompletedProcess([], 1, b"", b"password required")]
        with patch.object(diagnostics.shutil, "which", side_effect=lambda name: "/usr/bin/"+name), patch.object(diagnostics.os, "geteuid", return_value=1000, create=True), patch.object(diagnostics.subprocess, "run", side_effect=responses):
            data, status = diagnostics.policy_read(["journalctl", "-k"], privileged=True)
        self.assertIsNone(data)
        self.assertEqual(status, "existing_sudo_permission_unavailable")

    def test_tls_verification_failure_is_never_bypassed(self):
        connection = MagicMock()
        context = MagicMock(verify_mode=ssl.CERT_REQUIRED, check_hostname=True)
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("private certificate detail")
        with patch.object(diagnostics.socket, "socket", return_value=connection), patch.object(diagnostics.ssl, "create_default_context", return_value=context), redirect_stdout(io.StringIO()) as output:
            result = diagnostics.probe("api.telegram.org", socket.AF_INET, "192.0.2.1")
        self.assertEqual(result["failed_stage"], "tls_handshake")
        context.wrap_socket.assert_called_once_with(connection, server_hostname="api.telegram.org")
        connection.sendall.assert_not_called()
        self.assertNotIn("private certificate detail", output.getvalue())

    def test_verified_tls_then_header_read_is_reported_separately(self):
        connection = MagicMock()
        secure = MagicMock()
        secure.version.return_value = "TLSv1.3"
        secure.recv.side_effect = TimeoutError("private")
        context = MagicMock(verify_mode=ssl.CERT_REQUIRED, check_hostname=True)
        context.wrap_socket.return_value = secure
        with patch.object(diagnostics.socket, "socket", return_value=connection), patch.object(diagnostics.ssl, "create_default_context", return_value=context), redirect_stdout(io.StringIO()):
            result = diagnostics.probe("api.telegram.org", socket.AF_INET, "192.0.2.1")
        self.assertTrue(result["tls_verified"])
        self.assertEqual(result["failed_stage"], "http_read_headers")
        secure.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
