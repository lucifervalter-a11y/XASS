from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PhpAgentProxyTests(unittest.TestCase):
    def test_front_controller_routes_agent_json_and_binary_paths(self) -> None:
        index = (ROOT / "index.php").read_text(encoding="utf-8")
        self.assertIn("str_starts_with($requestPath, '/agent/')", index)
        self.assertIn("$requestPath === '/health'", index)
        self.assertIn("'/agent/update/package'", index)
        self.assertIn("'/agent/installer/'", index)
        self.assertIn("require __DIR__ . '/proxy.php'", index)

    def test_proxy_allows_only_api_and_agent_namespaces(self) -> None:
        proxy = (ROOT / "proxy.php").read_text(encoding="utf-8")
        self.assertIn("strpos($pathOnly, '/api/')", proxy)
        self.assertIn("strpos($pathOnly, '/agent/')", proxy)
        self.assertIn("$pathOnly !== '/health'", proxy)
        self.assertIn("strpos($rawPath, '..')", proxy)
        self.assertIn("proxy_error(400, 'invalid proxy path')", proxy)


if __name__ == "__main__":
    unittest.main()
