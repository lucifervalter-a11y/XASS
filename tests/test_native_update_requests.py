from pathlib import Path
import json
import sys
import tempfile
import unittest
from pc_client.native_update_request import normalize_request, enqueue, pending, acknowledge, TTL

class NativeUpdateRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
    def test_all_known_channel_operations(self):
        for operation in ('check', 'install'):
            for channel in ('selected', 'native', 'native-test'):
                self.assertEqual(normalize_request(dict(operation=operation, channel=channel))['channel'], channel)
        self.assertEqual(normalize_request(dict(operation='rollback', channel='selected'))['operation'], 'rollback')
    def test_urls_unknown_channels_and_partial_payloads_rejected(self):
        for payload in ({}, {'operation': 'install'}, {'operation': 'execute', 'channel': 'native'},
                        {'operation': 'install', 'channel': 'https://evil.invalid'},
                        {'operation': 'rollback', 'channel': 'native-test'},
                        {'operation': 'install', 'channel': 'native', 'url': 'https://evil.invalid'},
                        {'operation': [], 'channel': 'native'}, None):
            with self.subTest(payload=payload), self.assertRaises(ValueError): normalize_request(payload)
    def test_queue_acknowledgment_and_replay_are_noninstalling(self):
        result = enqueue(self.data, 42, dict(operation='install', channel='native-test'), now=1000)
        self.assertTrue(result['requires_pc_confirmation'])
        self.assertEqual(pending(self.data, now=1001)[0]['id'], 42)
        acknowledge(self.data, 42)
        self.assertEqual(pending(self.data, now=1002), [])
        self.assertTrue(enqueue(self.data, 42, dict(operation='install', channel='native'), now=1003)['already_received'])
    def test_expired_or_future_requests_never_execute(self):
        enqueue(self.data, 1, dict(operation='check', channel='selected'), now=1000)
        enqueue(self.data, 2, dict(operation='check', channel='selected'), now=100000)
        self.assertEqual(pending(self.data, now=1001 + TTL), [])
    def test_queue_is_bounded_and_does_not_overwrite(self):
        for i in range(1, 17): enqueue(self.data, i, dict(operation='check', channel='selected'), now=1000)
        with self.assertRaises(ValueError): enqueue(self.data, 17, dict(operation='install', channel='native'), now=1001)
        self.assertEqual(len(pending(self.data, now=1002)), 16)
    def test_invalid_command_identity_rejected(self):
        for command in (True, 0, -1, '../escape', 2**63):
            with self.subTest(command=command), self.assertRaises(ValueError):
                enqueue(self.data, command, dict(operation='check', channel='native'))
    def test_malformed_request_cannot_become_authority(self):
        enqueue(self.data, 1, dict(operation='check', channel='selected'), now=1000)
        path = self.data / '.native-update-requests/1.json'; value = json.loads(path.read_text())
        value['url'] = 'https://evil.invalid'; path.write_text(json.dumps(value))
        self.assertEqual(pending(self.data, now=1001), [])
    def test_native_desktop_does_not_use_server_installer_manifest(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / 'windows/background_agent.py').read_text()
        section = service[service.index('def native_dispatch'):service.index('agent._dispatch_agent_command = native_dispatch')]
        self.assertIn('enqueue(data, command_id, payload)', section)
        self.assertNotIn('_apply_update(', section); self.assertNotIn('_apply_installer_update(', section)
        ui = (root / 'windows/Xass.Native/MainWindow.UpdateRequests.cs').read_text()
        self.assertLess(ui.index('DesktopConfirmAsync("Запрос сервера"'), ui.index('desktop_ack_update_request'))
        self.assertIn('await InstallNativeUpdateAsync()', ui)
        self.assertIn('NativeUpdatePreferences.Save(false)', ui)
    def test_packaging_includes_the_new_request_boundary(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'windows/packaging/native-payload.json').read_text())
        self.assertIn('native_update_request.py', manifest['pc_client_sources'])
        self.assertIn('native_update_request.py', (root / 'windows/Xass.Native/Xass.Native.csproj').read_text())
        self.assertIn("'native_update_request'", (root / 'windows/build_installer.ps1').read_text())
    def test_miniapp_has_channel_selector_and_payload_bound_proof(self):
        source = (Path(__file__).resolve().parents[1] / 'assets/miniapp-control-center.js').read_text()
        self.assertIn('ccNativeUpdateChannel', source)
        self.assertIn("{operation, channel}", source)
        self.assertIn("'native_update', 'reboot'", source)
        self.assertIn('X.agentActionBinding(source, command, payload)', source)
