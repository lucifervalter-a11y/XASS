from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from app.services.vk_bind import issue_vk_bind_token, verify_vk_bind_token


def _settings() -> SimpleNamespace:
    return SimpleNamespace(bot_token="bot-token-for-tests", setup_api_key="setup-key-for-tests")


class VkBindTokenTests(unittest.TestCase):
    def test_roundtrip_and_expiry(self) -> None:
        settings = _settings()
        now = int(time.time())
        token = issue_vk_bind_token(settings, chat_id=42, ttl_seconds=120, now=now)
        payload = verify_vk_bind_token(token, settings, now=now)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["purpose"], "vk_bind")
        self.assertEqual(payload["chat_id"], 42)
        self.assertIsNone(verify_vk_bind_token(token, settings, now=now + 121))
        self.assertIsNone(verify_vk_bind_token("tampered." + token.split(".", 1)[1], settings, now=now))
        self.assertIsNone(verify_vk_bind_token("not-a-token", settings, now=now))


if __name__ == "__main__":
    unittest.main()
