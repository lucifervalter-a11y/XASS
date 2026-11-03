"""Fail locally if the proposed server/client/CI versions drift apart."""
import ast
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ReleaseCandidateIdentityTests(unittest.TestCase):
    def test_server_client_and_validation_expect_same_version(self):
        tree = ast.parse((ROOT / 'app/main.py').read_text(encoding='utf-8'))
        versions = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'APP_VERSION' for t in node.targets)]
        version = json.loads((ROOT / 'pc_client/version.json').read_text())['version']
        self.assertEqual(versions, [version])
        workflow = (ROOT / '.github/workflows/windows-channel-release.yml').read_text()
        self.assertIn("default: '" + version + "'", workflow)
        self.assertIn("EXPECTED_VERSION: ${{ inputs.expected_version || '" + version + "' }}", workflow)

    def test_candidate_lane_has_no_release_or_deployment_rights(self):
        workflow = (ROOT / '.github/workflows/windows-channel-release.yml').read_text()
        self.assertIn('permissions:\n  contents: read', workflow)
        self.assertNotIn('contents: write', workflow)
        self.assertNotIn('secrets.', workflow)
        self.assertNotIn('gh release create', workflow)
        self.assertIn('codex/xass-release-0.21.5', workflow)

if __name__ == '__main__':
    unittest.main()
