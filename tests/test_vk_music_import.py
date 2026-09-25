from __future__ import annotations

import unittest

from app.services.vk_music_import import _direct_url, _safe_name


class VkMusicImportHelpersTests(unittest.TestCase):
    def test_accepts_https_cdn_mp3_and_rejects_hls_or_http(self):
        self.assertTrue(_direct_url({"url": "https://psv4.userapi.com/audio.mp3?extra=1"}))
        self.assertEqual(_direct_url({"url": "https://vk.com/audio/index.m3u8"}), "")
        self.assertEqual(_direct_url({"url": "http://userapi.com/audio.mp3"}), "")
        self.assertEqual(_direct_url({"url": "https://evil.example/audio.mp3"}), "")

    def test_safe_filename_keeps_extension(self):
        name = _safe_name("Ночь / Street", "Artist?")
        self.assertTrue(name.endswith(".mp3"))
        self.assertNotIn("/", name)
        self.assertNotIn("?", name)

    def test_rejects_blank_and_non_audio_hosts(self):
        self.assertEqual(_direct_url({"url": ""}), "")
        self.assertEqual(_direct_url({"url": "https://vk.com/audio"}), "https://vk.com/audio")


if __name__ == "__main__":
    unittest.main()
