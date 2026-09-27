from pathlib import Path
from types import SimpleNamespace
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from mutagen.id3 import ID3, USLT, SYLT
from mutagen.wave import WAVE

from app.services import music_lyrics as lyrics
from test_music_library import silent_wav


class MusicLyricsTests(unittest.TestCase):
    def test_lrc_repeated_stamps_offsets_order_and_plain_text(self):
        value = lyrics.parse_lyrics("[ar:Fixture]\n[offset:-500]\n[00:02.25][00:04.250]Second\n[00:01.00]First")
        self.assertEqual(value["lines"], [{"time": .5, "text": "First"}, {"time": 1.75, "text": "Second"}, {"time": 3.75, "text": "Second"}])
        self.assertTrue(value["synced"])
        self.assertEqual(lyrics.parse_lyrics("Fixture\r\nSecond\x00")["text"], "Fixture\nSecond")
        self.assertFalse(lyrics.parse_lyrics("Plain text")["synced"])

    def test_untrusted_text_is_bounded_and_not_fetched(self):
        for value in (None, "Я" * lyrics.MAX_TEXT_BYTES, "x\n" * (lyrics.MAX_LINES + 1), "[00:01]" * (lyrics.MAX_LINES + 1) + "x",
                      "[00:01]" * 100 + "x" * 60000):
            self.assertEqual(lyrics.parse_lyrics(value), lyrics.empty_lyrics())
        self.assertEqual(lyrics.parse_lyrics("https://example.invalid/lyrics")["text"], "https://example.invalid/lyrics")

    def test_id3_sylt_requires_millisecond_lyric_timestamps(self):
        tags = ID3(); tags.add(SYLT(encoding=3, lang="eng", format=2, type=1, text=[("Second", 2000), ("First", 1000)]))
        self.assertEqual(lyrics._from_tags(SimpleNamespace(tags=tags))["lines"][0], {"time": 1, "text": "First"})
        tags = ID3(); tags.add(SYLT(encoding=3, lang="eng", format=1, type=1, text=[("Frames", 1000)]))
        self.assertEqual(lyrics._from_tags(SimpleNamespace(tags=tags)), lyrics.empty_lyrics())

    def test_vorbis_and_mp4_tags(self):
        for key in ("lyrics", "unsyncedlyrics", "\xa9lyr"):
            self.assertEqual(lyrics._from_tags(SimpleNamespace(tags={key: ["Fixture"]}))["text"], "Fixture")

    def test_real_tagged_wav_cache_and_deleted_missing_or_unsafe_track(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / ("a" * 32 + ".wav")
            source.write_bytes(silent_wav())
            audio = WAVE(source); audio.add_tags(); audio.tags.add(USLT(encoding=3, lang="eng", text="[00:01.50]Fixture")); audio.save()
            track = SimpleNamespace(storage_name=source.name, sha256=hashlib.sha256(source.read_bytes()).hexdigest(), size=source.stat().st_size, deleted=False)
            self.assertEqual(lyrics.embedded_lyrics(root, track)["lines"], [{"time": 1.5, "text": "Fixture"}])
            with patch.object(lyrics.mutagen, "File", side_effect=AssertionError("Repeated reads must use cache")):
                self.assertTrue(lyrics.embedded_lyrics(root, track)["synced"])
            track.deleted = True
            self.assertEqual(lyrics.embedded_lyrics(root, track), lyrics.empty_lyrics())
            track.deleted = False; track.storage_name = "../outside.wav"
            self.assertEqual(lyrics.embedded_lyrics(root, track), lyrics.empty_lyrics())
