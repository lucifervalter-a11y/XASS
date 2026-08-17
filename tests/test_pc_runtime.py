from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pc_client.runtime_state as runtime_state


class PcRuntimeTests(unittest.TestCase):
    def test_atomic_config_restores_last_valid_backup(self) -> None:
        import sys

        client_root = Path(__file__).resolve().parents[1] / "pc_client"
        if str(client_root) not in sys.path:
            sys.path.insert(0, str(client_root))
        import client_agent

        with tempfile.TemporaryDirectory() as raw_root:
            config = Path(raw_root) / "config.json"
            with patch.object(client_agent, "CONFIG_PATH", config):
                client_agent.save_config({"server_url": "http://first", "api_key": "secret-1"})
                client_agent.save_config({"server_url": "http://second", "api_key": "secret-2"})
                config.write_text("{broken", encoding="utf-8")
                restored = client_agent.load_config()
            self.assertEqual(restored["server_url"], "http://first")
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["server_url"], "http://first")

    def test_processed_command_ids_survive_restart(self) -> None:
        import sys

        client_root = Path(__file__).resolve().parents[1] / "pc_client"
        if str(client_root) not in sys.path:
            sys.path.insert(0, str(client_root))
        import client_agent

        with tempfile.TemporaryDirectory() as raw_root:
            state = Path(raw_root) / "processed.json"
            with patch.object(client_agent, "PROCESSED_COMMANDS_PATH", state):
                self.assertFalse(client_agent.command_was_processed(42))
                client_agent.mark_command_processed(42, "lock")
                self.assertTrue(client_agent.command_was_processed(42))

    def test_single_instance_guard_rejects_second_copy(self) -> None:
        name = f"XASS-test-{uuid4().hex}"
        first = runtime_state.acquire_single_instance(name)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(runtime_state.acquire_single_instance(name))
        finally:
            assert first is not None
            first.close()
        third = runtime_state.acquire_single_instance(name)
        self.assertIsNotNone(third)
        assert third is not None
        third.close()

    def test_persisted_log_is_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            log = root / "xass.log"
            with patch.object(runtime_state, "LOG_ROOT", root), patch.object(runtime_state, "APP_LOG_PATH", log):
                runtime_state.append_log("Агент подключён — всё работает")
                self.assertEqual(runtime_state.read_log_tail(), ["Агент подключён — всё работает"])
            self.assertIn("подключён".encode("utf-8"), log.read_bytes())


if __name__ == "__main__":
    unittest.main()
