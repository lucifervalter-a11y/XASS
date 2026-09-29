"""Exact-name covers. No live provider calls."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from app.services.music_covers import adopted_catalog_names, cover_file, cover_queries, cover_url, pick_cover_digest, pick_joined_release, prefetch_exact_covers

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

    def test_yo_slowed_and_a_short_suffix_still_match_the_same_artist(self):
        rows = [
            {"title": "хочу быть с ней и всë", "title_short": "хочу быть с ней и всë", "artist": {"name": "урал гайсин"},
             "album": {"title": "Single", "md5_image": DIGEST}},
            {"title": "я молодой вампир vamp", "title_short": "я молодой вампир vamp", "artist": {"name": "урал гайсин"},
             "album": {"title": "Pack", "md5_image": OTHER}},
            {"title": "Song Remix", "title_short": "Song Remix", "artist": {"name": "Artist"},
             "album": {"title": "Album", "md5_image": "d" * 32}},
        ]
        self.assertEqual(pick_cover_digest(rows, "Хочу быть с ней и всё", "Урал Гайсин", ""), DIGEST)
        self.assertEqual(pick_cover_digest(rows, "хочу быть с ней и всё (slowed)", "урал гайсин", ""), DIGEST)
        self.assertEqual(pick_cover_digest(rows, "я молодой вампир", "урал гайсин", ""), OTHER)
        self.assertIsNone(pick_cover_digest(rows, "Song", "Artist", ""))
        self.assertEqual(cover_queries("урал гайсин", "блюз"), ["урал гайсин блюз", "блюз"])

    def test_untagged_filename_matches_one_artist_title_and_ignores_a_different_song(self):
        rows = [
            {"title": "Конфетка", "artist": {"name": "Marry Me, Bellamy"}, "album": {"title": "Конфетка", "md5_image": DIGEST}},
            {"title": "КОНФЕТКА (REMIX)", "artist": {"name": "Marry Me, Bellamy"}, "album": {"title": "Remix", "md5_image": OTHER}},
            {"title": "Хочу быть с ней и всё", "artist": {"name": "урал гайсин"}, "album": {"title": "Single", "md5_image": "d" * 32}},
        ]
        found = pick_joined_release(rows, "Marry Me Bellamy Конфетка")
        self.assertEqual(found[0], DIGEST)
        self.assertEqual(found[1], "Marry Me, Bellamy")
        self.assertIsNone(pick_joined_release(rows, "урал гайсин священная война"))
        self.assertIsNone(pick_joined_release(rows, "Marry Me Bellamy"))

    def test_a_longer_near_title_does_not_rename_the_track(self):
        from app.services import music_covers
        music_covers._chosen.clear()
        key_artist, key_title = "MARRY ME, BELLAMY", "GENSHIN IMPACT"
        music_covers._chosen[f"{music_covers._norm(key_artist)}\n{music_covers._norm(key_title)}\n"] = ("Marry Me, Bellamy", "GENSHIN IMPACT vamp")
        self.assertIsNone(adopted_catalog_names("MARRY ME, BELLAMY - GENSHIN IMPACT", ""))
        music_covers._chosen[f"{music_covers._norm(key_artist)}\n{music_covers._norm(key_title)}\n"] = ("Marry Me, Bellamy", "GENSHIN IMPACT")
        self.assertEqual(adopted_catalog_names("MARRY ME, BELLAMY - GENSHIN IMPACT", ""), ("Marry Me, Bellamy", "GENSHIN IMPACT"))
        music_covers._chosen["\nурал гайсин священная война\n"] = ("урал гайсин", "Хочу быть с ней и всё")
        self.assertIsNone(adopted_catalog_names("урал гайсин священная война", ""))


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 30, 40)).save(buffer, "JPEG")
    return buffer.getvalue()


class CoverPrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_cover_is_stored_and_a_miss_is_not(self):
        jpeg = _jpeg()
        sha = "ab" * 32
        storage = "cd" * 16 + ".mp3"
        other = "ef" * 32
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = {"id": 1, "title": "Конфетка", "artist": "Marry Me, Bellamy", "album": "", "sha256": sha, "storage_name": storage}
            missed = {"id": 2, "title": "священная война", "artist": "урал гайсин", "album": "", "sha256": other, "storage_name": "12" * 16 + ".mp3"}
            calls = []

            async def fake(title, artist, album=""):
                calls.append(title)
                return jpeg if title == "Конфетка" else None

            with patch("app.services.music_covers.display_cover", fake):
                stored = await prefetch_exact_covers(root, [row, missed])
            self.assertEqual(stored, 1)
            self.assertEqual(calls, ["Конфетка", "священная война"])
            self.assertTrue(cover_file(root, sha).is_file())
            self.assertFalse(cover_file(root, other).exists())
            calls.clear()
            with patch("app.services.music_covers.display_cover", fake):
                self.assertEqual(await prefetch_exact_covers(root, [row, missed]), 0)
            self.assertEqual(calls, ["священная война"])


if __name__ == "__main__":
    unittest.main()
