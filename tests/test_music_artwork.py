from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mutagen.flac import Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4Cover
from mutagen.wave import WAVE
from PIL import Image

from app.services import music_artwork as artwork
from app.services.music_library import track_json
from test_music_library import silent_wav


def png_bytes(size=(1000, 600), color="red"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


class MusicArtworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / ("a" * 32 + ".wav")
        self.path.write_bytes(silent_wav())

    def track(self):
        data = self.path.read_bytes()
        return SimpleNamespace(id=7, storage_name=self.path.name, sha256=hashlib.sha256(data).hexdigest(),
                               size=len(data), deleted=False, title="my_old_track", filename="my_old_track.wav",
                               artist="", album="", duration=5, mime="audio/wav", favorite=False)

    def with_apic(self, pictures):
        audio = WAVE(self.path)
        audio.add_tags()
        for description, picture_type, data in pictures:
            audio.tags.add(APIC(encoding=3, mime="image/png", type=picture_type, desc=description, data=data))
        audio.save()
        return self.track()

    def test_real_wav_apic_front_cover_jpeg_thumb_and_private_cache(self):
        track = self.with_apic([("back", 4, png_bytes(color="blue")), ("front", 3, png_bytes(color="red"))])
        path = artwork.artwork_thumbnail(self.root, track)
        self.assertIsNotNone(path)
        self.assertEqual(path.parent, self.root / ".artwork")
        self.assertEqual(path.name, track.sha256 + "-v1.jpg")
        with Image.open(path) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (512, 307))
            self.assertEqual(image.mode, "RGB")
            self.assertGreater(image.getpixel((50, 50))[0], 200)
            self.assertEqual(dict(image.getexif()), {})
        self.assertLess(path.stat().st_size, artwork.MAX_OUTPUT_BYTES)
        with patch.object(artwork.mutagen, "File", side_effect=AssertionError("cache must avoid reparsing")):
            self.assertEqual(artwork.artwork_thumbnail(self.root, track), path)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_no_embedded_artwork_returns_none_and_negative_cache(self):
        track = self.track()
        self.assertIsNone(artwork.artwork_thumbnail(self.root, track))
        cache = self.root / ".artwork"
        self.assertEqual([path.suffix for path in cache.iterdir()], [".none"])
        with patch.object(artwork.mutagen, "File", side_effect=AssertionError("negative cache must avoid reparsing")):
            self.assertIsNone(artwork.artwork_thumbnail(self.root, track))

    def test_flac_mp4_ogg_and_legacy_ogg_embedded_candidates(self):
        data = png_bytes((32, 16))
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, "image/png", data
        id3 = ID3()
        id3.add(APIC(encoding=3, mime="image/png", type=3, data=data))
        objects = [SimpleNamespace(pictures=[picture], tags={}), SimpleNamespace(tags=id3),
                   SimpleNamespace(tags={"covr": [MP4Cover(data, imageformat=MP4Cover.FORMAT_PNG)]}),
                   SimpleNamespace(tags={"metadata_block_picture": [base64.b64encode(picture.write()).decode()]}),
                   SimpleNamespace(tags={"coverart": [base64.b64encode(data).decode()]})]
        for audio in objects:
            with self.subTest(audio=type(audio)):
                self.assertEqual(list(artwork._embedded_candidates(audio)), [data])
                self.assertIsNotNone(artwork._jpeg_thumbnail(data))

    def test_real_flac_metadata_blocks_are_parsed_through_bounded_reader(self):
        # Minimal FLAC metadata fixture (not a playback/audio-decoder test).
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, "image/png", png_bytes((120, 80))
        stream_info = (4096).to_bytes(2, "big") * 2 + b"\0" * 6
        stream_info += ((44100 << 44) | (15 << 36) | 44100).to_bytes(8, "big") + b"\0" * 16
        cover = picture.write()
        self.path = self.root / ("b" * 32 + ".flac")
        self.path.write_bytes(b"fLaC\x00" + len(stream_info).to_bytes(3, "big") + stream_info
                              + b"\x86" + len(cover).to_bytes(3, "big") + cover)
        path = artwork.artwork_thumbnail(self.root, self.track())
        self.assertIsNotNone(path)
        with Image.open(path) as image:
            self.assertEqual(image.size, (120, 80))

    def test_external_url_corrupt_picture_and_invalid_base64_are_never_artwork(self):
        picture = Picture()
        picture.mime, picture.type, picture.data = "-->", 3, b"https://private.invalid/avatar"
        audio = SimpleNamespace(pictures=[picture], tags={"metadata_block_picture": ["not valid base64", "AAAA"],
                                                        "coverart": ["http://never-fetch.invalid"]})
        self.assertEqual(list(artwork._embedded_candidates(audio)), [])
        self.assertIsNone(artwork._jpeg_thumbnail(b"https://private.invalid/avatar"))
        self.assertIsNone(artwork._jpeg_thumbnail(b"<svg><image href='https://private.invalid'/></svg>"))

    def test_encoded_byte_pixel_and_candidate_limits(self):
        data = png_bytes((100, 100))
        with patch.object(artwork, "MAX_EMBEDDED_BYTES", 5):
            self.assertEqual(list(artwork._embedded_candidates(SimpleNamespace(tags={"covr": [data]}))), [])
            self.assertIsNone(artwork._jpeg_thumbnail(data))
        with patch.object(artwork, "MAX_IMAGE_PIXELS", 5000):
            self.assertIsNone(artwork._jpeg_thumbnail(data))
        candidates = list(artwork._embedded_candidates(SimpleNamespace(tags={"covr": [data] * 100})))
        self.assertEqual(len(candidates), artwork.MAX_CANDIDATES)

    def test_metadata_reader_caps_bytes_and_seek_operations(self):
        with self.path.open("rb") as stream, patch.object(artwork, "MAX_METADATA_BYTES", 128):
            reader = artwork._BoundedReader(stream)
            self.assertEqual(len(reader.read(128)), 128)
            with self.assertRaises(ValueError):
                reader.read(1)
        with self.path.open("rb") as stream:
            reader = artwork._BoundedReader(stream)
            reader.operations = 20_000
            with self.assertRaises(ValueError):
                reader.seek(0)

    def test_bad_cover_falls_back_to_next_embedded_cover_not_placeholder(self):
        track = self.with_apic([("broken-front", 3, b"not an image"), ("other", 0, png_bytes((24, 32), "green"))])
        path = artwork.artwork_thumbnail(self.root, track)
        self.assertIsNotNone(path)
        with Image.open(path) as image:
            self.assertEqual(image.size, (24, 32))

    def test_cached_artwork_not_returned_for_deleted_mismatched_or_unsafe_track(self):
        track = self.with_apic([("front", 3, png_bytes())])
        self.assertIsNotNone(artwork.artwork_thumbnail(self.root, track))
        track.deleted = True
        self.assertIsNone(artwork.artwork_thumbnail(self.root, track))
        track.deleted, track.size = False, track.size + 1
        self.assertIsNone(artwork.artwork_thumbnail(self.root, track))
        track = self.track()
        track.storage_name = "../outside.wav"
        self.assertIsNone(artwork.artwork_thumbnail(self.root, track))

    def test_cold_track_retains_positive_cached_cover_without_reading_audio(self):
        track = self.with_apic([("front", 3, png_bytes())])
        path = artwork.artwork_thumbnail(self.root, track)
        self.path.unlink()
        with patch.object(artwork.mutagen, "File", side_effect=AssertionError("cold audio cannot be parsed")):
            self.assertEqual(artwork.artwork_thumbnail(self.root, track), path)
        track.deleted = True
        self.assertIsNone(artwork.artwork_thumbnail(self.root, track))

    def test_cold_track_without_cover_does_not_parse_or_create_negative_cache(self):
        track = self.track()
        self.path.unlink()
        with patch.object(artwork.mutagen, "File", side_effect=AssertionError("cold audio cannot be parsed")):
            self.assertIsNone(artwork.artwork_thumbnail(self.root, track))
        self.assertFalse((self.root / ".artwork").exists())

    def test_cache_symlink_is_rejected_without_touching_target(self):
        track = self.track()
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.root / ".artwork").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is unavailable on this host")
        self.assertIsNone(artwork.artwork_thumbnail(self.root, track))
        self.assertEqual(list(outside.iterdir()), [])

    def test_track_json_humanizes_only_legacy_filename_fallback_and_adds_private_path(self):
        track = self.track()
        result = track_json(track)
        self.assertEqual(result["title"], "my old track")
        self.assertEqual(result["filename"], "my_old_track.wav")
        self.assertEqual(result["artwork_path"], "/api/mini/music/tracks/7/artwork")
        self.assertEqual(track.title, "my_old_track")  # Read-only migration, no DB mutation.
        track.title = "Artist_Title From Real Tags"
        self.assertEqual(track_json(track)["title"], track.title)


if __name__ == "__main__":
    unittest.main()
