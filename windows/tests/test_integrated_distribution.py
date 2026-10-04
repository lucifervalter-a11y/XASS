"""Cross-component package contracts, not a substitute for Windows build/acceptance."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / 'windows' / 'Xass.Native'


class IntegratedDistributionTests(unittest.TestCase):
    def test_all_allowlisted_companion_sources_exist(self):
        manifest = json.loads((ROOT / 'windows/packaging/native-payload.json').read_text())
        for filename in manifest['pc_client_sources']:
            with self.subTest(filename=filename):
                self.assertTrue((ROOT / 'pc_client' / filename).is_file())
                ast.parse((ROOT / 'pc_client' / filename).read_text(encoding='utf-8-sig'))

    def test_all_python_publish_companions_are_declared_and_present(self):
        manifest = json.loads((ROOT / 'windows/packaging/native-payload.json').read_text())
        project = ET.parse(NATIVE / 'Xass.Native.csproj')
        includes = {e.attrib.get('Link'): e.attrib['Include'] for e in project.iter('Content')}
        for name in manifest['required_native_files']:
            if name.endswith('.py'):
                with self.subTest(filename=name):
                    self.assertIn(name, includes)
                    self.assertTrue((NATIVE / includes[name].replace('\\', '/')).is_file())

    def test_every_xaml_event_has_a_partial_class_handler(self):
        source = '\n'.join(path.read_text() for path in NATIVE.glob('MainWindow*.cs'))
        events = {'Click', 'SelectionChanged', 'Toggled', 'KeyDown', 'TextChanged', 'ValueChanged',
                  'Loaded', 'Unloaded', 'ItemClick', 'DoubleTapped', 'DragOver', 'Drop'}
        for element in ET.parse(NATIVE / 'MainWindow.xaml').iter():
            for attribute, value in element.attrib.items():
                if attribute in events:
                    with self.subTest(handler=value):
                        self.assertRegex(source, rf'\b{re.escape(value)}\s*\(')

    def test_theme_and_voice_lifetimes_are_both_wired(self):
        source = '\n'.join(path.read_text() for path in NATIVE.glob('MainWindow*.cs'))
        for call in ('InitializeVoice();', 'InitializeAppearance();', 'DisposeVoice();', 'DisposeAppearance();'):
            with self.subTest(call=call):
                self.assertIn(call, source)

    def test_packaging_is_read_only_and_test_branch_only(self):
        workflow = (ROOT / '.github/workflows/windows-test-release.yml').read_text()
        self.assertIn('branches: [test]', workflow)
        self.assertIn('contents: read', workflow)
        self.assertNotIn('contents: write', workflow)
        self.assertNotIn('gh release', workflow)
        self.assertNotIn('secrets.', workflow)
        for token in ('VoiceLifecycle.Tests', 'build_installer.ps1', 'test_installer.ps1',
                      'requirements.txt', 'both must pass for release'):
            with self.subTest(token=token):
                self.assertIn(token, workflow)


if __name__ == '__main__':
    unittest.main()
