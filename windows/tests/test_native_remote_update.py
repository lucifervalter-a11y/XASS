from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pc_client'))
from background_agent import configure_native_update_commands
from native_update_request import pending

class RemoteUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); self.data = Path(temp.name)
        self.original = Mock(return_value='forwarded')
        self.agent = SimpleNamespace(_dispatch_agent_command=self.original, build_payload=Mock(return_value={'agent_distribution': 'installer'}), store_command_result=Mock())
        configure_native_update_commands(self.agent, self.data)
    def dispatch(self, command, payload=None, **extra):
        return self.agent._dispatch_agent_command({'id': 42, 'command': command, 'payload': payload or {}, **extra}, None, config={}, source_name='PC', archive_busy=False)
    def test_native_install_is_only_a_review_intent(self):
        self.assertIsNone(self.dispatch('native_update', {'operation': 'install', 'channel': 'native-test'}))
        self.original.assert_not_called()
        self.assertEqual(pending(self.data)[0]['channel'], 'native-test')
        self.assertTrue(self.agent.store_command_result.call_args.args[3]['requires_pc_confirmation'])
    def test_legacy_update_uses_selected_channel_not_server_exe(self):
        self.assertIsNone(self.dispatch('update', {'url': 'https://evil.invalid/setup.exe'}))
        self.original.assert_not_called()
        self.assertEqual(pending(self.data)[0]['channel'], 'selected')
    def test_legacy_check_is_not_false_already_up_to_date(self):
        self.dispatch('check_update'); self.assertEqual(pending(self.data)[0]['operation'], 'check')
        self.assertIn('ещё не выполнена', self.agent.store_command_result.call_args.args[2])
    def test_other_commands_still_use_existing_dispatch(self):
        self.assertEqual(self.dispatch('ping'), 'forwarded'); self.original.assert_called_once()
    def test_bad_remote_payload_is_not_forwarded_or_queued(self):
        self.dispatch('native_update', {'operation': 'install', 'channel': 'evil'})
        self.original.assert_not_called(); self.assertEqual(pending(self.data), [])
        self.assertFalse(self.agent.store_command_result.call_args.args[1])
    def test_heartbeat_advertises_confirmation_and_only_known_channels(self):
        result = self.agent.build_payload({})
        self.assertEqual(result['native_update_capabilities']['channels'], ['native', 'native-test'])
        self.assertTrue(result['native_update_capabilities']['requires_pc_confirmation'])
