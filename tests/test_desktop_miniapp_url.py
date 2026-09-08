from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc_client"))
try:
    from desktop_app import miniapp_entry_url
finally:
    sys.path.pop(0)


class DesktopMiniAppUrlTests(unittest.TestCase):
    def test_canonical_public_url_overrides_legacy_backend_host_and_port(self) -> None:
        self.assertEqual(
            miniapp_entry_url("http://redvps.site:8000", {
                "web_app_url": "https://xass.example/miniapp.php?standalone=1", "domain": "redvps.site",
                "requirements": {"https": False},
            }),
            "https://xass.example/miniapp.php?standalone=1",
        )

    def test_remote_path_query_fragment_and_agent_credentials_never_enter_browser(self) -> None:
        result = miniapp_entry_url("http://user:agent-key@old.example:8000/agent?key=agent-secret", {
            "web_app_url": "https://xass.example/private-route?api_key=private-key#pair=private-token",
        })
        self.assertEqual(result, "https://xass.example/miniapp.php?standalone=1")
        self.assertNotIn("private", result)
        self.assertNotIn("secret", result)

    def test_explicit_public_port_and_ipv6_are_preserved(self) -> None:
        for origin in ("https://xass.example:8443", "https://[2001:db8::1]:8443", "http://localhost:8080"):
            with self.subTest(origin=origin):
                self.assertEqual(miniapp_entry_url("http://old.example:8000", {"web_app_url": origin + "/profile"}),
                                 origin + "/miniapp.php?standalone=1")

    def test_old_api_domain_fallback_does_not_reuse_agent_port(self) -> None:
        result = miniapp_entry_url("http://redvps.site:8000", {"domain": "redvps.site", "requirements": {"https": True}})
        self.assertEqual(result, "https://redvps.site/miniapp.php?standalone=1")

    def test_old_api_preserves_declared_public_port_and_local_http(self) -> None:
        self.assertEqual(miniapp_entry_url("http://127.0.0.1:8000", {"domain": "localhost:8080", "requirements": {"https": False}}),
                         "http://localhost:8080/miniapp.php?standalone=1")
        self.assertEqual(miniapp_entry_url("https://old.example:8000", {"domain": "xass.example"}),
                         "https://xass.example/miniapp.php?standalone=1")

    def test_rejects_active_schemes_relative_urls_and_missing_hosts(self) -> None:
        for raw in ("javascript:alert(1)", "file:///C:/Windows/notepad.exe", "data:text/html,test", "//xass.example", "/miniapp.php", "https:///miniapp.php"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                miniapp_entry_url("http://old.example:8000", {"web_app_url": raw})

    def test_rejects_all_userinfo_including_empty_username(self) -> None:
        for raw in ("https://user:password@xass.example", "https://user@xass.example", "https://@xass.example", "https://:password@xass.example"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                miniapp_entry_url("http://old.example:8000", {"web_app_url": raw})

    def test_rejects_invalid_ports_in_canonical_and_legacy_config(self) -> None:
        for authority in ("xass.example:bad", "xass.example:999999", "xass.example:-1", "xass.example:0", "xass.example:", "[::1]:bad"):
            with self.subTest(authority=authority):
                for config in ({"web_app_url": "https://" + authority}, {"domain": authority, "requirements": {"https": True}}):
                    with self.assertRaises(ValueError):
                        miniapp_entry_url("http://old.example:8000", config)

    def test_rejects_controls_backslashes_and_ambiguous_authorities(self) -> None:
        for raw in ("https://xass.example\n/next", "https://xass.example\t.evil.test", "https://xass.example\x00.evil.test",
                    "https://xass.example\\@evil.test", "https://xass .example", "https://[invalid-ipv6]/"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                miniapp_entry_url("http://old.example:8000", {"web_app_url": raw})

    def test_malformed_config_fails_cleanly_without_opening_a_window(self) -> None:
        for config in (None, [], {}, {"domain": "xass.example/path"}, {"domain": "user@xass.example"},
                       {"domain": "xass.example", "requirements": "invalid"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                miniapp_entry_url("http://old.example:8000", config)


if __name__ == "__main__":
    unittest.main()
