from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.services.agent_connection import build_connection_profile, normalize_server_origin


class AgentConnectionTests(unittest.TestCase):
    def test_profile_contains_only_short_lived_bootstrap_data(self) -> None:
        expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
        profile = build_connection_profile(
            server_url="https://xass.example/profile.php",
            pair_code="XASS-2048",
            expires_at=expiry,
            source_name="OFFICE-PC",
        )
        self.assertEqual(profile["server_url"], "https://xass.example")
        self.assertEqual(profile["format"], "xass-connect")
        self.assertEqual(profile["pair_code"], "XASS-2048")
        self.assertNotIn("api_key", profile)

    def test_server_origin_rejects_credentials(self) -> None:
        self.assertIsNone(normalize_server_origin("https://user:secret@xass.example"))


if __name__ == "__main__":
    unittest.main()
