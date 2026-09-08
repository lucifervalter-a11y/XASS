from __future__ import annotations

import io
import unittest
from pathlib import Path
import zipfile

from mutagen.id3 import APIC
from mutagen.wave import WAVE
from PIL import Image

from app.music_models import MusicTrack
import test_music_api as fixtures
from test_music_library import silent_wav


class MusicUploadExtensionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MusicApiTests.asyncSetUp
    asyncTearDown = fixtures.MusicApiTests.asyncTearDown
    request = fixtures.MusicApiTests.request
    start = fixtures.MusicApiTests.start
    chunk = fixtures.MusicApiTests.chunk
    upload = fixtures.MusicApiTests.upload

    async def archive(self, entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        data = buffer.getvalue()
        upload_id = await self.start(data, "Моя музыка.zip")
        self.assertEqual((await self.chunk(upload_id, data)).status_code, 200)
        return upload_id, await self.request("POST", f"/api/mini/music/uploads/{upload_id}/finish")

    async def test_zip_order_incremental_import_and_idempotent_finish(self):
        upload_id, response = await self.archive([("01_первая.wav", silent_wav(5)), ("02_вторая.wav", silent_wav(6)), ("notes.txt", b"not music")])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual([item["title"] for item in result["tracks"]], ["01 первая", "02 вторая"])
        retry = await self.request("POST", f"/api/mini/music/uploads/{upload_id}/finish")
        self.assertEqual(retry.json(), result)
        self.assertFalse((Path(self.settings.music_root) / (upload_id + ".part")).exists())
        _, repeated = await self.archive([("first.wav", silent_wav(5)), ("new.wav", silent_wav(7))])
        self.assertEqual(repeated.json()["added"], 1)
        self.assertEqual(repeated.json()["duplicates"], 1)
        self.assertEqual(repeated.json()["tracks"][0]["id"], result["tracks"][0]["id"])
        self.assertEqual((await self.request("GET", "/api/mini/music/library")).json()["total"], 3)

    async def test_zip_traversal_rejected_before_any_track_added(self):
        _, result = await self.archive([("valid.wav", silent_wav()), ("../evil.wav", silent_wav(6))])
        self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual((await self.request("GET", "/api/mini/music/library")).json()["total"], 0)

    async def test_cold_audio_reupload_restores_same_identity_and_favorite(self):
        first = await self.upload()
        await self.request("PATCH", f"/api/mini/music/tracks/{first['id']}", json={"favorite": True})
        async with self.sessions() as session:
            track = await session.get(MusicTrack, first["id"])
            target = Path(self.settings.music_root) / track.storage_name
            target.unlink()  # This test's private fixture only, simulates verified cold eviction.
        restored = await self.upload(name="same_audio_again.wav")
        self.assertEqual(restored["id"], first["id"])
        self.assertTrue(restored["favorite"])
        self.assertEqual(target.read_bytes(), self.audio)

    async def test_pagination_preserves_playlist_order(self):
        one = await self.upload(silent_wav(5)); two = await self.upload(silent_wav(6)); three = await self.upload(silent_wav(7))
        ids = [two["id"], one["id"], three["id"]]
        playlist = (await self.request("POST", "/api/mini/music/playlists", json={"name": "Order", "track_ids": ids})).json()["playlist"]
        pages = []
        for offset in range(3):
            page = (await self.request("GET", "/api/mini/music/library", params={"playlist": playlist["id"], "limit": 1, "offset": offset})).json()
            self.assertEqual(page["total"], 3); self.assertEqual(page["has_more"], offset < 2)
            pages.extend(item["id"] for item in page["tracks"])
        self.assertEqual(pages, ids)

    async def test_real_artwork_authenticated_and_revoked_by_soft_delete(self):
        fixture = self.root / "cover.wav"
        fixture.write_bytes(self.audio)
        image = io.BytesIO(); Image.new("RGB", (600, 600), "navy").save(image, format="PNG")
        wave = WAVE(fixture); wave.add_tags(); wave.tags.add(APIC(mime="image/png", type=3, data=image.getvalue())); wave.save()
        track = await self.upload(fixture.read_bytes())
        path = track["artwork_path"]
        self.assertEqual((await self.client.get(path)).status_code, 401)
        response = await self.request("GET", path)
        self.assertEqual(response.status_code, 200, response.text[:100])
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        with Image.open(io.BytesIO(response.content)) as thumb:
            self.assertEqual(thumb.size, (512, 512))
        await self.request("DELETE", f"/api/mini/music/tracks/{track['id']}")
        self.assertEqual((await self.request("GET", path)).status_code, 404)
