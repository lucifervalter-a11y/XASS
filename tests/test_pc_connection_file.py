from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from app.services.qr_codes import connection_profile_svg
from pc_client.connection_file import parse_connection_text


class PcConnectionFileTests(unittest.TestCase):
    def test_existing_connection_payload_can_be_rendered_as_offline_qr(self) -> None:
        svg = connection_profile_svg(
            {"format": "xass-connect", "version": 1, "server_url": "https://xass.example", "pair_code": "ABCD-EFGH"}
        )
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('class="segno"', svg)

    def test_valid_profile_is_parsed(self) -> None:
        payload = {
            "format": "xass-connect",
            "version": 1,
            "server_url": "https://xass.example/anything",
            "pair_code": "XASS-2048",
            "source_name": "OFFICE-PC",
            "expires_at": "2030-01-01T00:00:00+00:00",
            "auto_update": True,
        }
        profile = parse_connection_text(
            json.dumps(payload),
            now=datetime(2029, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(profile.server_url, "https://xass.example")
        self.assertEqual(profile.source_name, "OFFICE-PC")

    def test_expired_profile_is_rejected(self) -> None:
        payload = {
            "format": "xass-connect",
            "version": 1,
            "server_url": "https://xass.example",
            "pair_code": "XASS-2048",
            "expires_at": "2028-01-01T00:00:00+00:00",
        }
        with self.assertRaisesRegex(ValueError, "истёк"):
            parse_connection_text(
                json.dumps(payload),
                now=datetime(2029, 1, 1, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
