from __future__ import annotations

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
        self.clock = 100.0
        self.sleeps: list[float] = []
        self.payloads: list[dict] = []
        self.context.enter_context(patch.object(client_agent.time, "monotonic", side_effect=lambda: self.clock))
        self.context.enter_context(patch.object(client_agent.time, "sleep", side_effect=self.sleep))
        self.config = {"server_url": "https://agent.invalid", "api_key": "test", "source_name": "PC", "interval_sec": 30, "auto_update": False}

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds

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
