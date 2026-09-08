from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import music_library as music


def silent_wav(seconds=5) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * (8000 * seconds))
    return output.getvalue()


class MusicLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = SimpleNamespace(bot_token="fixture-bot", setup_api_key="fixture-setup",
                                        pwa_session_generation_path=str(self.root / "generation"))

    def test_filename_and_storage_names_cannot_escape_managed_directory(self):
        self.assertEqual(music.filename(r"C:\fakepath\Моя песня.WAV"), "Моя песня.WAV")
        self.assertEqual(music.filename("../../track\x00.wav"), "track.wav")
        for name in ("", ".env", "track.wav.exe", "track.opus"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                music.filename(name)
        for name in ("../" + "a" * 32 + ".wav", "a" * 32 + ".wav/../.env", "a" * 31 + ".wav", "a" * 32 + ".exe"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                music.track_path(self.root, name)
        self.assertEqual(music.track_path(self.root, "a" * 32 + ".wav"), self.root / ("a" * 32 + ".wav"))

    def test_actual_wav_metadata_and_digest_not_filename_claim(self):
        data = silent_wav()
        path = self.root / "incoming.part"
        path.write_bytes(data)
        info = music.inspect_audio(path, "Моя музыка.wav")
        self.assertEqual(info["title"], "Моя музыка")
        self.assertEqual(info["duration"], 5)
        self.assertEqual(info["mime"], "audio/wav")
        self.assertEqual(info["size"], len(data))
        self.assertEqual(info["sha256"], hashlib.sha256(data).hexdigest())
        with self.assertRaises(ValueError):
            music.inspect_audio(path, "disguised.mp3")
        path.write_bytes(b"not music")
        with self.assertRaises(ValueError):
            music.inspect_audio(path, "invalid.wav")

    def test_invalid_duration_and_unsupported_ogg_opus_are_rejected(self):
        path = self.root / "incoming.part"
        path.write_bytes(b"fixture")
        for length in (0, float("nan"), float("inf"), -1, 86401):
            fake = SimpleNamespace(info=SimpleNamespace(length=length), tags={})
            with self.subTest(duration=length), patch.object(music.mutagen, "File", return_value=fake), self.assertRaises(ValueError):
                music.inspect_audio(path, "bad.wav")
        opus_class = type("OggOpus", (), {"__module__": "mutagen.oggopus"})
        opus = opus_class()
        opus.info, opus.tags = SimpleNamespace(length=5), {}
        with patch.object(music.mutagen, "File", return_value=opus), self.assertRaises(ValueError):
            music.inspect_audio(path, "unsupported.ogg")

    def test_tickets_bind_track_purpose_expiry_secret_and_generation(self):
        with patch.object(music.time, "time", return_value=1000):
            ticket = music.issue_ticket(self.settings, 7, purpose="listen", ttl=30)
            self.assertEqual(music.verify_ticket(self.settings, ticket, 7)["p"], "listen")
            self.assertIsNone(music.verify_ticket(self.settings, ticket, 8))
            self.assertIsNone(music.verify_ticket(self.settings, ticket, 7, purposes=("agent",)))
            agent = music.issue_ticket(self.settings, 7, purpose="agent", binding="credential-hash")
            self.assertEqual(music.verify_ticket(self.settings, agent, 7, purposes=("agent",))["b"], "credential-hash")
            self.assertIsNone(music.verify_ticket(self.settings, agent, 7))
            for invalid in ("", "malformed", ticket[:-1] + ("1" if ticket[-1] != "1" else "2"), "x" * 1025):
                self.assertIsNone(music.verify_ticket(self.settings, invalid, 7))
        with patch.object(music.time, "time", return_value=1030):
            self.assertIsNone(music.verify_ticket(self.settings, ticket, 7))
        with patch.object(music.time, "time", return_value=1001):
            Path(self.settings.pwa_session_generation_path).write_text("1")
            self.assertIsNone(music.verify_ticket(self.settings, ticket, 7))
            Path(self.settings.pwa_session_generation_path).write_text("0")
            self.settings.setup_api_key = "rotated"
            self.assertIsNone(music.verify_ticket(self.settings, ticket, 7))


if __name__ == "__main__":
    unittest.main()
