"""Synthetic-data and mocked-process native parity contracts; no live PC actions."""
from __future__ import annotations
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

WINDOWS = Path(__file__).resolve().parents[1]
SOURCE = WINDOWS.parent / "pc_client"
sys.path.insert(0, str(WINDOWS)); sys.path.insert(0, str(SOURCE))
from desktop_bridge import DesktopService, SCHEMAS, config_lock, number, redact, read_json, bind_runtime

class NativeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XASS_DATA_ROOT": str(self.root), "LOCALAPPDATA": str(self.root)})
        env.start(); self.addCleanup(env.stop)
        self.service = DesktopService.__new__(DesktopService)
        self.service.source = SOURCE; self.service.data = self.root
        self.config = {"api_key": "ag_synthetic_secret_key", "server_url": "https://source.invalid", "source_name": "Committed PC",
            "owner_name": "Owner", "interval_sec": 30, "auto_update": True, "archive_folder": str(self.root / "archive")}
        self.write("config.json", self.config)
    def write(self, name, value):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value))
    def settings(self):
        return {"owner_name": "Name", "interval_sec": 15, "auto_update": False, "transcription_enabled": False,
                "archive_folder": "", "archive_max_gb": 3.5, "archive_retention_days": 30}
    def profile_payload(self, **changes):
        return {"format": "xass-connect", "version": 1, "pair_code": "profile-code-fixture",
                "server_url": "https://profile.invalid", "source_name": "Profile PC",
                "expires_at": "2099-01-01T00:00:00Z", "auto_update": False, **changes}
    @contextlib.contextmanager
    def pairing_dependencies(self):
        # All mutation dependencies are synthetic, including key generation and
        # both discovery/claim network entry points. No live pairing is exercised.
        public = {"kty": "EC", "crv": "P-256", "x": "fixture-x", "y": "fixture-y"}
        def keys(config):
            config.update(e2e_public_jwk=public, e2e_private_jwk={**public, "d": "fixture-private-key"})
        with contextlib.ExitStack() as stack:
            def mock(target, **kwargs):
                return stack.enter_context(patch(target, **kwargs))
            yield SimpleNamespace(
                config=stack.enter_context(patch.object(self.service, "config", return_value=dict(self.config))),
                save=stack.enter_context(patch.object(self.service, "save")),
                normalize=mock("client_agent.normalize_server_url", side_effect=lambda value: value),
                discover=mock("client_agent.discover_backend_url", side_effect=lambda value: value),
                claim=mock("client_agent.claim_pair_code", return_value={"agent_api_key": "ag_new_fixture_secret"}),
                keys=mock("e2e_crypto.ensure_agent_keys", side_effect=keys), public=public)
    def assert_pairing_untouched(self, dependencies):
        for operation in (dependencies.config, dependencies.save, dependencies.normalize,
                          dependencies.discover, dependencies.claim, dependencies.keys):
            operation.assert_not_called()
        self.assertFalse((self.root / ".native-config.lock").exists())
        self.assertEqual(read_json(self.root / "config.json"), self.config)
    def test_exact_operations_and_unknown_fields_rejected_before_io(self):
        for action in ["exec", "reboot", "shutdown", "delete_file", "desktop_exec"]:
            with self.assertRaises(ValueError): self.service.request(action, {})
        for action in SCHEMAS:
            with self.subTest(action=action), self.assertRaises(ValueError): self.service.request(action, {"untrusted": 1})
    def test_protocol_version_rejected(self):
        for value in (2, True, "1"):
            with self.assertRaises(ValueError): self.service.request("desktop_settings", {"version": value})
    def test_numeric_limits_reject_bool_nan_and_strings(self):
        for value in [True, "5", float("nan"), float("inf"), -1, 999999]:
            with self.subTest(value=value), self.assertRaises(ValueError): number(value, 5, 86400, integer=True)
    def test_settings_projection_excludes_credentials_host_and_pairing(self):
        result = self.service.settings()
        self.assertNotIn("api_key", result); self.assertNotIn("server_url", result)
        self.assertNotIn("source_name", result); self.assertNotIn("ag_", json.dumps(result))
    def test_settings_atomic_commit_preserves_host_name_and_key(self):
        with patch.object(self.service, "config", return_value=dict(self.config)), patch.object(self.service, "save") as save:
            result = self.service.request("desktop_save_settings", {"settings": self.settings()})
        self.assertTrue(result["saved"])
        self.assertEqual(save.call_args.args[0]["server_url"], "https://source.invalid")
        self.assertEqual(save.call_args.args[0]["source_name"], "Committed PC")
        self.assertEqual(save.call_args.args[0]["api_key"], self.config["api_key"])
    def test_failed_settings_write_does_not_mutate_original_config(self):
        original = dict(self.config)
        with patch.object(self.service, "config", return_value=dict(self.config)), patch.object(self.service, "save", side_effect=OSError):
            with self.assertRaises(OSError): self.service.request("desktop_save_settings", {"settings": self.settings()})
        self.assertEqual(self.config, original)
        self.assertEqual(read_json(self.root / "config.json"), original)
    def test_settings_rejects_extra_host_and_invalid_values(self):
        for values in [{**self.settings(), "server_url": "https://evil.invalid"}, {**self.settings(), "archive_max_gb": float("nan")},
                       {**self.settings(), "interval_sec": 4}, {**self.settings(), "transcription_enabled": "yes"},
                       {**self.settings(), "archive_folder": "../outside"}]:
            with self.assertRaises(ValueError): self.service.request("desktop_save_settings", {"settings": values})
    def test_config_lock_blocks_second_writer_and_releases(self):
        with config_lock(self.root):
            with self.assertRaises(RuntimeError):
                with config_lock(self.root): pass
        with config_lock(self.root): pass
    def test_corrupt_sealed_config_is_not_replaced_with_empty_secret(self):
        for envelope in [{"cipher": "dpapi"}, "invalid", {"data": "fake", "cipher": "unknown"}]:
            self.write("config.json", {"sealed": envelope})
            with self.assertRaises(ValueError): self.service.config()
    def test_aes_reader_never_provisions_missing_key(self):
        self.write("config.json", {"sealed": {"cipher": "aes-256-gcm", "data": "fake"}})
        with self.assertRaises(ValueError): self.service.config()
        self.assertFalse((self.root / ".xass-master.key").exists())
    def test_unpaired_config_is_empty(self):
        (self.root / "config.json").unlink(); self.assertEqual(self.service.config(), {})
    def test_profile_requires_exactly_one_source_and_expiry(self):
        with self.assertRaises(ValueError): self.service.request("desktop_profile", {"path": "x", "text": "x"})
        profile = {"format": "xass-connect", "version": 1, "pair_code": "123456", "server_url": "https://xass.invalid",
                   "source_name": "Fixture", "expires_at": "2000-01-01T00:00:00Z"}
        with self.assertRaises(ValueError): self.service.request("desktop_profile", {"text": json.dumps(profile)})
        profile["expires_at"] = "2099-01-01T00:00:00Z"
        result = self.service.request("desktop_profile", {"text": json.dumps(profile)})
        self.assertEqual(result["name"], "Fixture"); self.assertNotIn("123456", json.dumps(result))
    def test_profile_rejects_oversized_input(self):
        with self.assertRaises(ValueError): self.service.request("desktop_profile", {"text": "x" * 65537})
    def test_manual_pair_uses_only_exact_manual_values_after_profile_preview(self):
        manual = {"server": "https://manual.invalid/custom", "name": "Manual PC", "code": "manual-code-fixture"}
        with self.pairing_dependencies() as dependencies:
            self.service.request("desktop_profile", {"text": json.dumps(self.profile_payload())})
            with patch.object(self.service, "profile", side_effect=AssertionError("Manual pairing read a profile")):
                result = self.service.request("desktop_pair", manual)
        dependencies.normalize.assert_called_once_with(manual["server"])
        dependencies.discover.assert_called_once_with(manual["server"])
        dependencies.claim.assert_called_once_with(server_url=manual["server"], pair_code=manual["code"],
            source_name=manual["name"], source_type="PC_AGENT", e2e_public_jwk=dependencies.public, allow_insecure_http=False)
        saved = dependencies.save.call_args.args[0]
        self.assertEqual(saved["server_url"], manual["server"])
        self.assertEqual(saved["source_name"], manual["name"])
        self.assertEqual(saved["auto_update"], self.config["auto_update"])
        self.assertEqual(result, {"paired": True, "name": manual["name"], "server": manual["server"]})
        for secret in (manual["code"], self.config["api_key"], "ag_new_fixture_secret", "fixture-private-key"):
            self.assertNotIn(secret, json.dumps(result))
    def test_manual_pair_rejects_legacy_profile_keys_even_when_empty_before_io(self):
        manual = {"server": "https://manual.invalid", "name": "Manual PC", "code": "manual-code-fixture"}
        for extra in ({"path": "profile.xass"}, {"text": json.dumps(self.profile_payload())},
                      {"path": ""}, {"text": ""}, {"path": None, "text": None}):
            with self.subTest(extra=extra), self.pairing_dependencies() as dependencies:
                with patch.object(self.service, "profile") as profile, self.assertRaises(ValueError):
                    self.service.request("desktop_pair", {**manual, **extra})
                profile.assert_not_called()
                self.assert_pairing_untouched(dependencies)
    def test_profile_pair_requires_boolean_confirmation_before_parsing_or_io(self):
        for confirmation in ({}, {"confirmed": False}, {"confirmed": None}, {"confirmed": 1}, {"confirmed": "true"}):
            with self.subTest(confirmation=confirmation), self.pairing_dependencies() as dependencies:
                with patch.object(self.service, "profile") as profile, self.assertRaises(ValueError):
                    self.service.request("desktop_pair_profile", {"text": json.dumps(self.profile_payload()), **confirmation})
                profile.assert_not_called()
                self.assert_pairing_untouched(dependencies)
    def test_profile_pair_rejects_paths_manual_fields_and_missing_text_before_io(self):
        for fields in ({"path": str(self.root / "profile.xass")}, {"path": ""}, {"server": "https://manual.invalid"},
                       {"name": "Manual PC"}, {"code": "manual-code-fixture"}):
            with self.subTest(fields=fields), self.pairing_dependencies() as dependencies:
                with self.assertRaises(ValueError):
                    self.service.request("desktop_pair_profile", {"text": json.dumps(self.profile_payload()), "confirmed": True, **fields})
                self.assert_pairing_untouched(dependencies)
        with self.pairing_dependencies() as dependencies, self.assertRaises(ValueError):
            self.service.request("desktop_pair_profile", {"confirmed": True})
        self.assert_pairing_untouched(dependencies)
    def test_file_and_json_profile_preview_never_write_or_project_credentials(self):
        payload = self.profile_payload(e2e_public_jwk={"kty": "EC", "crv": "P-256", "x": "fixture-x", "y": "fixture-y"})
        self.write("profile.xass", payload)
        original = (self.root / "config.json").read_bytes()
        for source in ({"path": str(self.root / "profile.xass")}, {"text": json.dumps(payload)}):
            with self.subTest(source=next(iter(source))), self.pairing_dependencies() as dependencies:
                result = self.service.request("desktop_profile", source)
                self.assertEqual(set(result), {"server", "name", "expires_at", "auto_update"})
                self.assertEqual(result["server"], payload["server_url"])
                self.assertEqual(result["name"], payload["source_name"])
                for secret in (payload["pair_code"], "ag_", "fixture-x", "fixture-y"):
                    self.assertNotIn(secret, json.dumps(result))
                self.assert_pairing_untouched(dependencies)
                self.assertEqual((self.root / "config.json").read_bytes(), original)
                self.assertEqual({path.name for path in self.root.iterdir()}, {"config.json", "profile.xass"})
    def test_profile_pair_uses_confirmed_text_snapshot_when_original_file_changes(self):
        payload = self.profile_payload()
        snapshot = json.dumps(payload)
        self.write("profile.xass", payload)
        with self.pairing_dependencies() as dependencies:
            self.service.request("desktop_profile", {"path": str(self.root / "profile.xass")})
            self.write("profile.xass", self.profile_payload(server_url="https://changed.invalid", pair_code="changed-code-fixture"))
            with patch("connection_file.load_connection_file", side_effect=AssertionError("Import reread a mutable file")):
                result = self.service.request("desktop_pair_profile", {"text": snapshot, "confirmed": True})
        self.assertEqual(dependencies.claim.call_args.kwargs["server_url"], payload["server_url"])
        self.assertEqual(dependencies.claim.call_args.kwargs["pair_code"], payload["pair_code"])
        self.assertFalse(dependencies.save.call_args.args[0]["auto_update"])
        self.assertEqual(set(result), {"paired", "name", "server"})
        for secret in (payload["pair_code"], "ag_new_fixture_secret", "fixture-private-key"):
            self.assertNotIn(secret, json.dumps(result))
    def test_failed_profile_pair_can_retry_with_fresh_profile_without_stale_credentials(self):
        first = self.profile_payload()
        fresh = self.profile_payload(server_url="https://fresh.invalid", pair_code="fresh-code-fixture", source_name="Fresh PC", auto_update=True)
        original = (self.root / "config.json").read_bytes()
        with self.pairing_dependencies() as dependencies:
            dependencies.claim.side_effect = [RuntimeError("Synthetic claim rejected"), {"agent_api_key": "ag_new_fixture_secret"}]
            with self.assertRaisesRegex(RuntimeError, "Synthetic claim rejected"):
                self.service.request("desktop_pair_profile", {"text": json.dumps(first), "confirmed": True})
            dependencies.save.assert_not_called()
            self.assertEqual(dependencies.config.return_value, self.config)
            self.assertEqual((self.root / "config.json").read_bytes(), original)
            result = self.service.request("desktop_pair_profile", {"text": json.dumps(fresh), "confirmed": True})
        self.assertEqual([call.kwargs["pair_code"] for call in dependencies.claim.call_args_list], [first["pair_code"], fresh["pair_code"]])
        self.assertEqual([call.kwargs["server_url"] for call in dependencies.claim.call_args_list], [first["server_url"], fresh["server_url"]])
        dependencies.save.assert_called_once()
        saved = dependencies.save.call_args.args[0]
        self.assertEqual((saved["server_url"], saved["source_name"], saved["auto_update"]), (fresh["server_url"], fresh["source_name"], True))
        self.assertEqual(dependencies.config.return_value, self.config)
        for secret in (first["pair_code"], fresh["pair_code"], "ag_new_fixture_secret", "fixture-private-key"):
            self.assertNotIn(secret, json.dumps(result))
    def test_profile_expiring_after_preview_is_revalidated_and_saved_config_is_preserved(self):
        from connection_file import parse_connection_text
        snapshot = json.dumps(self.profile_payload())
        original = (self.root / "config.json").read_bytes()
        now = datetime(2098, 1, 1, tzinfo=timezone.utc)
        with self.pairing_dependencies() as dependencies, patch("connection_file.parse_connection_text", side_effect=lambda value: parse_connection_text(value, now=now)):
            self.service.request("desktop_profile", {"text": snapshot})
            now = datetime(2100, 1, 1, tzinfo=timezone.utc)
            with self.assertRaises(ValueError):
                self.service.request("desktop_pair_profile", {"text": snapshot, "confirmed": True})
            self.assert_pairing_untouched(dependencies)
        self.assertEqual((self.root / "config.json").read_bytes(), original)
    def test_pairing_protocol_never_emits_secrets_on_success_or_failure(self):
        spec = importlib.util.spec_from_file_location("pairing_bridge_fixture", WINDOWS / "bridge.py")
        bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
        payload = self.profile_payload()
        request = {"action": "desktop_pair_profile", "text": json.dumps(payload), "confirmed": True}
        secrets = (payload["pair_code"], self.config["api_key"], "ag_new_fixture_secret", "fixture-private-key")
        for failed in (False, True):
            with self.subTest(failed=failed), self.pairing_dependencies() as dependencies:
                def claim(**kwargs):
                    # Even accidental dependency output and exception details must
                    # remain inside the bridge's private protocol boundary.
                    print(" ".join(secrets))
                    print(" ".join(secrets), file=sys.stderr)
                    if failed:
                        raise RuntimeError(" ".join(secrets))
                    return {"agent_api_key": "ag_new_fixture_secret"}
                dependencies.claim.side_effect = claim
                output, errors = io.StringIO(), io.StringIO()
                with patch("desktop_bridge.DesktopService", return_value=self.service), \
                        patch.object(sys, "argv", ["bridge.py", "--source", str(SOURCE), "--data", str(self.root)]), \
                        patch.object(sys, "stdin", io.StringIO(json.dumps(request))), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    bridge.main()
                self.assertEqual(json.loads(output.getvalue())["ok"], not failed)
                self.assertEqual(errors.getvalue(), "")
                for secret in secrets:
                    self.assertNotIn(secret, output.getvalue())
                if failed:
                    dependencies.save.assert_not_called()
    def test_files_allowlist_nested_navigation_truncation_and_traversal(self):
        home = self.root / "home"; home.mkdir()
        with patch.dict(os.environ, {"USERPROFILE": str(home)}):
            initial = self.service.request("desktop_files", {"root": "downloads", "path": ""})
            self.assertEqual(initial["entries"], [])
            folder = home / "Downloads" / "Русская папка"; folder.mkdir(); (folder / "child.txt").write_text("hello")
            result = self.service.request("desktop_files", {"root": "downloads", "path": "Русская папка"})
            self.assertEqual(result["entries"][0]["name"], "child.txt")
            for i in range(260): (home / "Downloads" / f"f{i:03}.txt").touch()
            result = self.service.request("desktop_files", {"root": "downloads", "path": ""})
            self.assertTrue(result["truncated"]); self.assertEqual(len(result["entries"]), 250)
            for root, path in [("system", ""), ("downloads", "../"), ("downloads", str(self.root))]:
                with self.subTest(root=root,path=path), self.assertRaises((ValueError, PermissionError)):
                    self.service.request("desktop_files", {"root": root, "path": path})
    def test_files_does_not_follow_escaping_symlink(self):
        home = self.root / "home"; home.mkdir()
        with patch.dict(os.environ, {"USERPROFILE": str(home)}):
            self.service.request("desktop_files", {"root": "downloads"})
            (home / "Downloads" / "escape").symlink_to(self.root, target_is_directory=True)
            result = self.service.request("desktop_files", {"root": "downloads"})
            self.assertNotIn("escape", [v["name"] for v in result["entries"]])
    def test_archive_probe_leaves_no_artifact(self):
        result = self.service.request("desktop_archive_probe", {"path": str(self.root)})
        self.assertTrue(result["writable"]); self.assertFalse(list(self.root.glob(".xass-write-test-*")))
    def test_lock_requires_explicit_confirmation(self):
        with self.assertRaises(ValueError): self.service.request("desktop_lock", {"confirmed": False})
    def test_redaction_strips_tokens_url_credentials_and_query(self):
        value = redact("api_key=ag_secretabc https://xass.invalid/path?token=private https://user:pass@secret.invalid/ abc-private", ("abc-private",))
        for secret in ["ag_secretabc", "private", "user:pass"]: self.assertNotIn(secret, value)
    def test_status_does_not_decrypt_and_rejects_dead_pid(self):
        self.write(".agent-status.json", {"process_id": 1234, "updated_at": 100, "state": "online", "detail": "ag_privatekey"})
        import psutil
        with patch("psutil.Process", side_effect=psutil.NoSuchProcess(1234)), patch.object(self.service, "config") as config:
            result = self.service.status()
        config.assert_not_called(); self.assertEqual(result["state"], "stopped"); self.assertNotIn("ag_privatekey", json.dumps(result))
    def test_status_checks_pid_creation_future_and_interval_freshness(self):
        process = Mock(); process.cmdline.return_value = ["XASS.NativeHelper.exe", "--role", "background-agent", "--agent-child"]
        process.is_running.return_value = True; process.create_time.return_value = 500
        self.config["interval_sec"] = 120; self.write("config.json", self.config)
        self.write(".agent-status.json", {"process_id": 1234, "updated_at": 900, "state": "online", "latency_ms": 10})
        with patch("psutil.Process", return_value=process), patch("desktop_bridge.time.time", return_value=1100):
            self.assertEqual(self.service.status()["state"], "online")
        for timestamp in [400, 2000]:
            self.write(".agent-status.json", {"process_id": 1234, "updated_at": timestamp, "state": "online"})
            with patch("psutil.Process", return_value=process), patch("desktop_bridge.time.time", return_value=1100):
                self.assertEqual(self.service.status()["state"], "stopped")
    def test_archive_projection_preserves_text_and_markers(self):
        rows = [{"id": 42, "text_content": "hello", "deleted": 1, "forwarded_from": "name", "reply_to_message_id": 6,
                 "media_count": 2, "message_date": "today", "api_key": "secret"}]
        with patch.object(self.service,"config",return_value=self.config), patch("archive_store.conversation_rows", return_value=rows):
            result = self.service.request("desktop_archive_rows", {})
            detail = self.service.request("desktop_archive_detail", {"id": 42})
        self.assertEqual(result["rows"][0]["text"], "hello"); self.assertEqual(detail["text"], "hello")
        self.assertEqual(result["rows"][0]["forwarded_from"], "name"); self.assertNotIn("api_key", result["rows"][0])
    def test_500_unicode_archive_previews_fit_bounded_desktop_envelope(self):
        rows = [{"id":i,"text_content":"😀"*1000,"chat_title":"Я"*240,"forwarded_from":"Я"*240,
                 "from_username":"Я"*240,"message_date":"2026-10-04","direction":"incoming","media_count":1} for i in range(500)]
        with patch.object(self.service,"config",return_value=self.config), patch("archive_store.conversation_rows",return_value=rows):
            result=self.service.request("desktop_archive_rows",{})
        self.assertEqual(len(result["rows"]),500)
        self.assertLess(len(json.dumps({"ok":True,"result":result},ensure_ascii=True)),4*1024*1024)
        self.assertEqual(len(result["rows"][0]["text"]),256)

    def test_runtime_binding_rejects_relative_paths(self):
        with self.assertRaises(ValueError): bind_runtime(Path("pc_client"), self.root)

class NativeUiContracts(unittest.TestCase):
    def test_pairing_ui_reads_current_manual_controls_without_pending_profile_fields(self):
        code = (WINDOWS / "Xass.Native/MainWindow.Desktop.cs").read_text()
        for field in ("pendingProfilePath", "pendingProfileText", "pendingProfile", "pendingConnectionPath", "pendingConnectionText"):
            self.assertNotIn(field, code)
        manual = code.split("private Task PairDesktopAsync()", 1)[1].split("private async Task RunDesktopPairingAsync", 1)[0]
        self.assertIn("desktopPairingFlow.PairManualAsync(", manual)
        self.assertIn("pairServer.Text, pairName.Text, pairCode.Password, client.RequestAsync, cancellation", manual)
        preview = code.split("private Task ImportConnectionSourceAsync", 1)[1].split("private Task PairDesktopAsync()", 1)[0]
        self.assertIn("desktopPairingFlow.ImportAsync(readSource", preview)
        self.assertIn("operationCancellation", preview)
        self.assertNotIn("pairServer.Text =", preview)
        self.assertNotIn("pairName.Text =", preview)
        self.assertNotIn("pairCode.Password =", preview)
    def test_pairing_runtime_guard_covers_path_save_lifecycle_and_pairing_start(self):
        code = (WINDOWS / "Xass.Native/MainWindow.Desktop.cs").read_text()
        paths = code.split("private async Task SaveDesktopPathsAsync()", 1)[1].split("private async Task EnsureDesktopHostAsync()", 1)[0]
        self.assertIn("if (desktopPairing || desktopRuntimeChanging)", paths)
        self.assertLess(paths.index("desktopRuntimeChanging = true;"), paths.index("await "))
        self.assertIn("finally { desktopRuntimeChanging = false; }", paths)
        self.assertLess(paths.index("client.DataPath = data;"), paths.index("finally"))
        pairing = code.split("private async Task RunDesktopPairingAsync", 1)[1].split("private void BuildComputerPanels()", 1)[0]
        self.assertIn("if (desktopPairing || desktopQuitting || closed) return;", pairing)
        self.assertLess(pairing.index("if (desktopRuntimeChanging)"), pairing.index("await operation("))
    def test_pairing_quit_cancels_operation_before_asynchronous_shutdown(self):
        code = (WINDOWS / "Xass.Native/MainWindow.Desktop.cs").read_text()
        pairing = code.split("private async Task RunDesktopPairingAsync", 1)[1].split("private void BuildComputerPanels()", 1)[0]
        self.assertIn("CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token)", pairing)
        self.assertIn("desktopPairingCancellation = cancellation;", pairing)
        self.assertIn("await operation(cancellation.Token)", pairing)
        self.assertIn("desktopPairingCancellation = null;", pairing.split("finally", 1)[1])
        self.assertLess(pairing.index("if (closed || desktopQuitting || cancellation.IsCancellationRequested) return;"),
                        pairing.index("await RestartDesktopAgentAsync();"))
        quit_code = code.split("private async Task QuitDesktopAsync()", 1)[1].split("private void DisposeDesktop()", 1)[0]
        self.assertLess(quit_code.index("desktopPairingCancellation?.Cancel();"), quit_code.index("await "))
    def test_all_legacy_destinations_exist_without_tk_fallback(self):
        code = (WINDOWS / "Xass.Native/MainWindow.Desktop.cs").read_text()
        for tag in ["files", "archive", "journal", "updates", "settings", "commands"]:
            self.assertIn(f'AddDesktopPage("{tag}"', code)
        self.assertNotIn("desktop_app", code)
        for action in ["desktop_files", "desktop_archive_rows", "desktop_save_settings", "desktop_screenshot", "desktop_lock"]:
            self.assertIn(action, code)
        self.assertIn("DesktopPairingFlow", code)
        pairing = (WINDOWS / "Xass.Native/Services/DesktopPairingFlow.cs").read_text()
        for action in ["desktop_pair", "desktop_pair_profile", "desktop_profile"]:
            self.assertIn(f'action = "{action}"', pairing)
    def test_lifecycle_direct_host_no_bridge_spawn_and_close_mutes(self):
        code = (WINDOWS / "Xass.Native/Services/DesktopHostClient.cs").read_text()
        self.assertIn('ArgumentList.Add("background-agent")', code)
        window = (WINDOWS / "Xass.Native/MainWindow.Desktop.cs").read_text()
        self.assertIn("AppWindow.Closing", window); self.assertIn("StopAllMicrophones();", window)
        self.assertIn("fileGeneration", window); self.assertIn("generation != fileGeneration", window)
    def test_native_update_boundaries(self):
        code = (WINDOWS / "Xass.Native/Services/NativeUpdateClient.cs").read_text()
        for check in ["lucifervalter-a11y/XASS", 'StartsWith("native-test-"', "GetProperty(\"prerelease\")", "SHA256", "AllowAutoRedirect = false", "native-test-update.json"]:
            self.assertIn(check, code)
        self.assertNotIn("X-Api-Key", code)

if __name__ == "__main__": unittest.main()

class NativeArchiveHostTests(unittest.TestCase):
    def setUp(self):
        import threading
        from background_agent import DesktopHost
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.source = self.root / "old"; self.source.mkdir(); self.target = self.root / "new"; self.target.mkdir()
        self.env = patch.dict(os.environ, {"XASS_DATA_ROOT": str(self.root), "LOCALAPPDATA": str(self.root)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.host = DesktopHost.__new__(DesktopHost)
        self.host.data = self.root; self.host.source = SOURCE; self.host.lock = threading.RLock(); self.host.stop_event = threading.Event()
        self.host.job_cancel = threading.Event(); self.host.job_thread = None; self.host.music = None
        self.host.job = {"state":"idle", "progress":0}; self.host.state = "stopped"
        self.host.service = Mock(); self.config = {"archive_folder":str(self.source), "api_key":"ag_fixture"}
        self.host.service.config.return_value = self.config
        self.host.stop = Mock(); self.host.start = Mock()
    def test_consistent_archive_copy_rewrites_paths_preserves_source_and_commits_last(self):
        from archive_store import DB_FILE, _connect
        media = self.source / "media"; media.mkdir(); (media / "file.jpg").write_bytes(b"fixture-media")
        with contextlib.closing(_connect(self.source)) as db, db:
            db.execute("INSERT INTO media (asset_id, message_id, local_path, saved) VALUES (1,1,?,1)", (str(media / "file.jpg"),))
        self.host._move_archive(self.target, True)
        self.assertEqual(self.host.job["state"], "completed")
        self.assertEqual((media / "file.jpg").read_bytes(), b"fixture-media")
        self.assertEqual((self.target / "media/file.jpg").read_bytes(), b"fixture-media")
        with contextlib.closing(sqlite3.connect(self.target / DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT local_path FROM media").fetchone()[0], str(self.target / "media/file.jpg"))
        self.assertEqual(self.host.service.save.call_args.args[0]["archive_folder"], str(self.target))
    def test_archive_copy_closes_all_database_handles_before_return(self):
        from archive_store import _connect
        with contextlib.closing(_connect(self.source)) as db, db:
            db.execute("SELECT 1")
        opened = []
        class TrackedConnection(sqlite3.Connection):
            closed = False
            def close(self):
                self.closed = True
                super().close()
        connect = sqlite3.connect
        def tracked(*args, **kwargs):
            connection = connect(*args, factory=TrackedConnection, **kwargs)
            opened.append(connection)
            return connection
        with patch("background_agent.sqlite3.connect", side_effect=tracked):
            self.host._move_archive(self.target, True)
        self.assertEqual(self.host.job["state"], "completed")
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(connection.closed for connection in opened))

    def test_archive_cancel_keeps_config_source_and_no_staging(self):
        (self.source / "fixture.txt").write_text("safe")
        self.host.job_cancel.set(); self.host._move_archive(self.target, True)
        self.assertEqual(self.host.job["state"], "cancelled"); self.host.service.save.assert_not_called()
        self.assertEqual((self.source / "fixture.txt").read_text(), "safe"); self.assertEqual(list(self.target.iterdir()), [])
    def test_archive_rejects_nonempty_destination_without_overwrite(self):
        (self.target / "user.txt").write_text("keep"); self.host._move_archive(self.target, True)
        self.assertEqual(self.host.job["state"], "error"); self.host.service.save.assert_not_called()
        self.assertEqual((self.target / "user.txt").read_text(), "keep")
    def test_archive_rejects_overlapping_target(self):
        target = self.source / "nested"; target.mkdir(); self.host._move_archive(target, True)
        self.assertEqual(self.host.job["state"], "error"); self.host.service.save.assert_not_called()
    def test_archive_save_failure_preserves_existing_config_and_source(self):
        (self.source / "fixture.txt").write_text("safe"); self.host.service.save.side_effect = OSError()
        self.host._move_archive(self.target, True)
        self.assertEqual(self.host.job["state"], "error"); self.assertEqual(self.config["archive_folder"], str(self.source))
        self.assertEqual((self.source / "fixture.txt").read_text(), "safe")
    def test_archive_final_quit_never_restarts_agent(self):
        self.host.stop_event.set(); self.host.job_cancel.set(); self.host._move_archive(self.target, True)
        self.host.start.assert_not_called()
    def test_cleanup_rejects_external_paths_even_with_confirm(self):
        from archive_store import _connect
        outside = self.root / "personal.txt"; outside.write_text("keep")
        with contextlib.closing(_connect(self.source)) as db, db:
            db.execute("INSERT INTO media (asset_id,message_id,local_path,saved) VALUES (1,1,?,1)",(str(outside),))
        with self.assertRaises(ValueError): self.host.request("host_archive_cleanup", {"confirmed": True})
        self.assertEqual(outside.read_text(), "keep")
    def test_destructive_host_commands_require_confirmation(self):
        for action in ["host_archive_move", "host_archive_cleanup"]:
            with self.assertRaises(ValueError): self.host.request(action, {"confirmed":False})
    def test_host_rejects_unknown_actions_and_fields(self):
        for action, request in [("exec",{}),("host_start",{"command":"shutdown"})]:
            with self.assertRaises(ValueError): self.host.request(action,request)
