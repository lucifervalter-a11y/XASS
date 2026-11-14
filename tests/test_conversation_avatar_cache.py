import os
from pathlib import Path
import tempfile
import time
import unittest

from app.services.conversation_avatar_cache import (
    load_cached_avatar,
    mark_avatar_missing,
    missing_cache_is_fresh,
    store_cached_avatar,
)


class ConversationAvatarCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache_dir = Path(temporary.name)

    def test_avatar_cache_round_trip_and_negative_marker(self):
        store_cached_avatar(self.cache_dir, 42, b"jpeg-data", "image/jpeg")

        self.assertEqual(
            load_cached_avatar(self.cache_dir, 42, ttl_seconds=60),
            (b"jpeg-data", "image/jpeg"),
        )
        self.assertFalse(missing_cache_is_fresh(self.cache_dir, 42, ttl_seconds=60))

        mark_avatar_missing(self.cache_dir, 42)
        self.assertTrue(missing_cache_is_fresh(self.cache_dir, 42, ttl_seconds=60))

    def test_avatar_cache_ignores_expired_or_invalid_entries(self):
        store_cached_avatar(self.cache_dir, 7, b"image", "image/png")
        expired = time.time() - 120
        os.utime(self.cache_dir / "7.bin", (expired, expired))
        os.utime(self.cache_dir / "7.json", (expired, expired))

        self.assertIsNone(load_cached_avatar(self.cache_dir, 7, ttl_seconds=60))

        (self.cache_dir / "8.bin").write_bytes(b"not-an-image")
        (self.cache_dir / "8.json").write_text('{"media_type":"text/plain"}', encoding="utf-8")
        self.assertIsNone(load_cached_avatar(self.cache_dir, 8, ttl_seconds=60))


if __name__ == "__main__":
    unittest.main()
