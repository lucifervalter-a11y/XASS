from __future__ import annotations

import unittest

from app.config import Settings
from app.services.web_push import normalize_subscription, push_configured


class WebPushTests(unittest.TestCase):
    def test_subscription_requires_https_and_browser_keys(self) -> None:
        with self.assertRaises(ValueError):
            normalize_subscription({"endpoint": "http://push.example/test", "keys": {"p256dh": "a", "auth": "b"}})
        with self.assertRaises(ValueError):
            normalize_subscription({"endpoint": "https://push.example/test", "keys": {}})

        clean = normalize_subscription(
            {
                "endpoint": "https://push.example/subscription/1",
                "expirationTime": None,
                "keys": {"p256dh": "public-browser-key", "auth": "auth-secret"},
                "ignored": "value",
            }
        )
        self.assertEqual(clean["endpoint"], "https://push.example/subscription/1")
        self.assertNotIn("ignored", clean)

    def test_push_is_only_advertised_with_complete_vapid_config(self) -> None:
        settings = Settings(_env_file=None)
        self.assertFalse(push_configured(settings))
        settings.pwa_vapid_public_key = "public"
        settings.pwa_vapid_private_key = "private"
        settings.pwa_vapid_subject = "mailto:owner@example.com"
        self.assertTrue(push_configured(settings))


if __name__ == "__main__":
    unittest.main()
