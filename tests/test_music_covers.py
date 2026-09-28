"""Exact-name covers. No live provider calls."""
from __future__ import annotations

import unittest

from app.services.music_covers import cover_url, pick_cover_digest

DIGEST = "b" * 32
OTHER = "c" * 32


class CoverPickTests(unittest.TestCase):
    def rows(self):
        return [
            {"title": "Song Remix", "title_short": "Song Remix", "artist": {"name": "Artist"},
             "album": {"title": "Album", "md5_image": "a" * 32}},
            {"title": "Song (feat. Guest)", "title_short": "Song", "artist": {"name": "Artist"},
             "album": {"title": "Other", "md5_image": OTHER}},
            {"title": "Song", "title_short": "Song", "artist": {"name": "Artist"},
             "album": {"title": "Album", "md5_image": DIGEST}},
            {"title": "Song", "title_short": "Song", "artist": {"name": "Someone Else"},
             "album": {"title": "Album", "md5_image": "d" * 32}},
        ]

    def test_exact_title_and_artist_prefer_the_named_album(self):
        self.assertEqual(pick_cover_digest(self.rows(), "Song", "Artist", "Album"), DIGEST)
        self.assertEqual(pick_cover_digest(self.rows(), "Song", "Artist", ""), OTHER)
        self.assertIsNone(pick_cover_digest(self.rows(), "Song", "Nobody", ""))
        self.assertIsNone(pick_cover_digest(self.rows(), "Other Song", "Artist", "Album"))

    def test_cover_url_is_only_the_fixed_thumbnail(self):
        self.assertEqual(cover_url(DIGEST), f"https://cdn-images.dzcdn.net/images/cover/{DIGEST}/500x500-000000-80-0-0.jpg")
        for bad in ("", "abc", "../" + DIGEST, DIGEST.upper(), "e2e018ad9df12e80671538b00d836dcg"):
            self.assertIsNone(cover_url(bad))
        poisoned = [{"title": "Song", "artist": {"name": "Artist"}, "album": {"title": "Album", "md5_image": "https://evil.invalid/a.jpg"}}]
        self.assertIsNone(pick_cover_digest(poisoned, "Song", "Artist", "Album"))


if __name__ == "__main__":
    unittest.main()
