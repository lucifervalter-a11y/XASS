"""Catalog search-query normalization: site tags, domains, upload noise, "Artist - Title"."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services import synced_lyrics as sl
from app.services.music_enrichment import _signature
from app.services.music_query import clean_search_text, filename_artist_title, search_names


class MusicQueryTests(unittest.TestCase):
    def test_site_tags_domains_and_upload_noise_are_removed(self):
        self.assertEqual(clean_search_text("Фенибут [mp3xa.cc]"), "Фенибут")
        self.assertEqual(clean_search_text("Song (zaycev.net)"), "Song")
        self.assertEqual(clean_search_text("Song [muzmo.ru] (Official Video)"), "Song")
        self.assertEqual(clean_search_text("Song  www.hitmo.org  "), "Song")
        self.assertEqual(clean_search_text("Song [Скачано с muzofond.fm]"), "Song")
        self.assertEqual(clean_search_text("My_Song.mp3"), "My Song")
        self.assertEqual(clean_search_text("Artist - [site.ru]"), "Artist")

    def test_musical_versions_and_ordinary_dots_are_kept(self):
        self.assertEqual(clean_search_text("Song (Remix) (feat. B)"), "Song (Remix) (feat. B)")
        self.assertEqual(clean_search_text("Mr. Brightside"), "Mr. Brightside")
        self.assertEqual(clean_search_text("Pt.II (Vol.2)"), "Pt.II (Vol.2)")
        self.assertEqual(clean_search_text("Song (Live)"), "Song (Live)")

    def test_artist_title_in_the_title_field(self):
        self.assertEqual(search_names("Нексюша [mp3xa.cc] - Фенибут", ""), ("Нексюша", "Фенибут"))
        self.assertEqual(search_names("Нексюша [mp3xa.cc] - Фенибут", "Unknown Artist"), ("Нексюша", "Фенибут"))
        self.assertEqual(search_names("Нексюша - Фенибут (zaycev.net)", "Нексюша"), ("Нексюша", "Фенибут"))
        self.assertEqual(search_names("Intro - Outro", "Band"), ("Band", "Intro - Outro"),
                         "A dash inside a real title of a tagged file is not an artist")

    def test_synced_lyrics_first_query_is_clean(self):
        self.assertEqual(sl.queries("Нексюша [mp3xa.cc] - Фенибут", "")[0], ("Нексюша", "Фенибут"))
        self.assertEqual(sl.queries("Фенибут [mp3xa.cc]", "Нексюша")[0], ("Нексюша", "Фенибут"))

    def test_enrichment_signature_queries_clean_names_without_touching_the_track(self):
        track = SimpleNamespace(title="Нексюша [mp3xa.cc] - Фенибут", artist="", album="", duration=180)
        signature = _signature(track)
        self.assertEqual((signature["artist"], signature["title"]), ("Нексюша", "Фенибут"))
        self.assertEqual(signature["query"], "Нексюша Фенибут")
        self.assertEqual(track.title, "Нексюша [mp3xa.cc] - Фенибут", "Stored metadata is never rewritten")
        tagged = _signature(SimpleNamespace(title="Нексюша - Фенибут (Official Video)", artist="Нексюша", album="", duration=180))
        self.assertEqual((tagged["artist"], tagged["title"]), ("Нексюша", "Фенибут"))

    def test_untagged_download_names_drop_the_index_and_the_reupload_mark(self):
        self.assertEqual(search_names("01. MARRY ME, BELLAMY - GENSHIN IMPACT", ""), ("MARRY ME, BELLAMY", "GENSHIN IMPACT"))
        self.assertEqual(search_names("урал гайсин священная война reUploads", ""), ("", "урал гайсин священная война"))
        self.assertEqual(filename_artist_title("MARRY ME, BELLAMY - Конфетка", ""), ("MARRY ME, BELLAMY", "Конфетка"))
        self.assertEqual(filename_artist_title("MARRY ME, BELLAMY - Конфетка", "MARRY ME, BELLAMY"), None)
        self.assertIsNone(filename_artist_title("урал гайсин священная война", ""))
        self.assertIsNone(filename_artist_title("A - B - C", ""))
        indexed = _signature(SimpleNamespace(title="01. Fixture Artist - Fixture Song", artist="", album="", duration=180))
        self.assertEqual((indexed["artist"], indexed["title"]), ("Fixture Artist", "Fixture Song"))


if __name__ == "__main__":
    unittest.main()
