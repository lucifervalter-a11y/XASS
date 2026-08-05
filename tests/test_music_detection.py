from __future__ import annotations

import unittest

from app.services.music_card import build_search_links, fallback_music_card, normalize_track_input
from pc_client.now_playing import _extract_track_from_window_title, _is_probable_non_music_session


class MusicDetectionTests(unittest.TestCase):
    def test_github_tab_is_not_music(self) -> None:
        self.assertEqual(normalize_track_input("lucifervalter-a11y/XASS - GitHub"), "")
        self.assertIsNone(
            _extract_track_from_window_title(
                "lucifervalter-a11y/XASS: dashboard - GitHub - Opera GX",
                "opera.exe",
            )
        )
        self.assertTrue(
            _is_probable_non_music_session(
                {
                    "artist": "",
                    "title": "lucifervalter-a11y/XASS: dashboard",
                    "album": "",
                    "app": "Opera GX",
                }
            )
        )

    def test_real_track_still_builds_immediate_search_links(self) -> None:
        card = fallback_music_card("M83 — Midnight City")
        self.assertEqual(card.artist, "M83")
        self.assertEqual(card.title, "Midnight City")
        links = build_search_links(card)
        self.assertIn("Apple Music", links)
        self.assertIn("Yandex Music", links)
        self.assertIn("M83", links["Google"])


if __name__ == "__main__":
    unittest.main()
