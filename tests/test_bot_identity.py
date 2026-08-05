import tempfile
import unittest
from pathlib import Path

from app.services.bot_identity import load_cached_bot_username, normalize_bot_username, save_cached_bot_username


class BotIdentityTests(unittest.TestCase):
    def test_normalizes_and_rejects_invalid_usernames(self) -> None:
        self.assertEqual(normalize_bot_username(" @XASS_AdminBot "), "xass_adminbot")
        self.assertEqual(normalize_bot_username("bad domain"), "")
        self.assertEqual(normalize_bot_username("бот_name"), "")
        self.assertEqual(normalize_bot_username(""), "")

    def test_round_trip_cache(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "identity.json"
            self.assertEqual(save_cached_bot_username(path, "@XASS_AdminBot"), "xass_adminbot")
            self.assertEqual(load_cached_bot_username(path), "xass_adminbot")

    def test_invalid_cache_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "identity.json"
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_cached_bot_username(path), "")


if __name__ == "__main__":
    unittest.main()
