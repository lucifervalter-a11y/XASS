import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
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
