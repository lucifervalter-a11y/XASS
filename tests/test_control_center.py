from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CLIENT_ROOT = ROOT / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import client_agent
from app.main import _agent_attention
from app.services.agent_commands import ALLOWED_AGENT_COMMANDS, DANGEROUS_AGENT_COMMANDS
from app.services.app_config import prepare_audit_payload, sanitize_audit_payload


class ControlCenterTests(unittest.TestCase):
    def test_extended_device_commands_are_available_and_power_is_protected(self) -> None:
        self.assertTrue(
            {"ping", "check_update", "update", "restart", "sleep", "reboot", "shutdown", "lock", "open_archive", "cleanup_archive"}
            <= ALLOWED_AGENT_COMMANDS
        )
        self.assertTrue({"update", "restart", "sleep", "reboot", "shutdown", "lock"} <= DANGEROUS_AGENT_COMMANDS)
        self.assertNotIn("ping", DANGEROUS_AGENT_COMMANDS)

    def test_agent_attention_explains_every_reason(self) -> None:
        reasons, needs_update = _agent_attention(
            {
                "agent_version": "0.9.0",
                "metrics": {"cpu_percent": 98, "ram_used_percent": 96, "disk_used_percent": 94},
                "last_error": "network",
                "archive_status": {"last_error": "disk"},
            },
            is_online=False,
            latest_version="0.10.0",
        )
        self.assertTrue(needs_update)
        self.assertEqual(
            set(reasons),
            {"offline", "high_cpu", "high_ram", "low_disk", "agent_error", "archive_error", "update_available"},
        )

    def test_audit_payload_redacts_credentials_and_is_bounded(self) -> None:
        cleaned = prepare_audit_payload(
            {
                "source_name": "home-pc",
                "bot_token": "secret",
                "nested": {"private_key": "secret", "message": "x" * 3000},
            }
        )
        self.assertEqual(cleaned["bot_token"], "[redacted]")
        self.assertEqual(cleaned["nested"]["private_key"], "[redacted]")
        self.assertEqual(len(cleaned["nested"]["message"]), 2000)
        self.assertEqual(cleaned["result"], "success")
        self.assertEqual(cleaned["source"], "server")
        self.assertEqual(len(cleaned["operation_id"]), 32)
        self.assertEqual(sanitize_audit_payload({"X-Api-Key": "secret"})["X-Api-Key"], "[redacted]")

    def test_power_command_clamps_delay_and_reports_result(self) -> None:
        with (
            patch.object(client_agent.os, "name", "nt"),
            patch.object(client_agent.subprocess, "Popen") as popen,
            patch.object(client_agent, "store_command_result") as store,
        ):
            client_agent._power_command(9, reboot=True, delay_sec=9999)
        popen.assert_called_once_with(
            ["shutdown.exe", "/r", "/t", "3600", "/d", "p:0:0"],
            creationflags=getattr(client_agent.subprocess, "CREATE_NO_WINDOW", 0),
        )
        store.assert_called_once_with(9, True, "Windows будет перезагружена через 3600 сек.")

    def test_sleep_failure_is_not_recorded_as_success(self) -> None:
        class FakeSuspend:
            argtypes = None
            restype = None

            def __call__(self, *_args: object) -> bool:
                return False

        with (
            patch.object(client_agent.os, "name", "nt"),
            patch.object(client_agent.ctypes, "WinDLL", return_value=SimpleNamespace(SetSuspendState=FakeSuspend())),
            patch.object(client_agent.ctypes, "get_last_error", return_value=5),
            patch.object(client_agent, "store_command_result") as store,
        ):
            client_agent._sleep_workstation(10)
        store.assert_called_once()
        self.assertFalse(store.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
