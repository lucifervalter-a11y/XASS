from pathlib import Path
import hashlib
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app.services import music_compatibility as compat
from app.services.music_library import issue_ticket, verify_ticket
from test_music_library import silent_wav


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / ("a" * 32 + ".wav")
        self.file.write_bytes(silent_wav())
        self.track = SimpleNamespace(storage_name=self.file.name, sha256=hashlib.sha256(self.file.read_bytes()).hexdigest(), mime="audio/wav")

    def test_actual_codec_not_extension_selects_conversion(self):
        with patch.object(compat, "probe_codec", return_value=("aac", 2)), patch.object(compat.shutil, "which", return_value=None):
            with self.assertRaises(HTTPException) as error:
                compat.prepare(self.root, self.track, ["pcm_s16le", "flac"])
            self.assertEqual(error.exception.status_code, 503)

    def test_supported_codec_preserves_original_bytes(self):
        with patch.object(compat, "probe_codec", return_value=("pcm_s16le", 2)):
            result = compat.prepare(self.root, self.track, ["pcm_s16le"])
        self.assertEqual(result["path"], self.file)
        self.assertEqual(result["variant"], "")
        self.assertEqual(result["sha256"], self.track.sha256)

    def test_untrusted_path_and_changed_source_rejected_before_subprocess(self):
        with patch.object(compat, "probe_codec") as probe:
            self.track.storage_name = "../../secret.wav"
            with self.assertRaises(ValueError):
                compat.prepare(self.root, self.track, ["flac"])
            self.track.storage_name = self.file.name
            self.track.sha256 = "b" * 64
            with self.assertRaises(HTTPException):
                compat.prepare(self.root, self.track, ["flac"])
            probe.assert_not_called()

    def test_job_limit_fails_bounded_without_queueing(self):
        compat._job.acquire()
        try:
            with patch.object(compat, "probe_codec", return_value=("aac", 2)):
                with self.assertRaises(HTTPException) as error:
                    compat.prepare(self.root, self.track, ["flac"])
            self.assertEqual(error.exception.status_code, 503)
        finally:
            compat._job.release()

    def test_variant_is_signed_and_credential_bound(self):
        settings = SimpleNamespace(bot_token="test", setup_api_key="fixture", pwa_session_generation_path=str(self.root / "generation"))
        token = issue_ticket(settings, 2, purpose="agent", binding="a" * 64, variant="flac")
        value = verify_ticket(settings, token, 2, purposes=("agent",))
        self.assertEqual((value["variant"], value["b"]), ("flac", "a" * 64))
        self.assertIsNone(verify_ticket(settings, token, 2, purposes=("listen",)))
        with self.assertRaises(ValueError):
            issue_ticket(settings, 2, purpose="agent", variant="../../evil")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
    def test_real_transcode_is_complete_cached_and_preserves_original(self):
        before = self.file.read_bytes()
        result = compat.prepare(self.root, self.track, ["flac"])
        self.assertEqual(compat.probe_codec(result["path"])[0], "flac")
        self.assertEqual(self.file.read_bytes(), before)
        again = compat.prepare(self.root, self.track, ["flac"])
        self.assertEqual(result["sha256"], again["sha256"])
        self.assertEqual(list((self.root / "compatible-audio").glob("*.flac")), [result["path"]])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
    def test_real_aac_alac_and_opus_prepare_for_flac_only_receiver(self):
        # Synthetic non-silent fixtures exercise the actual requested codecs,
        # not an extension rename or a mocked ffmpeg process.
        for index, (encoder, codec, suffix, mime) in enumerate([
            ("aac", "aac", "m4a", "audio/mp4"),
            ("alac", "alac", "m4a", "audio/mp4"),
            ("libopus", "opus", "ogg", "audio/ogg"),
        ], start=1):
            with self.subTest(codec=codec):
                source = self.root / (f"{index:032x}." + suffix)
                subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:a", encoder, str(source)], check=True, timeout=20,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                before = source.read_bytes()
                track = SimpleNamespace(storage_name=source.name,
                    sha256=hashlib.sha256(before).hexdigest(), mime=mime)
                actual, duration = compat.probe_codec(source)
                self.assertEqual(actual, codec)
                prepared = compat.prepare(self.root, track, ["flac"])
                converted, converted_duration = compat.probe_codec(prepared["path"])
                self.assertEqual(converted, "flac")
                self.assertLess(abs(duration - converted_duration), .1)
                self.assertNotEqual(source, prepared["path"])
                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(prepared["variant"], "flac")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
    def test_real_mp3_only_receiver_gets_complete_fallback(self):
        before = self.file.read_bytes()
        prepared = compat.prepare(self.root, self.track, ["mp3"])
        codec, duration = compat.probe_codec(prepared["path"])
        self.assertEqual(codec, "mp3")
        self.assertLess(abs(duration - 5), .5)
        self.assertEqual(prepared["mime"], "audio/mpeg")
        self.assertEqual(self.file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
