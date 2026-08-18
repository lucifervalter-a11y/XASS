from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from pc_client import network_client


class PcNetworkClientTests(unittest.TestCase):
    def setUp(self) -> None:
        network_client._cache.clear()

    def test_https_resolver_accepts_only_ipv4_answers(self) -> None:
        response = MagicMock()
        response.json.return_value = {
            "Answer": [
                {"type": 5, "data": "alias.example"},
                {"type": 1, "data": "203.0.113.25"},
            ]
        }
        with patch.object(network_client.httpx, "get", return_value=response):
            self.assertEqual(network_client.resolve_https_ipv4("xass.example"), "203.0.113.25")
        response.raise_for_status.assert_called_once()

    def test_client_keeps_normal_dns_when_https_resolver_is_unavailable(self) -> None:
        with patch.object(network_client, "resolve_https_ipv4", return_value=""), patch.object(
            network_client.httpx, "Client"
        ) as client:
            network_client.create_http_client("https://xass.example", timeout=5)
        self.assertNotIn("transport", client.call_args.kwargs)

    def test_backend_replaces_only_configured_hostname(self) -> None:
        backend = network_client._HostOverrideBackend({"xass.example": "203.0.113.25"})
        backend._backend = MagicMock()
        backend.connect_tcp("xass.example", 443, timeout=3)
        backend._backend.connect_tcp.assert_called_once_with(
            "203.0.113.25", 443, timeout=3, local_address=None, socket_options=None
        )


if __name__ == "__main__":
    unittest.main()
