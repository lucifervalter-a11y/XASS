from __future__ import annotations

import queue
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

CLIENT_ROOT = Path(__file__).resolve().parents[1] / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import client_agent
import client_update
from e2e_crypto import generate_keypair, seal_text, unseal_text


class StopLoop(BaseException):
    pass


class PcCommandLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        directory = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        self.context.enter_context(patch.object(client_agent, "PROCESSED_COMMANDS_PATH", directory / "processed.json"))
        self.context.enter_context(patch.object(client_update, "RESULTS_PATH", directory / "results.json"))
        self.context.enter_context(patch.object(client_agent, "archive_cursor", return_value=0))
        self.context.enter_context(patch.object(client_agent, "is_installer_build", return_value=False))
        self.status = self.context.enter_context(patch.object(client_agent, "write_agent_status"))
        self.archive = self.context.enter_context(patch.object(client_agent, "_ArchiveSyncWorker"))
        self.archive.return_value.busy = False
        self.telemetry = self.context.enter_context(patch.object(client_agent, "build_payload", return_value={"metrics": {}}))
        self.context.enter_context(patch("music_bridge.start_music_bridge", return_value=True))
        self.clock = 100.0
        self.drain_commands = True
        self.sleeps: list[float] = []
        self.payloads: list[dict] = []
        self.context.enter_context(patch.object(client_agent.time, "monotonic", side_effect=lambda: self.clock))
        self.context.enter_context(patch.object(client_agent.time, "sleep", side_effect=self.sleep))
        self.config = {"server_url": "https://agent.invalid", "api_key": "test", "source_name": "PC", "interval_sec": 30, "auto_update": False}

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds
        if self.drain_commands:
            self.assertTrue(client_agent.wait_for_agent_commands(2), "command worker did not finish")

    def test_heartbeat_rebuilds_pool_and_switches_dns_after_transport_failure(self) -> None:
        first, second = MagicMock(), MagicMock()
        first.post.side_effect = httpx.ConnectError("adapter changed")
        second.post.return_value = "reconnected"
        first_context, second_context = MagicMock(), MagicMock()
        first_context.__enter__.return_value = first
        second_context.__enter__.return_value = second
        with patch.object(client_agent, "create_http_client", side_effect=[first_context, second_context]) as factory, \
                patch.object(client_agent, "invalidate_network_state") as invalidate, \
                patch.object(client_agent, "activate_physical_fallback", return_value="192.168.1.111"), \
                patch.object(client_agent, "preferred_connection", return_value=(True, "192.168.1.111")), \
                patch.object(client_agent, "network_signature", return_value=(("wi-fi", "192.168.1.111", True),)):
            with client_agent._HeartbeatClient("https://xass.example", timeout=20, trust_env=False) as client:
                with self.assertRaises(httpx.ConnectError):
                    client.post("https://xass.example/agent/heartbeat")
                self.assertEqual(client.post("https://xass.example/agent/heartbeat"), "reconnected")
        self.assertFalse(factory.call_args_list[0].kwargs["prefer_system_dns"])
        self.assertTrue(factory.call_args_list[1].kwargs["prefer_system_dns"])
        self.assertEqual(factory.call_args_list[1].kwargs["local_address"], "192.168.1.111")
        invalidate.assert_called_once_with("xass.example")

    def test_heartbeat_never_sends_headers_to_another_origin(self) -> None:
        context, raw = MagicMock(), MagicMock()
        context.__enter__.return_value = raw
        with patch.object(client_agent, "create_http_client", return_value=context), \
                patch.object(client_agent, "network_signature", return_value=()):
            with client_agent._HeartbeatClient("https://xass.example", timeout=20, trust_env=False) as client:
                with self.assertRaises(ValueError):
                    client.post("https://attacker.example/collect", headers={"X-Api-Key": "secret"})
        raw.post.assert_not_called()

    def test_https_backend_discovery_never_downgrades_to_plaintext(self) -> None:
        candidates = client_agent._build_server_candidates("https://redvps.site")
        self.assertEqual(candidates, ["https://redvps.site", "https://redvps.site:8001"])
        self.assertTrue(all(value.startswith("https://") for value in candidates))

        raw = MagicMock()
        raw.get.side_effect = httpx.ConnectError("unreachable")
        context = MagicMock()
        context.__enter__.return_value = raw
        with patch.object(client_agent, "create_http_client", return_value=context):
            selected = client_agent.discover_backend_url("https://redvps.site")
        self.assertEqual(selected, "https://redvps.site")
        self.assertTrue(all(call.args[0].startswith("https://") for call in raw.get.call_args_list))

    def test_bare_remote_server_defaults_to_https_but_loopback_stays_http(self) -> None:
        self.assertEqual(client_agent.normalize_server_url("redvps.site"), "https://redvps.site")
        self.assertEqual(client_agent.normalize_server_url("51.250.80.137"), "https://51.250.80.137")
        self.assertEqual(client_agent.normalize_server_url("redvps.site:8001"), "https://redvps.site:8001")
        self.assertEqual(client_agent.normalize_server_url("127.0.0.1"), "http://127.0.0.1:8001")
        self.assertEqual(client_agent.normalize_server_url("localhost:9000"), "http://localhost:9000")

    def test_pairing_refuses_remote_http_before_sending_code(self) -> None:
        with patch.object(client_agent, "create_http_client") as factory, \
                patch.dict(client_agent.os.environ, {"XASS_ALLOW_INSECURE_HTTP": ""}):
            with self.assertRaisesRegex(RuntimeError, "не передаёт код привязки"):
                client_agent.claim_pair_code(
                    server_url="http://example.invalid:8001",
                    pair_code="one-time-secret",
                    source_name="PC",
                    source_type="PC_AGENT",
                )
        factory.assert_not_called()

    def test_remote_http_requires_explicit_development_opt_in(self) -> None:
        self.assertEqual(
            client_agent.require_secret_transport("http://example.invalid:8001", allow_insecure_http=True),
            "http://example.invalid:8001",
        )
        self.assertEqual(
            client_agent.require_secret_transport("http://127.0.0.1:8001"),
            "http://127.0.0.1:8001",
        )

    def test_agent_refuses_remote_http_before_building_heartbeat_client(self) -> None:
        config = {**self.config, "server_url": "http://example.invalid:8001"}
        with patch.object(client_agent, "_HeartbeatClient") as heartbeat, \
                patch.dict(client_agent.os.environ, {"XASS_ALLOW_INSECURE_HTTP": ""}):
            with self.assertRaisesRegex(RuntimeError, "не передаёт код привязки"):
                client_agent.run_agent(config)
        heartbeat.assert_not_called()

    def run_responses(self, responses: list[dict | Exception]) -> None:
        pending = iter(responses)
        client = MagicMock()

        def post(_url, *, headers, json):
            self.payloads.append(json)
            try:
                body = next(pending)
            except StopIteration:
                raise StopLoop()
            if isinstance(body, Exception):
                raise body
            return httpx.Response(200, json=body, request=httpx.Request("POST", _url))

        client.post.side_effect = post
        context = MagicMock()
        context.__enter__.return_value = client
        with patch.object(client_agent, "create_http_client", return_value=context), self.assertRaises(StopLoop):
            client_agent.run_agent(self.config)

    def test_command_result_is_acknowledged_promptly_without_recollecting_telemetry(self) -> None:
        self.run_responses([{"commands": [{"id": 1, "command": "ping"}]}, {}, {}])
        self.assertEqual(self.sleeps[:2], [0.1, 5.0])
        self.assertEqual(self.payloads[1]["command_results"][0]["id"], 1)
        self.assertTrue(self.payloads[1]["command_results"][0]["ok"])
        self.assertEqual(self.payloads[2]["command_results"], [])
        self.telemetry.assert_called_once()

    def test_music_commands_are_not_replayed_and_status_refreshes_each_heartbeat(self) -> None:
        snapshots = [{"state": "idle"}, {"state": "loading"}, {"state": "playing"}, {"state": "paused"}]
        with patch.object(client_agent, "handle_music_command", return_value={"state": "loading"}) as handle, \
                patch.object(client_agent, "music_snapshot", side_effect=snapshots):
            command = {"id": 800, "command": "music_play", "payload": {"track_id": 7}}
            self.run_responses([{"commands": [command]}, {"commands": [command]}, {}])
        handle.assert_called_once_with("music_play", {"track_id": 7}, self.config)
        self.assertEqual([row["music_player"]["state"] for row in self.payloads[:3]], ["idle", "loading", "playing"])
        self.assertEqual(self.payloads[1]["command_results"][0]["details"]["state"], "loading")
        self.telemetry.assert_called_once()

    def test_music_exception_reports_failed_command_not_failed_heartbeat_or_token(self) -> None:
        with patch.object(client_agent, "handle_music_command", side_effect=RuntimeError("secret ticket")):
            self.run_responses([{"commands": [{"id": 801, "command": "music_play"}]}, {}])
        result = self.payloads[1]["command_results"][0]
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", result["message"])
        self.assertEqual(self.status.call_args.args[0], "online")

    def test_duplicate_update_commands_all_receive_results_and_only_one_update_runs(self) -> None:
        def update(_config, _manifest, command_id):
            client_agent.store_command_result(command_id, True, "Обновление проверено")
            return None

        with patch.object(client_agent, "_apply_update", side_effect=update) as apply:
            self.run_responses([{
                "commands": [{"id": 11, "command": "update"}, {"id": 12, "command": "update"}],
                "update": {"available": True, "version": "99.0.0"},
            }, {}])
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[2], 11)
        results = {row["id"]: row for row in self.payloads[1]["command_results"]}
        self.assertEqual(set(results), {11, 12})
        self.assertTrue(results[11]["ok"])
        self.assertFalse(results[12]["ok"])
        self.assertEqual(results[12]["details"]["duplicate_of"], 11)

    def test_interrupted_command_is_reported_without_replaying_lock(self) -> None:
        client_agent.mark_command_processed(42, "lock")
        with patch.object(client_agent, "_lock_workstation") as lock:
            self.run_responses([{"commands": [{"id": 42, "command": "lock"}]}, {}])
        lock.assert_not_called()
        result = self.payloads[1]["command_results"][0]
        self.assertEqual(result["id"], 42)
        self.assertFalse(result["ok"])
        self.assertTrue(result["details"]["interrupted"])

    def test_redelivery_preserves_a_durable_result_without_replaying_the_action(self) -> None:
        client_agent.mark_command_processed(42, "lock")
        client_agent.store_command_result(42, True, "Экран заблокирован")
        with patch.object(client_agent, "_lock_workstation") as lock:
            self.run_responses([{"commands": [{"id": 42, "command": "lock"}]}, {}])
        lock.assert_not_called()
        self.assertTrue(self.payloads[1]["command_results"][0]["ok"])
        self.assertEqual(self.payloads[1]["command_results"][0]["message"], "Экран заблокирован")

    def test_heartbeat_timestamp_survives_a_connection_failure(self) -> None:
        self.run_responses([{}, httpx.ConnectError("offline")])
        reports = [call for call in self.status.call_args_list if call.args[0] in {"online", "offline"}]
        self.assertEqual([call.args[0] for call in reports], ["online", "offline"])
        self.assertGreater(reports[0].kwargs["heartbeat_at"], 0)
        self.assertEqual(reports[0].kwargs["heartbeat_at"], reports[1].kwargs["heartbeat_at"])

    def test_lost_response_keeps_result_until_a_successful_acknowledgement(self) -> None:
        client_agent.store_command_result(7, True, "Готово")
        self.run_responses([httpx.ConnectError("offline"), {}, {}])
        self.assertEqual(self.payloads[0]["command_results"], self.payloads[1]["command_results"])
        self.assertEqual(self.payloads[2]["command_results"], [])
        self.assertEqual(self.payloads[2]["last_error"], "")

    def configure_e2e(self):
        owner_private, owner_public = generate_keypair()
        agent_private, agent_public = generate_keypair()
        self.config.update({"e2e_private_jwk": agent_private, "e2e_public_jwk": agent_public, "owner_e2e_public_jwk": owner_public})
        return owner_private, agent_public

    def test_clipboard_result_stays_encrypted_through_immediate_acknowledgement(self) -> None:
        owner_private, agent_public = self.configure_e2e()
        with patch.object(client_agent, "clipboard_get", return_value="секретный текст"):
            self.run_responses([{"commands": [{"id": 21, "command": "clipboard_get"}]}, {}])
        result = self.payloads[1]["command_results"][0]
        self.assertTrue(result["ok"])
        self.assertTrue(result["details"]["sealed"])
        self.assertNotIn("text", result["details"])
        self.assertEqual(unseal_text(result["details"], private_jwk=owner_private, peer_public_jwk=agent_public, aad="clipboard"), "секретный текст")
        self.assertEqual(self.sleeps[0], 0.1)

    def test_sealed_clipboard_command_is_decrypted_with_the_clipboard_aad(self) -> None:
        owner_private, agent_public = self.configure_e2e()
        payload = seal_text("текст с iPhone", private_jwk=owner_private, peer_public_jwk=agent_public, aad="clipboard")
        with patch.object(client_agent, "clipboard_set", return_value=13) as clipboard:
            self.run_responses([{"commands": [{"id": 22, "command": "clipboard_set", "payload": payload}]}, {}])
        clipboard.assert_called_once_with("текст с iPhone")
        result = self.payloads[1]["command_results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["details"], {"length": 13})

    def test_sealed_clipboard_without_peer_keys_does_not_clear_local_clipboard(self) -> None:
        with patch.object(client_agent, "clipboard_set") as clipboard:
            self.run_responses([{"commands": [{"id": 23, "command": "clipboard_set", "payload": {"sealed": True, "blob": "unreadable"}}]}, {}])
        clipboard.assert_not_called()
        self.assertFalse(self.payloads[1]["command_results"][0]["ok"])

    def test_ciphertext_for_another_purpose_is_rejected_without_modifying_clipboard(self) -> None:
        owner_private, agent_public = self.configure_e2e()
        payload = seal_text("must not appear", private_jwk=owner_private, peer_public_jwk=agent_public, aad="file_upload")
        with patch.object(client_agent, "clipboard_set") as clipboard:
            self.run_responses([{"commands": [{"id": 24, "command": "clipboard_set", "payload": payload}]}, {}])
        clipboard.assert_not_called()
        result = self.payloads[1]["command_results"][0]
        self.assertFalse(result["ok"])
        self.assertIn("расшифровать", result["message"])

    def test_file_download_does_not_block_the_next_heartbeat(self) -> None:
        self.drain_commands = False
        release = threading.Event()
        logged: list[tuple[int, int, str]] = []

        def upload(*_args, **_kwargs):
            release.wait(2)
            return {"filename": "song.mp3"}

        def capture(command_id: int, attempt: int, _duration_ms: float, name: str) -> None:
            logged.append((command_id, attempt, name))

        pending = iter([
            {"commands": [{"id": 5, "command": "file_download", "payload": {"path": "secret-path"}, "attempt": 4}]},
            {},
        ])
        client = MagicMock()

        def post(_url, *, headers, json):
            self.payloads.append(json)
            if len(self.payloads) == 2:
                self.assertNotIn(5, [row.get("id") for row in json.get("command_results") or []])
                self.assertNotIn("secret-path", str(json))
                release.set()
            try:
                body = next(pending)
            except StopIteration:
                raise StopLoop()
            return httpx.Response(200, json=body, request=httpx.Request("POST", _url))

        client.post.side_effect = post
        context = MagicMock()
        context.__enter__.return_value = client
        with (
            patch.object(client_agent, "upload_requested_file", side_effect=upload),
            patch.object(client_agent, "_log_command", side_effect=capture),
            patch.object(client_agent, "create_http_client", return_value=context),
            self.assertRaises(StopLoop),
        ):
            client_agent.run_agent(self.config)
        self.assertGreaterEqual(len(self.payloads), 2)
        self.assertEqual(logged, [(5, 4, "file_download")])
        self.assertNotIn("secret-path", str(logged))

    def test_full_queue_reports_busy_without_stalling_heartbeat(self) -> None:
        self.drain_commands = False
        release = threading.Event()
        original = queue.Queue.put_nowait
        state = {"n": 0}

        def put_nowait(queue_self, item):
            state["n"] += 1
            if state["n"] > 1:
                raise queue.Full
            return original(queue_self, item)

        def block(_command_id: int) -> None:
            release.wait(3)

        pending = iter([{"commands": [{"id": 1, "command": "lock"}, {"id": 2, "command": "lock"}]}, {}])
        client = MagicMock()

        def post(_url, *, headers, json):
            self.payloads.append(json)
            if len(self.payloads) == 2:
                self.assertIn("агент занят", [row.get("message") for row in json.get("command_results") or []])
                release.set()
                raise StopLoop()
            try:
                body = next(pending)
            except StopIteration:
                raise StopLoop()
            return httpx.Response(200, json=body, request=httpx.Request("POST", _url))

        client.post.side_effect = post
        context = MagicMock()
        context.__enter__.return_value = client
        with (
            patch.object(queue.Queue, "put_nowait", put_nowait),
            patch.object(client_agent, "_lock_workstation", side_effect=block),
            patch.object(client_agent, "create_http_client", return_value=context),
            self.assertRaises(StopLoop),
        ):
            client_agent.run_agent(self.config)


class ArchiveWorkerTests(unittest.TestCase):
    def test_archive_download_does_not_block_submit_or_start_duplicate_workers(self) -> None:
        started, release = threading.Event(), threading.Event()

        def sync(*_args, **_kwargs):
            started.set()
            if not release.wait(2):
                raise RuntimeError("test did not release archive worker")
            return {"saved": 1, "cursor": 1}

        worker = client_agent._ArchiveSyncWorker()
        with patch.object(client_agent, "apply_archive_events", side_effect=sync) as apply, patch.object(client_agent, "create_http_client"):
            try:
                worker.submit({"server_url": "https://agent.invalid"}, {"archive_enabled": True, "archive_events": [{"event_id": 1}]}, {})
                self.assertTrue(started.wait(1))
                self.assertTrue(worker.busy)
                worker.submit({"server_url": "https://agent.invalid"}, {"archive_enabled": True, "archive_events": [{"event_id": 2}]}, {})
                apply.assert_called_once()
            finally:
                release.set()
                if worker.thread:
                    worker.thread.join(2)
        self.assertFalse(worker.busy)


if __name__ == "__main__":
    unittest.main()
