from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import httpx

CLIENT_ROOT = Path(__file__).resolve().parents[1] / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import hermes_client


class HermesClientTests(unittest.TestCase):
    def test_normalize_hermes_url(self) -> None:
        self.assertEqual(hermes_client.normalize_hermes_url(""), hermes_client.DEFAULT_HERMES_URL)
        self.assertEqual(hermes_client.normalize_hermes_url(None), hermes_client.DEFAULT_HERMES_URL)
        self.assertEqual(hermes_client.normalize_hermes_url("127.0.0.1:8787"), "http://127.0.0.1:8787")
        self.assertEqual(hermes_client.normalize_hermes_url("http://localhost:8787/"), "http://localhost:8787")
        self.assertEqual(hermes_client.normalize_hermes_url("https://hermes.example.com/api/"), "https://hermes.example.com")

    def test_get_hermes_webui_url(self) -> None:
        self.assertEqual(hermes_client.get_hermes_webui_url({}), hermes_client.DEFAULT_HERMES_URL)
        self.assertEqual(hermes_client.get_hermes_webui_url({"hermes_url": "http://192.168.1.50:8787"}), "http://192.168.1.50:8787")
        self.assertEqual(hermes_client.get_hermes_webui_url({"hermes_webui_url": "http://hermes.local"}), "http://hermes.local")

    def test_check_hermes_status_offline(self) -> None:
        with patch.object(httpx.Client, "get", side_effect=httpx.ConnectError("Connection refused")):
            status = hermes_client.check_hermes_status("http://127.0.0.1:8787")
            self.assertFalse(status["online"])
            self.assertEqual(status["status"], "offline")
            self.assertEqual(status["status_label"], "Не в сети")
            self.assertIn("127.0.0.1:8787", status["url"])

    def test_check_hermes_status_online(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok", "version": "1.0.0", "agent": "Hermes AI"}
        mock_resp.headers = {"content-type": "application/json"}

        with patch.object(httpx.Client, "get", return_value=mock_resp):
            status = hermes_client.check_hermes_status("http://127.0.0.1:8787")
            self.assertTrue(status["online"])
            self.assertEqual(status["status"], "online")
            self.assertEqual(status["status_label"], "В сети")
            self.assertEqual(status["details"]["agent"], "Hermes AI")

    def test_send_hermes_prompt_empty(self) -> None:
        result = hermes_client.send_hermes_prompt("")
        self.assertFalse(result["ok"])
        self.assertIn("пустым", result["error"])

    def test_send_hermes_prompt_offline(self) -> None:
        with patch.object(hermes_client, "check_hermes_status", return_value={"online": False, "error": "offline"}):
            result = hermes_client.send_hermes_prompt("Привет, агент!")
            self.assertFalse(result["ok"])
            self.assertFalse(result["online"])
            self.assertIn("недоступен", result["error"])

    def test_send_hermes_prompt_success_prompt_api(self) -> None:
        with patch.object(hermes_client, "check_hermes_status", return_value={"online": True}):
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"response": "Задача успешно выполнена Hermes."}

            with patch.object(httpx.Client, "post", return_value=mock_resp):
                result = hermes_client.send_hermes_prompt("Выполни задачу")
                self.assertTrue(result["ok"])
                self.assertEqual(result["response"], "Задача успешно выполнена Hermes.")
                self.assertEqual(result["prompt"], "Выполни задачу")

    def test_send_hermes_prompt_success_chat_completions(self) -> None:
        with patch.object(hermes_client, "check_hermes_status", return_value={"online": True}):
            mock_404 = MagicMock(is_success=False, status_code=404)
            mock_200 = MagicMock(
                is_success=True,
                status_code=200,
                json=lambda: {"choices": [{"message": {"content": "Ответ из chat/completions"}}]},
            )

            with patch.object(httpx.Client, "post", side_effect=[mock_404, mock_404, mock_200]):
                result = hermes_client.send_hermes_prompt("Привет")
                self.assertTrue(result["ok"])
                self.assertEqual(result["response"], "Ответ из chat/completions")


if __name__ == "__main__":
    unittest.main()
