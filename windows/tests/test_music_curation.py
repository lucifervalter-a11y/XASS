"""Synthetic local audio only. No user library, provider, decoder or GPU access."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import wave

WINDOWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WINDOWS)); sys.path.insert(0, str(WINDOWS.parent / "pc_client"))
from music_curation import CurationService, CurationError, identity, write_tags, _read_tags, lookup_lyrics, lookup, search_terms, cover_thumbnail, match_rating


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"; self.data.mkdir()
        self.music = self.root / "music"; self.music.mkdir()
        self.path = self.make_audio("Artist - Song.wav")
        self.service = CurationService(self.data)

    def make_audio(self, name, seconds=2, title="Song", artist="Artist"):
        path = self.music / name
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
            wav.writeframes(b"\0\0" * 8000 * seconds)
        write_tags(path, {"title": title, "artist": artist, "album": "Original"})
        return path

    def scan(self):
        rows = self.service.start(str(self.music))["tracks"]
        return [self.service.inspect(row["id"]) for row in rows]

    def proposal(self, changes=None):
        row = self.scan()[0]
        return self.service.propose(row["id"], changes or {"title": "Corrected"})

    def test_audit_reports_missing_art_lyrics_and_plain_timing_without_write(self):
        before = self.path.read_bytes()
        row = self.scan()[0]
        self.assertEqual(row["issues"], ["Нет обложки", "Нет текста"])
        self.assertEqual(self.path.read_bytes(), before)
        write_tags(self.path, {"lyrics": "A line"})
        row = self.scan()[0]
        self.assertIn("Текст без таймингов", row["issues"])
        write_tags(self.path, {"lyrics": "[00:00.10]A line"})
        self.assertNotIn("Текст без таймингов", self.scan()[0]["issues"])

    def test_confirmed_apply_preserves_other_tags_and_undo_restores_exact_bytes(self):
        from mutagen.wave import WAVE
        from mutagen.id3 import TXXX
        audio = WAVE(self.path); audio.tags.add(TXXX(encoding=3, desc="rare-private-tag", text="keep me")); audio.save()
        before = self.path.read_bytes()
        proposal = self.proposal({"title": "Corrected", "lyrics": "[00:00.10]New line"})
        self.assertEqual(proposal["before"]["title"], "Song")
        self.assertEqual(proposal["after"]["title"], "Corrected")
        with self.assertRaises(CurationError): self.service.apply(proposal["id"], False)
        self.assertEqual(self.path.read_bytes(), before)
        edit = self.service.apply(proposal["id"], True)
        self.assertEqual(_read_tags(self.path)["title"], "Corrected")
        self.assertEqual(str(WAVE(self.path).tags["TXXX:rare-private-tag"]), "keep me")
        self.assertEqual(self.service.undo(edit["id"], True)["state"], "reverted")
        self.assertEqual(self.path.read_bytes(), before)

    def test_changed_file_rejected_after_review(self):
        proposal = self.proposal()
        write_tags(self.path, {"album": "Changed externally"})
        changed = self.path.read_bytes()
        with self.assertRaisesRegex(CurationError, "после просмотра"):
            self.service.apply(proposal["id"], True)
        self.assertEqual(self.path.read_bytes(), changed)

    def test_undo_cannot_destroy_later_external_edit(self):
        edit = self.service.apply(self.proposal()["id"], True)
        write_tags(self.path, {"artist": "Someone else"})
        changed = self.path.read_bytes()
        with self.assertRaisesRegex(CurationError, "Откат заблокирован"):
            self.service.undo(edit["id"], True)
        self.assertEqual(self.path.read_bytes(), changed)

    def test_two_proposals_same_original_only_one_can_apply(self):
        row = self.scan()[0]
        first = self.service.propose(row["id"], {"title": "First"})
        second = self.service.propose(row["id"], {"title": "Second"})
        self.service.apply(first["id"], True)
        with self.assertRaises(CurationError): self.service.apply(second["id"], True)
        self.assertEqual(_read_tags(self.path)["title"], "First")

    def test_write_failure_leaves_original_and_no_completed_history(self):
        before = self.path.read_bytes(); proposal = self.proposal()
        with patch("music_curation.write_tags", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError): self.service.apply(proposal["id"], True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.service.history()["edits"], [])
        self.assertEqual(list(self.music.glob(".xass-curation-*")), [])

    def test_crash_before_replace_is_detected_without_touching_original(self):
        before = self.path.read_bytes(); proposal = self.proposal()
        with patch("music_curation.os.replace", side_effect=SystemExit("killed before rename")):
            with self.assertRaises(SystemExit): self.service.apply(proposal["id"], True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.service.history()["edits"][0]["state"], "not_applied")

    def test_crash_after_replace_recovers_applied_state_and_can_undo(self):
        before = self.path.read_bytes(); proposal = self.proposal()
        replace = os.replace
        def interrupted(source, destination):
            replace(source, destination); raise SystemExit("killed after rename")
        with patch("music_curation.os.replace", side_effect=interrupted):
            with self.assertRaises(SystemExit): self.service.apply(proposal["id"], True)
        row = self.service.history()["edits"][0]
        self.assertEqual(row["state"], "applied")
        self.service.undo(row["id"], True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_crash_after_undo_replace_recovers_reverted(self):
        before = self.path.read_bytes()
        edit = self.service.apply(self.proposal()["id"], True)
        replace = os.replace
        def interrupted(source, destination):
            replace(source, destination); raise SystemExit()
        with patch("music_curation.os.replace", side_effect=interrupted):
            with self.assertRaises(SystemExit): self.service.undo(edit["id"], True)
        self.assertEqual(self.service.history()["edits"][0]["state"], "reverted")
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_backup_blocks_undo(self):
        edit = self.service.apply(self.proposal()["id"], True)
        backup = self.service.root / (edit["id"] + ".original")
        backup.write_bytes(b"broken fixture backup")
        current = self.path.read_bytes()
        with self.assertRaisesRegex(CurationError, "повреждён"):
            self.service.undo(edit["id"], True)
        self.assertEqual(current, self.path.read_bytes())

    def test_corrupt_stage_copy_cannot_replace_original_during_apply(self):
        original = self.path.read_bytes(); proposal = self.proposal()
        copy = shutil.copy2
        def corrupted(source, destination, **kwargs):
            result = copy(source, destination, **kwargs)
            if Path(destination).name.startswith(".xass-curation-"):
                with Path(destination).open("ab") as stream:
                    stream.write(b"copy fault")
            return result
        with patch("music_curation.shutil.copy2", side_effect=corrupted):
            with self.assertRaisesRegex(CurationError, "копия оригинала повреждена"):
                self.service.apply(proposal["id"], True)
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(self.service.history()["edits"], [])
        self.assertEqual(list(self.music.glob(".xass-curation-*")), [])

    def test_corrupt_stage_copy_cannot_replace_current_track_during_undo(self):
        edit = self.service.apply(self.proposal()["id"], True)
        current = self.path.read_bytes(); copy = shutil.copy2
        def corrupted(source, destination, **kwargs):
            result = copy(source, destination, **kwargs)
            if Path(destination).name.startswith(".xass-curation-undo-"):
                with Path(destination).open("ab") as stream:
                    stream.write(b"copy fault")
            return result
        with patch("music_curation.shutil.copy2", side_effect=corrupted):
            with self.assertRaisesRegex(CurationError, "копия оригинала повреждена"):
                self.service.undo(edit["id"], True)
        self.assertEqual(current, self.path.read_bytes())
        self.assertEqual(self.service.history()["edits"][0]["state"], "applied")
        self.assertEqual(list(self.music.glob(".xass-curation-*")), [])

    def test_duplicate_candidates_not_inferred_from_duration_or_variant(self):
        shutil.copy2(self.path, self.music / "byte-copy.wav")
        self.make_audio("different-encoding.wav", 8)
        self.make_audio("live.wav", title="Song (Live)")
        self.make_audio("remaster.wav", title="Song (Remastered)")
        originals = {p.name: p.read_bytes() for p in self.music.iterdir()}
        self.scan()
        groups = self.service.duplicates()["groups"]
        self.assertEqual(sum(g["kind"] == "exact_bytes" for g in groups), 1)
        candidate = next(g for g in groups if g["kind"] == "candidate")
        self.assertEqual(len(candidate["tracks"]), 3)
        self.assertIn("не доказывает", candidate["reason"])
        self.assertFalse(any(t["name"] in {"live.wav", "remaster.wav"} for g in groups for t in g["tracks"]))
        self.assertEqual(originals, {p.name: p.read_bytes() for p in self.music.iterdir()})

    def test_manual_artwork_sanitized_and_original_undoable(self):
        from PIL import Image
        art = self.root / "cover.png"; Image.new("RGB", (32, 32), "red").save(art)
        row = self.scan()[0]; original = self.path.read_bytes()
        proposal = self.service.propose(row["id"], {}, artwork_path=str(art))
        self.assertTrue(proposal["artwork_changed"])
        self.assertNotIn("artwork_data", json.dumps(proposal))
        edit = self.service.apply(proposal["id"], True)
        self.assertTrue(_read_tags(self.path)["artwork"])
        self.service.undo(edit["id"], True)
        self.assertEqual(original, self.path.read_bytes())

    def test_proposals_survive_new_service_and_are_reused_on_selection(self):
        proposal = self.proposal()
        again = CurationService(self.data)
        with again.database() as db:
            track = db.execute("SELECT id FROM tracks").fetchone()[0]
        self.assertEqual(again.inspect(track)["proposals"][0]["id"], proposal["id"])

    def test_provider_response_after_external_refresh_cannot_override_new_file(self):
        row = self.scan()[0]
        def delayed(tags):
            write_tags(self.path, {"album": "External"}); self.service.inspect(row["id"])
            return {"status": "found", "candidates": [{"changes": {"title": "Stale"}, "source": "fixture", "source_id": "1", "confidence": "weak", "evidence": []}]}
        self.service.provider = delayed
        with self.assertRaisesRegex(CurationError, "изменился"):
            self.service.candidates(row["id"])
        self.assertEqual(_read_tags(self.path)["title"], "Song")

    def test_hardlinks_and_symlinks_are_not_scanned(self):
        hard = self.music / "hard.wav"
        os.link(self.path, hard)
        self.assertEqual(self.service.start(str(self.music))["tracks"], [])

    def test_stale_manual_digest_rejected(self):
        row = self.scan()[0]
        result = self.service.request("desktop_curation_propose", {"id": row["id"], "digest": "0" * 64, "changes": {"title": "No"}})
        self.assertIn("error", result)

    def test_red_candidate_blocks_apply_until_explicit_manual_choice(self):
        row = self.scan()[0]; original = self.path.read_bytes()
        evidence = {"source": "Имя файла", "source_id": "", **match_rating(), "evidence": ["Не подтверждено"]}
        proposal = self.service.propose(row["id"], {"title": "Uncertain"}, evidence=evidence)
        self.assertEqual(proposal["match_score"], 40)
        for choice in (False, "true", 1, None):
            with self.assertRaisesRegex(CurationError, "явно выберите"):
                self.service.apply(proposal["id"], True, manual_choice=choice)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.service.history()["edits"], [])
        self.service.apply(proposal["id"], True, manual_choice=True)
        self.assertEqual(_read_tags(self.path)["title"], "Uncertain")

    def test_green_candidate_keeps_source_score_and_still_requires_apply_confirmation(self):
        row = self.scan()[0]; original = self.path.read_bytes()
        evidence = {"source": "AcoustID / MusicBrainz", "source_id": "fixture", **match_rating(source_score=.91, difference=1), "evidence": ["Fixture evidence"]}
        proposal = self.service.propose(row["id"], {"title": "Matched"}, evidence=evidence)
        again = self.service.inspect(row["id"])["proposals"][0]
        self.assertEqual((again["match_score"], again["source_score"], again["confidence"]), (95, .91, "high"))
        with self.assertRaises(CurationError): self.service.apply(proposal["id"], False)
        self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_unscored_proposal_is_red_in_view_and_backend(self):
        proposal = self.proposal()
        evidence = {"source": "MusicBrainz", "source_id": "fixture", "confidence": "review", "evidence": []}
        with self.service.database() as db:
            db.execute("UPDATE proposals SET evidence=? WHERE id=?", (json.dumps(evidence), proposal["id"])); db.commit()
            key = db.execute("SELECT track FROM proposals WHERE id=?", (proposal["id"],)).fetchone()[0]
        view = self.service.inspect(key)["proposals"][0]
        self.assertEqual((view["match_score"], view["confidence"]), (40, "weak"))
        with self.assertRaisesRegex(CurationError, "явно выберите"):
            self.service.apply(proposal["id"], True)

    def test_transcription_is_an_explicit_reviewed_proposal_and_never_opens_microphone(self):
        row = self.scan()[0]; before = self.path.read_bytes()
        model_folder = self.root / "fixture-model"; model_folder.mkdir()
        segment = SimpleNamespace(start=.2, end=1.2, text="Synthetic lyric", words=[])
        model = SimpleNamespace(transcribe=lambda *args, **kwargs: (iter([segment]), SimpleNamespace()))
        with patch("voice_capture.WhisperTranscriber", return_value=SimpleNamespace(model=model)) as recognizer:
            proposal = self.service.transcribe(row["id"], str(model_folder))
        recognizer.assert_called_once_with(str(model_folder))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(proposal["source"], "Локальный Whisper")
        self.assertEqual(proposal["confidence"], "weak")
        self.assertIn("[00:00.20]Synthetic lyric", proposal["after"]["lyrics"])

    def test_empty_transcription_preserves_existing_lyrics(self):
        write_tags(self.path, {"lyrics": "Keep original"})
        row = self.scan()[0]; before = self.path.read_bytes()
        model_folder = self.root / "fixture-model"; model_folder.mkdir()
        model = SimpleNamespace(transcribe=lambda *args, **kwargs: (iter([]), SimpleNamespace()))
        with patch("voice_capture.WhisperTranscriber", return_value=SimpleNamespace(model=model)):
            with self.assertRaisesRegex(CurationError, "Исходный текст сохранён"):
                self.service.transcribe(row["id"], str(model_folder))
        self.assertEqual(before, self.path.read_bytes())

    def test_different_scan_does_not_lose_undo_history(self):
        original = self.path.read_bytes()
        edit = self.service.apply(self.proposal()["id"], True)
        other = self.root / "another-library"; other.mkdir()
        self.service.start(str(other))
        self.service.undo(edit["id"], True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_local_fingerprint_without_provider_key_is_persisted_as_review_only(self):
        second = self.make_audio("renamed.wav", title="Different title", artist="Different artist")
        originals = {path: path.read_bytes() for path in (self.path, second)}
        rows = self.scan()
        with patch.dict(os.environ, {"XASS_ACOUSTID_KEY": ""}), patch("music_curation.local_fingerprint", return_value={"duration": 2, "fingerprint": "synthetic-private-fingerprint"}), patch("music_curation._json_get") as network:
            for row in rows:
                self.assertTrue(self.service.fingerprint(row["id"])["available"])
        network.assert_not_called()
        again = CurationService(self.data)
        group = next(g for g in again.duplicates()["groups"] if g["kind"] == "fingerprint_candidate")
        self.assertIn("не побайтовая копия", group["reason"])
        self.assertEqual(len(group["tracks"]), 2)
        with again.database() as db:
            stored = db.execute("SELECT fingerprint FROM fingerprints").fetchone()[0]
            self.assertNotEqual(stored, "synthetic-private-fingerprint")
            self.assertEqual(len(stored), 64)
        self.assertEqual(originals, {path: path.read_bytes() for path in originals})

    def test_matching_fingerprint_keeps_variants_and_large_duration_differences_separate(self):
        self.make_audio("Live.wav", title="Song (Live)")
        self.make_audio("Extended.wav", title="Another title", seconds=8)
        rows = self.scan()
        for row in rows:
            with patch("music_curation.local_fingerprint", return_value={"duration": row["tags"]["duration"], "fingerprint": "same-fingerprint"}):
                self.service.fingerprint(row["id"])
        self.assertFalse(any(g["kind"] == "fingerprint_candidate" for g in self.service.duplicates()["groups"]))

    def test_filename_variant_is_protected_even_when_title_tag_omits_it(self):
        self.make_audio("Artist - Song (Live).wav", title="Song")
        rows = self.scan()
        live = next(row for row in rows if "Live" in row["name"])
        self.assertEqual(search_terms(live["tags"])["variants"], ["live"])
        self.assertFalse(any(group["kind"] == "candidate" for group in self.service.duplicates()["groups"]))

    def test_fingerprint_cannot_be_attached_after_file_changes_during_analysis(self):
        row = self.scan()[0]
        def delayed(path):
            write_tags(path, {"title": "Changed externally"}); self.service.inspect(row["id"])
            return {"duration": 2, "fingerprint": "stale-fingerprint"}
        with patch("music_curation.local_fingerprint", side_effect=delayed):
            with self.assertRaisesRegex(CurationError, "изменился во время"):
                self.service.fingerprint(row["id"])
        with self.service.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM fingerprints").fetchone()[0], 0)

    def test_fingerprint_invalidated_after_reviewed_tag_write(self):
        self.make_audio("other.wav", title="Different title")
        rows = self.scan()
        with patch("music_curation.local_fingerprint", return_value={"duration": 2, "fingerprint": "same-fingerprint"}):
            for row in rows: self.service.fingerprint(row["id"])
        proposal = self.service.propose(rows[0]["id"], {"album": "New album"})
        self.service.apply(proposal["id"], True); self.service.inspect(rows[0]["id"])
        self.assertFalse(any(g["kind"] == "fingerprint_candidate" for g in self.service.duplicates()["groups"]))

    def test_multi_undo_newest_first_restores_chain_but_preserves_conflicting_track(self):
        other = self.make_audio("Other.wav", title="Other")
        original = self.path.read_bytes(); rows = self.scan()
        track = next(row for row in rows if row["name"] == self.path.name)
        other_track = next(row for row in rows if row["name"] == other.name)
        self.service.apply(self.service.propose(track["id"], {"title": "First"})["id"], True)
        self.service.inspect(track["id"])
        self.service.apply(self.service.propose(track["id"], {"title": "Second"})["id"], True)
        self.service.apply(self.service.propose(other_track["id"], {"title": "Third"})["id"], True)
        write_tags(other, {"album": "External changes"}); external = other.read_bytes()
        restored, conflicts = 0, 0
        for edit in self.service.history()["edits"]:
            try: self.service.undo(edit["id"], True); restored += 1
            except CurationError: conflicts += 1
        self.assertEqual((restored, conflicts), (2, 1))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(other.read_bytes(), external)


class ProviderTests(unittest.TestCase):
    def tags(self, **values):
        return {"title": "Song", "artist": "Artist", "album": "", "lyrics": "", "duration": 120, "variants": [], "filename": "Artist - Song", **values}
    def row(self, **values):
        return {"id": 42, "trackName": "Song", "artistName": "Artist", "albumName": "Album", "duration": 120, "syncedLyrics": "[00:01.00]Fixture lyric", **values}

    def test_exact_metadata_never_claims_fingerprint_accuracy(self):
        with patch("music_curation._json_get", return_value=[self.row()]):
            candidate = lookup_lyrics(self.tags())["candidates"][0]
        self.assertEqual(candidate["confidence"], "review")
        self.assertTrue(any("не проверялся" in line for line in candidate["evidence"]))
        self.assertNotIn("90", json.dumps(candidate))
        self.assertEqual(candidate["match_score"], 75)
        self.assertIsNone(candidate["source_score"])

    def test_match_score_contract_boundaries_are_not_provider_probabilities(self):
        self.assertEqual(match_rating(source_score=.9, difference=1)["match_score"], 95)
        self.assertEqual(match_rating(source_score=.899, difference=1)["match_score"], 65)
        for difference in (None, 1.001, float("inf"), float("nan"), -1):
            self.assertLess(match_rating(source_score=.99, difference=difference)["match_score"], 90)
        for conflict in ("year", "track", "art"):
            self.assertEqual(match_rating(source_score=.99, difference=0, conflicts=[conflict])["match_score"], 65)
        self.assertEqual(match_rating(source_score=.99, difference=0, versions_match=False)["match_score"], 40)
        self.assertEqual(match_rating(source_score=.59, difference=0, catalog=True)["match_score"], 40)
        self.assertEqual(match_rating(catalog=True, difference=0)["match_score"], 75)
        self.assertEqual(match_rating(catalog=True, difference=0, conflicts=["album"])["match_score"], 65)

    def acoustic_result(self, tags=None, *, duration=120000, art="", date="2000", score=.98):
        release = "11111111-1111-1111-1111-111111111111"
        record = {"id": "22222222-2222-2222-2222-222222222222", "title": "Song", "length": duration,
                  "artist-credit": [{"name": "Artist"}], "releases": [{"id": release, "title": "Album", "status": "Official", "date": date}]}
        def provider(url, params):
            return [] if "lrclib" in url else dict(record)
        with patch("music_curation._json_get", side_effect=provider), patch("music_curation.cover_thumbnail", return_value=art):
            result = lookup(tags or self.tags(), acoustic=[(record["id"], score)])
        return next(candidate for candidate in result["candidates"] if candidate["source"] == "AcoustID / MusicBrainz")

    def test_acoustic_green_requires_known_local_and_catalog_duration(self):
        candidate = self.acoustic_result()
        self.assertEqual((candidate["match_score"], candidate["source_score"]), (95, .98))
        for duration in (None, 0, 124000):
            self.assertLess(self.acoustic_result(duration=duration)["match_score"], 90)
        self.assertLess(self.acoustic_result(self.tags(duration=0))["match_score"], 90)

    def test_acoustic_year_track_and_art_conflicts_never_become_green(self):
        cases = [self.acoustic_result(self.tags(year="2001")), self.acoustic_result(self.tags(track_number="3/12")), self.acoustic_result(art="fixture-image")]
        for candidate in cases:
            self.assertEqual(candidate["match_score"], 65)
            self.assertEqual(candidate["confidence"], "review")
        self.assertTrue(any("Обложка" in evidence and "не подтверждает" in evidence for evidence in cases[2]["evidence"]))
        self.assertEqual(self.acoustic_result(self.tags(year="2000"))["match_score"], 95)

    def test_wrong_version_and_out_of_range_timestamps_rejected(self):
        with patch("music_curation._json_get", return_value=[self.row(trackName="Song (Live)")]):
            self.assertEqual(lookup_lyrics(self.tags())["candidates"], [])
        with patch("music_curation._json_get", return_value=[self.row(syncedLyrics="[09:00]Wrong recording")]):
            self.assertNotIn("lyrics", lookup_lyrics(self.tags())["candidates"][0]["changes"])
        with patch("music_curation._json_get", return_value=[self.row(duration=126)]):
            candidate = lookup_lyrics(self.tags())["candidates"][0]
            self.assertEqual(candidate["confidence"], "weak"); self.assertNotIn("lyrics", candidate["changes"])

    def test_filename_cleanup_keeps_rare_version(self):
        tags = search_terms(self.tags(title="", artist="", filename="01. Artist - Song_(Acoustic)"))
        self.assertEqual(tags["artist"], "Artist")
        self.assertEqual(tags["title"], "Song (Acoustic)")
        self.assertEqual(tags["variants"], ["acoustic"])

    def test_musicbrainz_cover_is_explicit_candidate_and_provider_failure_partial(self):
        release = "11111111-1111-1111-1111-111111111111"
        record = {"id": "22222222-2222-2222-2222-222222222222", "title": "Song", "length": 120000,
                  "artist-credit": [{"name": "Artist"}], "releases": [{"id": release, "title": "Album", "status": "Official"}]}
        def provider(url, params):
            if "lrclib" in url: raise OSError("offline")
            return {"recordings": [record]}
        with patch("music_curation._json_get", side_effect=provider), patch("music_curation.cover_thumbnail", return_value="fixture-image") as cover:
            result = lookup(self.tags())
        cover.assert_called_once_with(release)
        self.assertEqual(result["candidates"][0]["artwork_data"], "fixture-image")
        self.assertEqual(result["candidates"][0]["source"], "MusicBrainz")
        self.assertTrue(result["warnings"])

    def test_absent_acoustic_setup_is_honest_and_does_not_launch_process(self):
        from music_curation import acoustic_recordings
        with patch.dict(os.environ, {"XASS_FPCALC": "", "XASS_ACOUSTID_KEY": ""}), patch("music_curation.subprocess.run") as run:
            rows, notice = acoustic_recordings(Path("missing.wav"))
        self.assertEqual(rows, []); self.assertIn("нет ключа", notice); run.assert_not_called()

    def test_local_fpcalc_needs_no_provider_key(self):
        from music_curation import local_fingerprint
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "fpcalc.exe"; binary.write_bytes(b"fixture-not-executable")
            response = SimpleNamespace(returncode=0, stdout=b'{"duration":120,"fingerprint":"fixture-fingerprint"}')
            with patch.dict(os.environ, {"XASS_ACOUSTID_KEY": "", "XASS_FPCALC": str(binary)}), patch("music_curation.subprocess.run", return_value=response) as run:
                result = local_fingerprint(Path(folder) / "fixture.wav")
        self.assertEqual(result["duration"], 120)
        self.assertIn("-length", run.call_args.args[0])
        self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
