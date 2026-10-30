"""Synthetic files only: audit never modifies originals or accesses providers."""
from __future__ import annotations

from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from app.services.music_curation import (audit_tracks, candidate_groups, edition_markers,
    fingerprint_similarity, local_fingerprint, measured_silence, messy_reasons)


class MusicCurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def audio(self, number=1, padding=0, title="Song", artist="Artist", album="Album"):
        name = f"{number:032x}.wav"
        path = self.root / name
        tone = array("h", [int(8000 * math.sin(i * .17)) for i in range(16000)])
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\0\0" * int(padding * 8000) + tone.tobytes() + b"\0\0" * int(padding * 8000))
        return SimpleNamespace(id=number, title=title, artist=artist, album=album,
            filename=title + ".wav", storage_name=name, size=path.stat().st_size,
            duration=2 + padding * 2, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), deleted=False)

    def test_audit_categories_size_codec_bitrate_and_immutable_files(self):
        row = self.audio(title="track01 Artist - Song [example.ru]")
        source = self.root / row.storage_name
        before = source.read_bytes()
        report = audit_tracks(self.root, [(row, None)])
        track = report["tracks"][0]
        self.assertEqual(track["issues"], ["no_artwork", "no_lyrics", "messy_tags"])
        self.assertEqual(track["codec"], "wave")
        self.assertEqual(track["bitrate"], 128000)
        self.assertEqual(track["size"], len(before))
        self.assertFalse(report["automatic_deletion"])
        self.assertEqual(before, source.read_bytes())
        self.assertNotIn("_sha256", track)
        self.assertNotIn("_raw", track["fingerprint"])

    def test_real_measured_padding_preserves_candidate_despite_raw_duration_difference(self):
        left, right = self.audio(), self.audio(2, padding=1.5)
        report = audit_tracks(self.root, [(left, None), (right, None)])
        self.assertEqual(len(report["candidate_groups"]), 1)
        evidence = report["candidate_groups"][0]["evidence"]
        self.assertEqual(evidence["duration_basis"], "measured_without_boundary_silence")
        self.assertLess(evidence["duration_difference"], .001)
        self.assertAlmostEqual(report["tracks"][1]["silence"]["leading_seconds"], 1.5, places=3)
        self.assertIsNone(report["candidate_groups"][0]["identical_recording"])

    def test_live_acoustic_remaster_radio_edits_never_group_with_original(self):
        original = self.audio()
        for index, marker in enumerate(("Live", "Acoustic", "Remastered", "Radio Edit"), 2):
            other = self.audio(index, album=f"Album [{marker}]")
            report = audit_tracks(self.root, [(original, None), (other, None)])
            self.assertEqual(report["candidate_groups"], [])
        self.assertEqual(edition_markers("Song [Remastered]"), ["remaster"])

    def test_different_master_years_and_live_locations_remain_distinct(self):
        for left_album, right_album in (("Album [2011 Remaster]", "Album [2020 Remaster]"),
                                        ("Album (Live in Berlin)", "Album (Live in Paris)")):
            left, right = self.audio(1, album=left_album), self.audio(2, album=right_album)
            self.assertEqual(audit_tracks(self.root, [(left, None), (right, None)])["candidate_groups"], [])

    def test_silent_audio_is_not_invented_zero_padding(self):
        row = self.audio()
        path = self.root / row.storage_name
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
            output.writeframes(b"\0\0" * 16000)
        silence = measured_silence(path, 2, deadline=time.monotonic() + 2)
        self.assertEqual(silence, {"status": "unmeasured_silent_boundary"})

    def test_missing_audio_uses_unknown_instead_of_false_missing_art_lyrics(self):
        row = self.audio()
        (self.root / row.storage_name).unlink()
        report = audit_tracks(self.root, [(row, None)])
        self.assertEqual(report["tracks"][0]["audio_status"], "unavailable")
        self.assertEqual(report["tracks"][0]["issues"], [])

    def test_recorded_lyrics_and_artwork_satisfy_audit(self):
        row = self.audio()
        record = SimpleNamespace(result={"lyrics": {"text": "Fixture lyrics"}}, owner_lyrics={},
                                 artwork_data=b"fixture", dismissed=False, revision=1)
        self.assertEqual(audit_tracks(self.root, [(row, record)])["tracks"][0]["issues"], [])

    def test_cached_cover_and_timed_lyrics_are_present_without_fetch_or_write(self):
        from app.services.synced_lyrics import fingerprint
        from app.services.music_artwork import CACHE_VERSION
        # Nested music root gives this fixture its own existing sibling cache.
        self.root = self.root / "music"
        self.root.mkdir()
        row = self.audio()
        artwork = self.root / ".artwork"
        artwork.mkdir()
        (artwork / f"{row.sha256}-{CACHE_VERSION}.jpg").write_bytes(b"\xff\xd8\xfffixture")
        cache = self.root.parent / "music-lyrics-cache"
        cache.mkdir()
        (cache / "1.json").write_text(json.dumps({"status": "synced", "text": "Fixture lyrics",
            "fingerprint": fingerprint(row), "cached_at": time.time()}), encoding="utf-8")
        self.assertEqual(audit_tracks(self.root, [(row, None)])["tracks"][0]["issues"], [])
        row.title = "Changed tags"
        self.assertEqual(audit_tracks(self.root, [(row, None)])["tracks"][0]["issues"], ["no_lyrics"])

    def test_messy_tags_does_not_rename_musical_version(self):
        self.assertEqual(messy_reasons("Song [Live]", "Artist"), [])
        self.assertIn("artist_repeated_in_title", messy_reasons("Artist - Song", "Artist"))
        self.assertIn("website_in_tags", messy_reasons("Song www.example.ru", "Artist"))

    def test_shifted_fingerprint_is_candidate_not_proof(self):
        base = [(i * 2654435761) & 0xFFFFFFFF for i in range(100)]
        result = fingerprint_similarity({"_raw": base}, {"_raw": [1] * 10 + base})
        self.assertEqual(result["similarity"], 1)
        self.assertEqual(result["shift_frames"], -10)
        self.assertIsNone(fingerprint_similarity({"_raw": [1]}, {"_raw": [1]}))
        self.assertIsNone(fingerprint_similarity({"_raw": base}, {"_raw": base}, deadline=time.monotonic() - 1))

    @unittest.skipUnless(os.environ.get("XASS_FPCALC") or shutil.which("fpcalc"), "optional local fpcalc is not installed")
    def test_real_fpcalc_json_raw_and_leading_padding_candidate(self):
        rows = []
        rate = 11025
        tone = array("h", [int(9000 * math.sin(2 * math.pi * (220 + 55 * ((i // rate) % 5)) * i / rate)
                              + 3000 * math.sin(2 * math.pi * 523.25 * i / rate)) for i in range(20 * rate)])
        for index, padding in ((1, 0), (2, 1.5)):
            row = self.audio(index)
            path = self.root / row.storage_name
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate)
                output.writeframes(b"\0\0" * int(padding * rate) + tone.tobytes())
            row.size, row.duration = path.stat().st_size, 20 + padding
            row.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append((row, None))
        report = audit_tracks(self.root, rows, acoustic=True)
        self.assertTrue(report["acoustic_comparison_complete"])
        self.assertTrue(all(row["fingerprint"]["status"] == "measured" for row in report["tracks"]), report)
        evidence = report["candidate_groups"][0]["evidence"]
        self.assertGreaterEqual(evidence["chromaprint"]["similarity"], .9)
        self.assertEqual(evidence["duration_basis"], "measured_without_boundary_silence")
        self.assertIsNone(report["candidate_groups"][0]["identical_recording"])

    def test_fpcalc_is_local_bounded_and_opaque_data_not_returned(self):
        row = self.audio()
        raw = list(range(30))
        with patch("app.services.music_curation.shutil.which", return_value="fpcalc"), \
             patch("app.services.music_curation.subprocess.run", return_value=SimpleNamespace(returncode=0,
                    stdout=json.dumps({"duration": 120, "fingerprint": raw}).encode())) as run:
            value = local_fingerprint(self.root / row.storage_name, time.monotonic() + 2)
        self.assertEqual(value["source"], "local_chromaprint")
        self.assertIn("-raw", run.call_args.args[0])
        self.assertLessEqual(run.call_args.kwargs["timeout"], 2)

    def test_fpcalc_terminal_eof_requires_exact_notice_valid_duration_and_raw(self):
        row = self.audio()
        raw = list(range(30))
        eof = b"ERROR: Error decoding audio frame (End of file)\n"
        for code, stderr, duration, fingerprint, accepted in (
            (3, eof, 20, raw, True),
            (2, eof, 20, raw, False),
            (3, b"ERROR: Invalid data found when processing input", 20, raw, False),
            (3, eof + b"another error", 20, raw, False),
            (3, b"", 20, raw, False),
            (3, eof, float("nan"), raw, False),
            (3, eof, 0, raw, False),
            (3, eof, 20, [True] * 30, False),
        ):
            with self.subTest(code=code, stderr=stderr, duration=duration), \
                 patch("app.services.music_curation.shutil.which", return_value="fpcalc"), \
                 patch("app.services.music_curation.subprocess.run", return_value=SimpleNamespace(returncode=code,
                       stderr=stderr, stdout=json.dumps({"duration": duration, "fingerprint": fingerprint}).encode())):
                result = local_fingerprint(self.root / row.storage_name, time.monotonic() + 2)
                self.assertEqual(result["status"], "measured" if accepted else "unavailable")
                if accepted:
                    self.assertEqual(result["decoder_notice"], "terminal_eof")

    def test_malformed_fingerprint_does_not_abort_audit(self):
        row = self.audio()
        with patch("app.services.music_curation.shutil.which", return_value="fpcalc"), \
             patch("app.services.music_curation.subprocess.run", return_value=SimpleNamespace(returncode=0,
                    stdout=b'{"fingerprint": [true, "wrong"]}')):
            result = audit_tracks(self.root, [(row, None)], acoustic=True)
        self.assertEqual(result["tracks"][0]["fingerprint"], {"status": "unavailable"})

    def test_path_traversal_and_symlink_are_not_read(self):
        row = self.audio()
        row.storage_name = "../outside.wav"
        self.assertEqual(audit_tracks(self.root, [(row, None)])["tracks"][0]["audio_status"], "unavailable")

    def test_busy_worker_and_track_limits(self):
        from app.services.music_curation import AuditBusy, _slots
        row = self.audio()
        with self.assertRaises(ValueError):
            audit_tracks(self.root, [(row, None)] * 21)
        _slots.acquire(); _slots.acquire()
        try:
            with self.assertRaises(AuditBusy):
                audit_tracks(self.root, [(row, None)])
        finally:
            _slots.release(); _slots.release()
