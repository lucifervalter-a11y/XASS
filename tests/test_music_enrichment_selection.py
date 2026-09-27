"""A confirmed catalog ID survives time and transient provider failures."""
import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from app.music_models import MusicEnrichment, MusicTrack
from app.services.music_lyrics import empty_lyrics
import test_music_api as fixtures


class MusicEnrichmentSelectionTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.MusicApiTests.asyncTearDown
    request = fixtures.MusicApiTests.request
    upload = fixtures.MusicApiTests.upload
    start = fixtures.MusicApiTests.start
    chunk = fixtures.MusicApiTests.chunk

    async def asyncSetUp(self):
        await fixtures.MusicApiTests.asyncSetUp(self)
        for name in ("enrich_track", "enrich_confirmed_candidate", "fetch_artwork_thumbnail"):
            guard = patch("app.services.music_enrichment." + name,
                AsyncMock(side_effect=AssertionError("Unexpected network helper: " + name)))
            guard.start()
            self.addCleanup(guard.stop)

    async def prepared(self):
        from app.music_enrichment_api import fingerprint
        track = await self.upload()
        candidate = {"title": "Selected Song", "artist": "Selected Artist", "album": "Selected Album",
            "source": "lrclib", "source_id": 123, "duration": 180,
            "source_url": "https://lrclib.net/lyrics/123"}
        lyrics = {"text": "Previously selected fixture", "lines": [{"time": 1, "text": "Previously selected fixture"}],
            "synced": True, "source": "lrclib", "source_url": candidate["source_url"]}
        provenance = [{"source": "lrclib", "id": 123, "url": candidate["source_url"]}]
        async with self.sessions() as session:
            item = await session.get(MusicTrack, track["id"])
            item.title, item.artist, item.album, item.duration = candidate["title"], candidate["artist"], candidate["album"], 180.0
            session.add(MusicEnrichment(track_id=item.id, fingerprint=fingerprint(item), revision=2,
                original={"title": "Original title", "artist": "Original artist", "album": "Original album"},
                result={"status": "confirmed", "candidate": candidate, "lyrics": lyrics, "provenance": provenance,
                    "lookup_status": "matched"}, artwork_data=b"previous-cover", owner_lyrics={},
                checked_at=datetime.now(timezone.utc) - timedelta(days=60)))
            await session.commit()
        return track["id"], f"/api/mini/music/tracks/{track['id']}/enrichment", candidate, lyrics, provenance

    async def test_selected_record_survives_sixty_days_without_repeating_ambiguous_search(self):
        identity, route, candidate, lyrics, _ = await self.prepared()
        no_network = AsyncMock(side_effect=AssertionError("A saved selection must not auto-search"))
        with patch("app.services.music_enrichment.enrich_track", no_network), \
             patch("app.services.music_enrichment.enrich_confirmed_candidate", no_network):
            loaded = await self.request("GET", route.replace("/enrichment", "/lyrics"))
            response = await self.request("POST", route, json={})
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["lyrics"], {**lyrics, "status": "matched"})
        self.assertEqual(response.json()["enrichment"]["status"], "confirmed")
        self.assertEqual(response.json()["enrichment"]["candidate"], candidate)
        no_network.assert_not_awaited()
        async with self.sessions() as session:
            self.assertEqual((await session.get(MusicEnrichment, identity)).revision, 2)

    async def test_explicit_refresh_uses_selected_id_and_keeps_owner_metadata(self):
        identity, route, candidate, _, provenance = await self.prepared()
        updated = {"text": "Fresh selected fixture", "lines": [{"time": 2, "text": "Fresh selected fixture"}], "source": "lrclib", "synced": True}
        selected = AsyncMock(return_value={"status": "matched", "lyrics": updated, "provenance": provenance,
            "candidate": {**candidate, "title": "Do not silently rename owner selection"}})
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(side_effect=AssertionError("Do not search again"))), \
             patch("app.services.music_enrichment.enrich_confirmed_candidate", selected), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            response = await self.request("POST", route, json={"refresh": True})
        self.assertEqual(response.status_code, 200, response.text)
        selected.assert_awaited_once()
        self.assertEqual(selected.await_args.args[1], candidate)
        self.assertEqual(response.json()["enrichment"]["status"], "confirmed")
        self.assertEqual(response.json()["enrichment"]["lyrics"], updated)
        self.assertEqual(response.json()["track"]["title"], candidate["title"])
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, identity)
            self.assertEqual(record.revision, 3)
            self.assertEqual(record.artwork_data, b"previous-cover")

    async def test_temporary_failure_preserves_selected_lyrics_provenance_and_artwork(self):
        identity, route, candidate, lyrics, provenance = await self.prepared()
        for outcome in ("unavailable", "rate_limited", "timeout"):
            async with self.sessions() as session:
                (await session.get(MusicEnrichment, identity)).checked_at = datetime.now(timezone.utc) - timedelta(minutes=2)
                await session.commit()
            selected = AsyncMock(side_effect=TimeoutError) if outcome == "timeout" else AsyncMock(
                return_value={"status": outcome, "retry_after": 120, "lyrics": empty_lyrics()})
            with patch("app.services.music_enrichment.enrich_confirmed_candidate", selected):
                response = await self.request("POST", route, json={"refresh": True})
            self.assertEqual(response.status_code, 200, response.text)
            value = response.json()["enrichment"]
            self.assertEqual(value["status"], "confirmed")
            self.assertEqual(value["candidate"], candidate)
            self.assertEqual(value["lyrics"], lyrics)
            self.assertEqual(value["provenance"], provenance)
            self.assertTrue(value["using_cached_result"])
            self.assertEqual(value["lookup_status"], "unavailable" if outcome == "timeout" else outcome)
        async with self.sessions() as session:
            self.assertEqual((await session.get(MusicEnrichment, identity)).artwork_data, b"previous-cover")

    async def test_confirmed_provider_mismatch_clears_unverified_lyrics_not_the_owner_choice(self):
        identity, route, candidate, _, _ = await self.prepared()
        with patch("app.services.music_enrichment.enrich_confirmed_candidate", AsyncMock(return_value={
                "status": "candidate", "reason": "duration_mismatch", "lyrics": empty_lyrics()})):
            response = await self.request("POST", route, json={"refresh": True})
        value = response.json()["enrichment"]
        self.assertEqual(value["status"], "confirmed")
        self.assertEqual(value["candidate"], candidate)
        self.assertEqual(value["lyrics"]["text"], "")
        self.assertEqual(value["lookup_reason"], "duration_mismatch")
        self.assertEqual(response.json()["track"]["title"], candidate["title"])
        async with self.sessions() as session:
            self.assertIsNone((await session.get(MusicEnrichment, identity)).artwork_data)

    async def test_restore_during_selected_refresh_wins_revision_race(self):
        identity, route, _, _, _ = await self.prepared()
        entered, release = asyncio.Event(), asyncio.Event()

        async def lookup(*_, **_kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return {"status": "unavailable", "lyrics": empty_lyrics()}

        with patch("app.services.music_enrichment.enrich_confirmed_candidate", side_effect=lookup):
            task = asyncio.create_task(self.request("POST", route, json={"refresh": True}))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                restored = await asyncio.wait_for(self.request("POST", route + "/restore", json={}), 5)
                self.assertEqual(restored.status_code, 200, restored.text)
                release.set()
                completed = await asyncio.wait_for(task, 5)
                self.assertEqual(completed.json()["enrichment"]["status"], "changed")
                self.assertEqual(completed.json()["track"]["title"], "Original title")
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, identity)
            self.assertTrue(record.dismissed)
            self.assertIsNone(record.artwork_data)


if __name__ == "__main__":
    unittest.main()
