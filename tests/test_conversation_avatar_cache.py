import os
import time

from app.services.conversation_avatar_cache import (
    load_cached_avatar,
    mark_avatar_missing,
    missing_cache_is_fresh,
    store_cached_avatar,
)


def test_avatar_cache_round_trip_and_negative_marker(tmp_path):
    store_cached_avatar(tmp_path, 42, b"jpeg-data", "image/jpeg")

    assert load_cached_avatar(tmp_path, 42, ttl_seconds=60) == (b"jpeg-data", "image/jpeg")
    assert not missing_cache_is_fresh(tmp_path, 42, ttl_seconds=60)

    mark_avatar_missing(tmp_path, 42)
    assert missing_cache_is_fresh(tmp_path, 42, ttl_seconds=60)


def test_avatar_cache_ignores_expired_or_invalid_entries(tmp_path):
    store_cached_avatar(tmp_path, 7, b"image", "image/png")
    expired = time.time() - 120
    os.utime(tmp_path / "7.bin", (expired, expired))
    os.utime(tmp_path / "7.json", (expired, expired))

    assert load_cached_avatar(tmp_path, 7, ttl_seconds=60) is None

    (tmp_path / "8.bin").write_bytes(b"not-an-image")
    (tmp_path / "8.json").write_text('{"media_type":"text/plain"}', encoding="utf-8")
    assert load_cached_avatar(tmp_path, 8, ttl_seconds=60) is None
