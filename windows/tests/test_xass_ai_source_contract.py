"""Fail-closed source contracts for the native XASS AI panel; no process or network I/O."""
from pathlib import Path
import re
import unittest


NATIVE = Path(__file__).resolve().parents[1] / "Xass.Native"
PROTOCOL = (NATIVE / "Services" / "XassAiProtocol.cs").read_text(encoding="utf-8")
RUNTIME = (NATIVE / "Services" / "XassAiRuntime.cs").read_text(encoding="utf-8")
UI = (NATIVE / "MainWindow.XassAi.cs").read_text(encoding="utf-8")
WINDOW = (NATIVE / "MainWindow.xaml.cs").read_text(encoding="utf-8")


class XassAiSourceContractTests(unittest.TestCase):
    def test_main_ui_contains_unbranded_xass_ai_page(self):
        self.assertIn('AddDesktopPage("xass-ai", "XASS AI"', UI)
        self.assertIn("XASS AI установлен, но недоступен", UI)
        self.assertNotRegex(UI, re.compile(r"hermes", re.IGNORECASE))
        self.assertIn("InitializeXassAi();", WINDOW)
        self.assertIn("OnXassAiNavigated(page);", WINDOW)
        self.assertIn("DisposeXassAi();", WINDOW)

    def test_ui_is_disabled_until_an_owned_authenticated_runtime_is_ready(self):
        self.assertIn('Content = "Отправить", IsEnabled = false', UI)
        self.assertIn("availability.Kind == XassAiAvailabilityKind.Ready", UI)
        self.assertIn("xassAiProtocol?.IsConnected == true", UI)
        self.assertIn("BindOwnedXassAiRuntimeAsync", UI)
        self.assertIn("NativeRuntimeMode.IsTestMode", UI)

    def test_install_probe_reports_installed_but_unavailable_not_online(self):
        self.assertIn("XassAiAvailabilityKind.InstalledUnavailable", RUNTIME)
        self.assertIn("XASS AI установлен, но сейчас недоступен", RUNTIME)
        self.assertNotIn("XassAiAvailabilityKind.Ready,", RUNTIME)
        test_guard = RUNTIME.index("if (NativeRuntimeMode.IsTestMode)")
        host_probe = RUNTIME.index("Environment.SpecialFolder.LocalApplicationData")
        self.assertLess(test_guard, host_probe)

    def test_protocol_is_exact_health_then_loopback_websocket(self):
        connect = PROTOCOL[PROTOCOL.index("public async Task ConnectAsync"):]
        self.assertLess(connect.index("ProbeExactHealthAsync"), connect.index("socket.ConnectAsync"))
        self.assertIn('new Uri(endpoint.BaseUri, "api/health")', PROTOCOL)
        self.assertIn('Path = "/api/ws"', PROTOCOL)
        self.assertIn('Query = "token=" + Uri.EscapeDataString(sessionToken)', PROTOCOL)
        self.assertIn("IPAddress.IsLoopback(address)", PROTOCOL)
        self.assertIn("AllowAutoRedirect = false", PROTOCOL)
        self.assertIn("UseProxy = false", PROTOCOL)
        self.assertIn('SupportedRuntimeVersion = "0.22.2"', PROTOCOL)
        self.assertIn('SupportedRuntimeCommit = "d923a5eb487a17c0a68801bba9ef0f56c81040a8"', PROTOCOL)
        for key in ("ok", "version", "displayVersion", "commit", "auth_required"):
            self.assertIn(f'"{key}"', PROTOCOL)
        self.assertIn('GetProperty("auth_required").ValueKind == JsonValueKind.True', PROTOCOL)
        self.assertIn("seen.SetEquals(expected)", PROTOCOL)
        self.assertIn("ReadBoundedContentAsync", PROTOCOL)
        self.assertNotIn("ReadAsByteArrayAsync", PROTOCOL)
        self.assertNotIn('"api/status"', PROTOCOL)
        self.assertNotIn("http://localhost", PROTOCOL)

    def test_protocol_uses_only_documented_rpc_sequence_and_terminal_events(self):
        ask = PROTOCOL[PROTOCOL.index("public async Task<string> AskAsync"):PROTOCOL.index("private async Task ProbeExactHealthAsync")]
        self.assertLess(ask.index('"session.create"'), ask.index('"prompt.submit"'))
        self.assertIn('gatewayEvent.Type == "message.delta"', ask)
        self.assertIn('gatewayEvent.Type != "message.complete"', ask)
        self.assertIn('["session_id"] = sessionId', ask)
        self.assertIn('["close_on_disconnect"] = true', ask)
        self.assertIn("pendingEvent ??= events.Reader.ReadAsync", ask)
        self.assertIn("pendingEvent = null", ask)
        self.assertIn('if (status != "complete")', ask)
        self.assertNotRegex(PROTOCOL, re.compile(r"mock|fallback", re.IGNORECASE))

    def test_cancel_requires_bounded_server_confirmation(self):
        interrupt = PROTOCOL[PROTOCOL.index("private async Task<bool> TryInterruptAsync"):PROTOCOL.index("private async Task ProbeExactHealthAsync")]
        self.assertIn('"session.interrupt"', interrupt)
        self.assertIn('CancelAfter(TimeSpan.FromSeconds(5))', interrupt)
        self.assertIn('ReadOptionalString(result, "status") == "interrupted"', interrupt)
        self.assertIn("XassAiCancellationException", PROTOCOL)
        self.assertIn("остановка не подтверждена", UI)
        self.assertIn("Состояние запроса неизвестно", UI)

    def test_failed_turn_retires_pending_event_read_before_releasing_gate(self):
        cleanup = PROTOCOL[PROTOCOL.index("        finally\n        {", PROTOCOL.index("public async Task<string> AskAsync")):
                           PROTOCOL.index("    private async Task<bool> TryInterruptAsync")]
        self.assertLess(cleanup.index("turn.Cancel()"), cleanup.index("await pendingEvent"))
        self.assertLess(cleanup.index("await pendingEvent"), cleanup.index("promptGate.Release()"))
        dispose = PROTOCOL[PROTOCOL.index("public async ValueTask DisposeAsync"):]
        self.assertIn("await promptGate.WaitAsync", dispose)
        self.assertNotIn("promptGate.Dispose", dispose)
        self.assertNotIn("sendGate.Dispose", dispose)

    def test_tokens_are_memory_only_and_redacted_from_diagnostics(self):
        endpoint = PROTOCOL[PROTOCOL.index("public sealed class XassAiRuntimeEndpoint"):PROTOCOL.index("internal readonly record struct")]
        self.assertIn("private readonly string sessionToken", endpoint)
        self.assertNotIn("public string SessionToken", endpoint)
        self.assertIn("public override string ToString() => BaseUri", endpoint)
        self.assertNotIn("WriteAllText", RUNTIME)
        self.assertNotIn("WriteAllBytes", RUNTIME)

    def test_lifecycle_cannot_launch_discover_or_stop_a_foreign_process(self):
        self.assertNotIn("Process.Start", RUNTIME)
        self.assertNotIn("GetProcesses", RUNTIME)
        self.assertNotIn("GetProcessById", RUNTIME)
        self.assertIn("Process? child = Interlocked.Exchange(ref ownedChild, null)", RUNTIME)
        self.assertIn("child.Kill(entireProcessTree: true)", RUNTIME)
        self.assertIn('TryGetProperty("port"', RUNTIME)
        self.assertIn("MaximumReadyFileBytes = 4096", RUNTIME)

    def test_streaming_is_throttled_off_the_winui_textbox(self):
        self.assertIn("XassAiStreamBuffer", UI)
        self.assertIn("TimeSpan.FromMilliseconds(75)", UI)
        self.assertIn("FlushXassAiStream", UI)
        self.assertNotIn("progress?.Report", UI)


if __name__ == "__main__":
    unittest.main()
