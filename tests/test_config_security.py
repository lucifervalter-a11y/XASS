import sys
import unittest
from unittest.mock import MagicMock

if "pydantic_settings" not in sys.modules:
    mock_ps = MagicMock()
    class MockBase:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    mock_ps.BaseSettings = MockBase
    mock_ps.NoDecode = lambda x: x
    mock_ps.SettingsConfigDict = dict
    sys.modules["pydantic_settings"] = mock_ps

from app.config import Settings


class ConfigSecurityTests(unittest.TestCase):
    def test_validate_security_detects_default_keys(self):
        s = Settings(
            setup_api_key="change-me-setup-key",
            agent_api_key="change-me-agent-key",
            agent_api_key_enabled=True,
            telegram_webhook_path="change-me-webhook-path",
        )
        warnings = s.validate_security()
        self.assertEqual(len(warnings), 3)
        self.assertTrue(any("SETUP_API_KEY" in w for w in warnings))
        self.assertTrue(any("AGENT_API_KEY" in w for w in warnings))
        self.assertTrue(any("TELEGRAM_WEBHOOK_PATH" in w for w in warnings))

    def test_validate_security_clean_when_keys_configured(self):
        s = Settings(
            setup_api_key="prod-secure-setup-key-999",
            agent_api_key="prod-secure-agent-key-888",
            agent_api_key_enabled=True,
            telegram_webhook_path="/custom-secret-hook-777",
        )
        warnings = s.validate_security()
        self.assertEqual(len(warnings), 0)

    def test_validate_security_agent_key_ignored_if_disabled(self):
        s = Settings(
            setup_api_key="prod-secure-setup-key-999",
            agent_api_key="change-me-agent-key",
            agent_api_key_enabled=False,
            telegram_webhook_path="/custom-secret-hook-777",
        )
        warnings = s.validate_security()
        self.assertEqual(len(warnings), 0)


if __name__ == "__main__":
    unittest.main()
