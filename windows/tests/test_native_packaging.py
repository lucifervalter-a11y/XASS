"""Cross-platform installer/source contracts; no Windows build is implied."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package = load('native_package', ROOT / 'windows/stage_native_package.py')
host = load('native_host', ROOT / 'windows/native_host.py')


class PackagingTests(unittest.TestCase):
    def test_frozen_dispatch_is_allowlisted_and_does_not_accept_script_paths(self):
        self.assertEqual(set(host.ROLES), {'assistant', 'listener', 'agent-bridge', 'background-agent', 'desktop-music'})
        self.assertEqual(host.ROLES['listener'], 'background_voice_bridge')
        self.assertEqual(host.ROLES['background-agent'], 'background_agent')
        for args in (['--role', 'unknown'], ['--role', 'assistant', 'arbitrary.py'], ['--health-check', 'extra']):
            with self.subTest(args=args), self.assertRaises(SystemExit), patch('sys.stderr'):
                host.main(args)

    def test_health_check_never_imports_application_roles(self):
        with patch.object(host.importlib, 'import_module') as imported, patch.object(host.importlib.util, 'find_spec', return_value=object()):
            result = host.health_check()
        self.assertTrue(result['ok'])
        self.assertFalse(result['whisper_model_bundled'])
        self.assertEqual([call.args[0] for call in imported.call_args_list], list(host.HEALTH_IMPORTS))

    def test_payload_rejects_personal_configs_keys_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('config.json', '.env', '.env.local', 'secret.key', 'backup.db', 'appearance.json', 'config.json.bak', 'runtime.json', 'local-music.json'):
                path = root / name
                path.write_text('private')
                with self.subTest(name=name), self.assertRaises(ValueError):
                    package.safe_files(root)
                path.unlink()
            (root / 'safe.dll').write_bytes(b'safe')
            self.assertEqual(len(package.safe_files(root)), 1)
            try:
                (root / 'link.dll').symlink_to(root / 'safe.dll')
            except OSError:
                return
            with self.assertRaises(ValueError):
                package.safe_files(root)

    def test_only_certifi_public_ca_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cert = root / '_internal/certifi/cacert.pem'
            cert.parent.mkdir(parents=True)
            cert.write_text('public trust store')
            self.assertEqual(package.safe_files(root), [cert])
            (root / 'secret.pem').write_text('private')
            with self.assertRaises(ValueError):
                package.safe_files(root)

    def test_pe_header_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'program.exe'
            image = bytearray(256)
            image[:2] = b'MZ'
            struct.pack_into('<I', image, 0x3c, 128)
            image[128:132] = b'PE\0\0'
            struct.pack_into('<H', image, 132, 0x8664)
            path.write_bytes(image)
            package.require_x64_pe(path)
            struct.pack_into('<H', image, 132, 0x14c)
            path.write_bytes(image)
            with self.assertRaises(ValueError):
                package.require_x64_pe(path)

    def test_stage_copies_all_runtime_files_and_records_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            native, companion, source, licenses = (root / name for name in ('native', 'companion', 'source', 'licenses'))
            for directory in (native, companion / '_internal', source / 'pc_client', licenses):
                directory.mkdir(parents=True)
            image = bytearray(256)
            image[:2] = b'MZ'
            struct.pack_into('<I', image, 0x3c, 128)
            image[128:132] = b'PE\0\0'
            struct.pack_into('<H', image, 132, 0x8664)
            (native / 'Xass.Native.exe').write_bytes(image)
            (native / 'Xass.Native.deps.json').write_text('{}')
            (native / 'Xass.Native.runtimeconfig.json').write_text('{}')
            (native / 'extra-required.dll').write_bytes(b'keep complete native folder')
            (companion / 'XASS.NativeHelper.exe').write_bytes(image)
            (companion / '_internal/python312.dll').write_bytes(b'python fixture')
            (companion / '_internal/build-info.json').write_text(json.dumps({'revision': 'a' * 40, 'distribution': 'native-test'}))
            for name in ('module.py', 'requirements.txt', 'voice-requirements.txt', 'version.json'):
                (source / 'pc_client' / name).write_text('fixture')
            (licenses / 'python-LICENSE.txt').write_text('license fixture')
            (licenses / 'python-dependencies.json').write_text('[]')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'pc_client_sources': ['module.py'],
                'required_native_files': ['Xass.Native.exe'],
                'required_companion_files': ['XASS.NativeHelper.exe', '_internal/python312.dll']}))
            destination = root / 'payload'
            with patch.object(package, 'ROOT', source), patch.object(package, 'MANIFEST', manifest):
                result = package.stage(native, companion, destination, 'a' * 40, licenses)
                with self.assertRaises(ValueError):
                    package.stage(native, companion, destination, 'a' * 40, licenses)
            indexed = {item['path']: item for item in result['files']}
            self.assertIn('extra-required.dll', indexed)
            self.assertIn('runtime/_internal/python312.dll', indexed)
            self.assertIn('pc_client/module.py', indexed)
            self.assertTrue(result['python_bundled'])
            self.assertFalse(result['whisper_model_bundled'])
            self.assertTrue((destination / 'payload-manifest.json').is_file())
            self.assertEqual(json.loads((destination / 'build-info.json').read_text())['revision'], 'a' * 40)
            self.assertEqual((destination / 'build-info.json').read_bytes(), (destination / 'pc_client/build-info.json').read_bytes())

    def test_manifest_allowlists_source_and_complete_runtime(self):
        manifest = json.loads(package.MANIFEST.read_text())
        sources = manifest['pc_client_sources']
        self.assertEqual(len(sources), len(set(sources)))
        self.assertTrue(all(name.endswith('.py') and '/' not in name for name in sources))
        self.assertTrue({'secret_store.py', 'network_client.py', 'music_bridge.py', 'client_update.py', 'runtime_state.py'} <= set(sources))
        self.assertIn('_internal/python312.dll', manifest['required_companion_files'])
        self.assertTrue({'coreclr.dll', 'Microsoft.ui.xaml.dll', 'Xass.Native.pri'} <= set(manifest['required_native_files']))

    def test_installer_is_separate_per_user_and_preserves_data(self):
        installer = (ROOT / 'windows/packaging/XASS-Native.iss').read_text()
        for contract in ('PrivilegesRequired=lowest', 'UsePreviousAppDir=yes', 'CloseApplications=yes',
                         'DefaultDirName={localappdata}\\Programs\\XASS-Native-Test', 'recursesubdirs', 'skipifsilent'):
            self.assertIn(contract, installer)
        deletes = installer.split('[InstallDelete]', 1)[1].split('[Icons]', 1)[0]
        self.assertNotIn('Name: "{localappdata}', deletes)
        self.assertNotIn('Name: "{app}\\*', deletes)
        self.assertNotIn('[UninstallDelete]', installer)
        self.assertNotIn('[Registry]', installer)
        self.assertNotIn('B9284FD3-A46F-4DB8-9F1C-FA9144A290B8', installer)

    def test_build_verifies_frozen_dependencies_and_never_downloads_models(self):
        build = (ROOT / 'windows/build_installer.ps1').read_text()
        for contract in ('--onedir', '--console', '--health-check', 'native-test', 'voice-requirements.txt', 'stage_native_package.py'):
            self.assertIn(contract, build)
        self.assertNotIn('Invoke-WebRequest', build)
        self.assertNotIn('winget install', build)
        self.assertNotIn('snapshot_download', build)


if __name__ == '__main__':
    unittest.main()
