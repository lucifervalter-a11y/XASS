from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

CLIENT_ROOT = Path(__file__).resolve().parents[1] / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import client_agent
import hermes_client
import themes


class HermesCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "server_url": "https://test.server",
            "api_key": "ag_test",
            "source_name": "PC-TEST",
            "hermes_url": "http://127.0.0.1:8787",
            "theme": "Cyber",
        }
        self.client = MagicMock()

    @patch.object(client_agent, "store_command_result")
    @patch.object(client_agent, "_log_command")
    def test_hermes_status_command(self, mock_log: MagicMock, mock_store: MagicMock) -> None:
        mock_status = {
            "online": True,
            "status": "online",
            "status_label": "В сети",
            "url": "http://127.0.0.1:8787",
            "latency_ms": 12.5,
        }
        with patch.object(hermes_client, "check_hermes_status", return_value=mock_status):
            job = {
                "id": 101,
                "command": "hermes_status",
                "payload": {},
                "attempt": 1,
            }
            res = client_agent._dispatch_agent_command(
                job,
                self.client,
                config=self.config,
                source_name="PC-TEST",
                archive_busy=False,
            )
            self.assertIsNone(res)
            mock_store.assert_called_once()
            call_args = mock_store.call_args[0]
            self.assertEqual(call_args[0], 101)  # command_id
            self.assertTrue(call_args[1])  # success
            self.assertIn("В сети", call_args[2])
            self.assertEqual(call_args[3]["url"], "http://127.0.0.1:8787")

    @patch.object(client_agent, "store_command_result")
    @patch.object(client_agent, "_log_command")
    def test_hermes_prompt_command_success(self, mock_log: MagicMock, mock_store: MagicMock) -> None:
        mock_result = {
            "ok": True,
            "online": True,
            "prompt": "Какая сегодня погода?",
            "response": "Сегодня солнечная погода, +20°C.",
            "url": "http://127.0.0.1:8787",
            "latency_ms": 150.0,
        }
        with patch.object(hermes_client, "send_hermes_prompt", return_value=mock_result):
            job = {
                "id": 102,
                "command": "hermes_prompt",
                "payload": {"prompt": "Какая сегодня погода?"},
                "attempt": 1,
            }
            res = client_agent._dispatch_agent_command(
                job,
                self.client,
                config=self.config,
                source_name="PC-TEST",
                archive_busy=False,
            )
            self.assertIsNone(res)
            mock_store.assert_called_once()
            call_args = mock_store.call_args[0]
            self.assertEqual(call_args[0], 102)
            self.assertTrue(call_args[1])
            self.assertIn("солнечная погода", call_args[2])

    @patch.object(client_agent, "store_command_result")
    @patch.object(client_agent, "_log_command")
    def test_hermes_prompt_command_empty(self, mock_log: MagicMock, mock_store: MagicMock) -> None:
        job = {
            "id": 103,
            "command": "hermes_prompt",
            "payload": {"prompt": ""},
            "attempt": 1,
        }
        client_agent._dispatch_agent_command(
            job,
            self.client,
            config=self.config,
            source_name="PC-TEST",
            archive_busy=False,
        )
        mock_store.assert_called_once()
        call_args = mock_store.call_args[0]
        self.assertEqual(call_args[0], 103)
        self.assertFalse(call_args[1])
        self.assertIn("Пустой", call_args[2])

    @patch.object(client_agent, "save_config")
    @patch.object(client_agent, "store_command_result")
    @patch.object(client_agent, "_log_command")
    def test_set_theme_command(self, mock_log: MagicMock, mock_store: MagicMock, mock_save: MagicMock) -> None:
        job = {
            "id": 104,
            "command": "set_theme",
            "payload": {"theme": "Tokyo"},
            "attempt": 1,
        }
        client_agent._dispatch_agent_command(
            job,
            self.client,
            config=self.config,
            source_name="PC-TEST",
            archive_busy=False,
        )
        mock_save.assert_called_once()
        self.assertEqual(self.config["theme"], "Tokyo")
        mock_store.assert_called_once()
        call_args = mock_store.call_args[0]
        self.assertEqual(call_args[0], 104)
        self.assertTrue(call_args[1])
        self.assertIn("Tokyo", call_args[2])


if __name__ == "__main__":
    unittest.main()
