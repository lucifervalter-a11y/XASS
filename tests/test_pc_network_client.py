from __future__ import annotations

import unittest
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pc_client import network_client


class PcNetworkClientTests(unittest.TestCase):
    def setUp(self) -> None:
        network_client._cache.clear()
        network_client._preferred_local_address = ""
        network_client._preferred_signature = ()
        network_client._preferred_origin = ("", "", 0)
        network_client._preferred_system_dns = False

    def test_secret_transport_rejects_remote_http_but_keeps_loopback_and_explicit_dev(self) -> None:
        with patch.dict(network_client.os.environ, {"XASS_ALLOW_INSECURE_HTTP": ""}):
            with self.assertRaisesRegex(RuntimeError, "HTTP"):
                network_client.require_secure_transport("http://example.invalid:8001")
            self.assertEqual(
                network_client.require_secure_transport("http://127.0.0.1:8001"),
                "http://127.0.0.1:8001",
            )
            self.assertEqual(
                network_client.require_secure_transport("http://example.invalid:8001", allow_insecure_http=True),
                "http://example.invalid:8001",
            )
            with self.assertRaises(RuntimeError):
                network_client.require_secure_transport("https://user:secret@example.invalid")

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
        transport = MagicMock()
        with patch.object(network_client, "resolve_https_ipv4", return_value=""), \
                patch.object(network_client, "_AdaptiveTransport", return_value=transport) as build, \
                patch.object(network_client.httpx, "Client") as client:
            network_client.create_http_client("https://xass.example", timeout=5)
        build.assert_called_once_with(
            "https://xass.example", resolved_address="", prefer_system_dns=False,
            local_address="", pinned=False,
        )
        self.assertIs(client.call_args.kwargs["transport"], transport)
        self.assertFalse(client.call_args.kwargs["trust_env"])

    def test_system_dns_mode_does_not_pin_a_doh_address(self) -> None:
        transport = MagicMock()
        with patch.object(network_client, "resolve_https_ipv4", side_effect=AssertionError("no DoH")), \
                patch.object(network_client, "_AdaptiveTransport", return_value=transport) as build, \
                patch.object(network_client.httpx, "Client") as client:
            network_client.create_http_client(
                "https://xass.example", timeout=5, prefer_system_dns=True,
            )
        build.assert_called_once_with(
            "https://xass.example", resolved_address="", prefer_system_dns=True,
            local_address="", pinned=True,
        )
        self.assertIs(client.call_args.kwargs["transport"], transport)
        self.assertFalse(client.call_args.kwargs["trust_env"])

    def test_network_invalidation_forgets_only_the_failed_host(self) -> None:
        network_client._cache.update({
            "xass.example": (1.0, "203.0.113.25"),
            "keep.example": (1.0, "203.0.113.26"),
        })
        network_client.invalidate_network_state("XASS.EXAMPLE.")
        self.assertNotIn("xass.example", network_client._cache)
        self.assertIn("keep.example", network_client._cache)

    def test_physical_candidates_exclude_vpn_and_public_addresses(self) -> None:
        addresses = {
            "Wi-Fi": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.111")],
            "Happ Tunnel": [SimpleNamespace(family=socket.AF_INET, address="10.99.0.2")],
            "vEthernet (WSL)": [SimpleNamespace(family=socket.AF_INET, address="172.20.0.1")],
            "Ethernet": [SimpleNamespace(family=socket.AF_INET, address="203.0.113.10")],
        }
        stats = {name: SimpleNamespace(isup=True) for name in addresses}
        with patch.object(network_client.psutil, "net_if_addrs", return_value=addresses), \
                patch.object(network_client.psutil, "net_if_stats", return_value=stats):
            self.assertEqual(network_client.physical_ipv4_candidates(), ("192.168.1.111",))

    def test_bound_client_keeps_tls_verification_and_original_hostname(self) -> None:
        transport = MagicMock()
        with patch.object(network_client, "resolve_https_ipv4", return_value="51.250.80.137"), \
                patch.object(network_client, "physical_ipv4_candidates", return_value=("192.168.1.111",)), \
                patch.object(network_client, "_AdaptiveTransport", return_value=transport) as build, \
                patch.object(network_client.httpx, "Client") as client:
            network_client.create_http_client(
                "https://redvps.site", timeout=5, local_address="192.168.1.111",
            )
        build.assert_called_once_with(
            "https://redvps.site", resolved_address="51.250.80.137",
            prefer_system_dns=False, local_address="192.168.1.111", pinned=True,
        )
        self.assertIs(client.call_args.kwargs["transport"], transport)
        self.assertNotIn("verify", client.call_args.kwargs)
        self.assertFalse(client.call_args.kwargs["follow_redirects"])

    def test_safe_get_retries_once_on_physical_https_without_changing_origin(self) -> None:
        request = network_client.httpx.Request("GET", "https://redvps.site/health")
        expected = network_client.httpx.Response(200, request=request)
        first, second = MagicMock(), MagicMock()
        first.handle_request.side_effect = network_client.httpx.ConnectError("TLS failed", request=request)
        second.handle_request.return_value = expected
        with patch.object(network_client, "network_signature", return_value=(("wi-fi", "192.168.1.111", True),)):
            transport = network_client._AdaptiveTransport(
                "https://redvps.site", resolved_address="51.250.80.137",
            )
        transport._transport.close()
        transport._transport = first
        with patch.object(transport, "_refresh_shared_preference"), \
                patch.object(transport, "_build", return_value=second), \
                patch.object(network_client, "activate_physical_fallback", return_value="192.168.1.111"), \
                patch.object(network_client, "preferred_connection", return_value=(True, "192.168.1.111")):
            response = transport.handle_request(request)
        self.assertIs(response, expected)
        self.assertEqual(transport.origin, ("https", "redvps.site", 443))
        self.assertTrue(transport.prefer_system_dns)
        self.assertEqual(transport.local_address, "192.168.1.111")
        first.handle_request.assert_called_once_with(request)
        second.handle_request.assert_called_once_with(request)

    def test_post_switches_future_pool_but_is_not_replayed(self) -> None:
        request = network_client.httpx.Request("POST", "https://redvps.site/agent/heartbeat")
        failure = network_client.httpx.ConnectError("TLS failed", request=request)
        first, second = MagicMock(), MagicMock()
        first.handle_request.side_effect = failure
        with patch.object(network_client, "network_signature", return_value=(("wi-fi", "192.168.1.111", True),)):
            transport = network_client._AdaptiveTransport("https://redvps.site")
        transport._transport.close()
        transport._transport = first
        with patch.object(transport, "_refresh_shared_preference"), \
                patch.object(transport, "_build", return_value=second), \
                patch.object(network_client, "activate_physical_fallback", return_value="192.168.1.111"):
            with self.assertRaises(network_client.httpx.ConnectError):
                transport.handle_request(request)
        first.handle_request.assert_called_once_with(request)
        second.handle_request.assert_not_called()

    def test_transport_never_falls_back_for_another_origin_or_http(self) -> None:
        request = network_client.httpx.Request("GET", "https://attacker.example/collect")
        failure = network_client.httpx.ConnectError("failed", request=request)
        first = MagicMock()
        first.handle_request.side_effect = failure
        with patch.object(network_client, "network_signature", return_value=()):
            transport = network_client._AdaptiveTransport("https://redvps.site")
        transport._transport.close()
        transport._transport = first
        with patch.object(transport, "_refresh_shared_preference"), \
                patch.object(network_client, "activate_physical_fallback") as fallback:
            with self.assertRaises(network_client.httpx.ConnectError):
                transport.handle_request(request)
        fallback.assert_not_called()
        self.assertEqual(network_client.activate_physical_fallback("http://redvps.site"), "")

    def test_network_signature_change_recreates_existing_pool(self) -> None:
        signature_a = (("wi-fi", "192.168.1.111", True),)
        signature_b = (("wi-fi", "192.168.1.112", True),)
        with patch.object(network_client, "network_signature", return_value=signature_a):
            transport = network_client._AdaptiveTransport("https://redvps.site", prefer_system_dns=True)
        first, second = MagicMock(), MagicMock()
        transport._transport.close()
        transport._transport = first
        with patch.object(network_client, "network_signature", return_value=signature_b), \
                patch.object(transport, "_build", return_value=second):
            transport._refresh_shared_preference()
        self.assertIs(transport._transport, second)
        first.close.assert_called_once()

    def test_arbitrary_or_vpn_bind_is_rejected_before_transport(self) -> None:
        with patch.object(network_client, "physical_ipv4_candidates", return_value=("192.168.1.111",)), \
                patch.object(network_client.httpx, "Client") as client:
            with self.assertRaises(ValueError):
                network_client.create_http_client(
                    "https://redvps.site", timeout=5, local_address="10.99.0.2",
                )
        client.assert_not_called()

    def test_activated_physical_fallback_is_shared_by_later_clients(self) -> None:
        signature = (("wi-fi", "192.168.1.111", True), ("happ tunnel", "10.99.0.2", True))
        with patch.object(network_client, "physical_ipv4_candidates", return_value=("192.168.1.111",)), \
                patch.object(network_client, "network_signature", return_value=signature), \
                patch.object(network_client, "resolve_https_ipv4", return_value="51.250.80.137"), \
                patch.object(network_client, "_probe_https_candidate", return_value=True):
            self.assertEqual(
                network_client.activate_physical_fallback("https://redvps.site"), "192.168.1.111",
            )
            self.assertEqual(
                network_client.preferred_connection("https://redvps.site"),
                (False, "192.168.1.111"),
            )
            self.assertEqual(network_client.preferred_connection("http://redvps.site"), (False, ""))

    def test_fallback_probes_every_physical_candidate_and_caches_only_the_success(self) -> None:
        signature = (("ethernet", "192.168.1.10", True), ("wi-fi", "192.168.1.20", True))
        with patch.object(network_client, "physical_ipv4_candidates",
                          return_value=("192.168.1.10", "192.168.1.20")), \
                patch.object(network_client, "network_signature", return_value=signature), \
                patch.object(network_client, "resolve_https_ipv4", return_value="51.250.80.137"), \
                patch.object(network_client, "_probe_https_candidate",
                             side_effect=[False, False, True]) as probe:
            selected = network_client.activate_physical_fallback("https://redvps.site/api")
        self.assertEqual(selected, "192.168.1.20")
        self.assertEqual([call.args[1] for call in probe.call_args_list],
                         ["192.168.1.10", "192.168.1.10", "192.168.1.20"])
        self.assertEqual(probe.call_args_list[0].kwargs["resolved_address"], "51.250.80.137")
        self.assertNotIn("resolved_address", probe.call_args_list[1].kwargs)
        self.assertEqual(probe.call_args_list[2].kwargs["resolved_address"], "51.250.80.137")
        self.assertEqual(network_client._preferred_local_address, "192.168.1.20")
        self.assertEqual(network_client._preferred_origin, ("https", "redvps.site", 443))
        self.assertFalse(network_client._preferred_system_dns)

    def test_failed_probes_clear_a_stale_shared_bind(self) -> None:
        network_client._preferred_local_address = "192.168.1.10"
        network_client._preferred_origin = ("https", "redvps.site", 443)
        with patch.object(network_client, "physical_ipv4_candidates",
                          return_value=("192.168.1.10", "192.168.1.20")), \
                patch.object(network_client, "_probe_https_candidate", return_value=False), \
                patch.object(network_client, "resolve_https_ipv4", return_value="51.250.80.137"), \
                patch.object(network_client, "network_signature", return_value=(("wi-fi", "192.168.1.20", True),)):
            self.assertEqual(network_client.activate_physical_fallback("https://redvps.site"), "")
        self.assertEqual(network_client._preferred_local_address, "")
        self.assertEqual(network_client._preferred_origin, ("", "", 0))
        self.assertEqual(network_client.preferred_connection("https://redvps.site"), (False, ""))

    def test_probe_uses_original_https_hostname_with_verified_tls_and_bound_source(self) -> None:
        response_context = MagicMock()
        response_context.__enter__.return_value.status_code = 404
        client = MagicMock()
        client.stream.return_value = response_context
        client_context = MagicMock()
        client_context.__enter__.return_value = client
        transport = MagicMock()
        with patch.object(network_client, "_ResolvedHTTPTransport", return_value=transport) as resolved, \
                patch.object(network_client.httpx, "Client", return_value=client_context) as build:
            self.assertTrue(network_client._probe_https_candidate(
                "https://redvps.site:8443/agent/heartbeat", "192.168.1.111",
                resolved_address="51.250.80.137",
            ))
        resolved.assert_called_once_with(
            {"redvps.site": "51.250.80.137"}, retries=0, local_address="192.168.1.111",
        )
        self.assertIs(build.call_args.kwargs["transport"], transport)
        self.assertNotIn("verify", build.call_args.kwargs)
        client.stream.assert_called_once_with(
            "HEAD", "https://redvps.site:8443/", headers={"Connection": "close"},
        )

    def test_system_dns_failure_keeps_doh_pinned_candidate_for_runtime(self) -> None:
        def probe(_url, _candidate, **kwargs):
            return bool(kwargs.get("resolved_address"))

        with patch.object(network_client, "physical_ipv4_candidates", return_value=("192.168.1.111",)), \
                patch.object(network_client, "resolve_https_ipv4", return_value="51.250.80.137"), \
                patch.object(network_client, "_probe_https_candidate", side_effect=probe), \
                patch.object(network_client, "network_signature", return_value=(("wi-fi", "192.168.1.111", True),)):
            selected = network_client.activate_physical_fallback("https://redvps.site")
            preference = network_client.preferred_connection("https://redvps.site")
        self.assertEqual(selected, "192.168.1.111")
        self.assertEqual(preference, (False, "192.168.1.111"))
        self.assertFalse(network_client._preferred_system_dns)

    def test_backend_replaces_only_configured_hostname(self) -> None:
        backend = network_client._HostOverrideBackend({"xass.example": "203.0.113.25"})
        backend._backend = MagicMock()
        backend.connect_tcp("xass.example", 443, timeout=3)
        backend._backend.connect_tcp.assert_called_once_with(
            "203.0.113.25", 443, timeout=3, local_address=None, socket_options=None
        )


if __name__ == "__main__":
    unittest.main()
