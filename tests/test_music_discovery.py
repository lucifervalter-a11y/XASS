from __future__ import annotations

import unittest

import httpx

from app.services.music_card import discover_music_catalog


class MusicDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_catalog_results_are_ranked_deduped_and_metadata_only(self):
        rows = [
            {"trackId": 2, "trackName": "Other", "artistName": "Someone", "collectionName": "X",
             "previewUrl": "https://audio.example/protected.m4a"},
            {"trackId": 1, "trackName": "Midnight City", "artistName": "M83", "collectionName": "Hurry Up",
             "trackTimeMillis": 244000, "artworkUrl100": "https://is1-ssl.mzstatic.com/image/100x100bb.jpg",
             "trackViewUrl": "https://music.apple.com/us/song/midnight-city/1",
             "previewUrl": "https://audio.example/protected.m4a"},
            {"trackId": 3, "trackName": "Midnight City", "artistName": "M83", "collectionName": "Hurry Up"},
        ]

        def handler(request):
            self.assertEqual(request.url.host, "itunes.apple.com")
            self.assertEqual(request.headers.get("accept-encoding"), "identity")
            return httpx.Response(200, json={"resultCount": len(rows), "results": rows})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            value = await discover_music_catalog("M83 - Midnight City", client=client)
        self.assertEqual(value["items"][0]["title"], "Midnight City")
        self.assertEqual(len([row for row in value["items"] if row["title"] == "Midnight City"]), 1)
        self.assertFalse(value["audio_import_supported"])
        self.assertNotIn("preview_url", value["items"][0])
        self.assertNotIn("audio.example", str(value))
        self.assertIn("1200x1200bb.jpg", value["items"][0]["artwork_url"])
        self.assertIn("VK", value["items"][0]["search_links"])

    async def test_oversized_or_untrusted_urls_are_not_returned(self):
        row = {"trackId": 1, "trackName": "Song", "artistName": "Artist", "collectionName": "Album",
               "artworkUrl100": "https://evil.example/100x100bb.jpg",
               "trackViewUrl": "https://evil.example/song"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"results": [row]}))) as client:
            value = await discover_music_catalog("Artist Song", client=client)
        self.assertEqual(value["items"][0]["artwork_url"], "")
        self.assertEqual(value["items"][0]["catalog_url"], "")

        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"x" * (512 * 1024 + 1)))) as client:
            value = await discover_music_catalog("Artist Song", client=client)
        self.assertEqual(value["items"], [])


if __name__ == "__main__":
    unittest.main()
