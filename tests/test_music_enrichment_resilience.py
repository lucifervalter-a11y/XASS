from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.music_enrichment_api import catalog_lookup
from app.music_models import MusicEnrichment, MusicTrack
import test_music_api as fixtures
import test_music_enrichment_api as enrichment_fixtures


class MusicEnrichmentResilienceTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.MusicApiTests.asyncTearDown
    request = fixtures.MusicApiTests.request
    start = fixtures.MusicApiTests.start
    chunk = fixtures.MusicApiTests.chunk
    upload = fixtures.MusicApiTests.upload
    prepared = enrichment_fixtures.MusicEnrichmentApiTests.prepared
    matched = enrichment_fixtures.MusicEnrichmentApiTests.matched
    expire_lookup_cache = enrichment_fixtures.MusicEnrichmentApiTests.expire_lookup_cache
    catalog_while = enrichment_fixtures.MusicEnrichmentApiTests.catalog_while

    async def asyncSetUp(self):
        await fixtures.MusicApiTests.asyncSetUp(self)
        for name in ("enrich_track", "enrich_confirmed_candidate", "fetch_artwork_thumbnail"):
            guard = patch("app.services.music_enrichment." + name, AsyncMock(side_effect=AssertionError("Unexpected live provider call")))
            guard.start()
            self.addCleanup(guard.stop)

    async def test_explicit_refresh_passes_cache_bypass_only_after_api_sixty_second_gate(self):
        track, route = await self.prepared()
        lookup = AsyncMock(return_value=self.matched())
        with patch("app.services.music_enrichment.enrich_track", lookup), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            await self.request("POST", route, json={})
            self.assertNotIn("refresh", lookup.await_args.kwargs)
            self.assertIn("deadline", lookup.await_args.kwargs)
            await self.request("POST", route, json={"refresh": True})
            self.assertEqual(lookup.await_count, 1)
            await self.expire_lookup_cache(track["id"])
            response = await self.request("POST", route, json={"refresh": True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(lookup.await_count, 2)
            self.assertIs(lookup.await_args.kwargs["refresh"], True)

    async def test_cover_timeout_does_not_erase_matched_metadata_lyrics_or_provenance(self):
        track, route = await self.prepared()
        matched = {**self.matched(), "artwork": {"status": "candidate", "source": "coverartarchive"}}
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=matched)), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(side_effect=TimeoutError)):
            result = await self.request("POST", route, json={})
        self.assertEqual(result.status_code, 200, result.text)
        value = result.json()["enrichment"]
        self.assertEqual(value["status"], "matched")
        self.assertEqual(value["lyrics"], matched["lyrics"])
        self.assertEqual(value["provenance"], matched["provenance"])
        self.assertEqual(value["artwork_reason"], "artwork_unavailable")
        self.assertIs(value["artwork_available"], False)
        result = await self.request("GET", f"/api/mini/music/tracks/{track['id']}/lyrics")
        self.assertEqual(result.json()["lyrics"]["text"], "Fixture")
        self.assertEqual(result.json()["track"]["title"], "Fixture Song")

    async def test_selected_confirmation_keeps_verified_lyrics_when_cover_io_fails(self):
        track, route = await self.prepared()
        candidate = {"title": "Fixture Song", "artist": "Fixture Artist", "album": "Owner album", "duration": 180,
            "source": "lrclib", "source_id": 123, "source_url": "https://lrclib.net/lyrics/123"}
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value={"status": "ambiguous", "candidates": [candidate]})):
            found = (await self.request("POST", route, json={})).json()
        with patch("app.services.music_enrichment.enrich_confirmed_candidate", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(side_effect=OSError)):
            result = await self.request("POST", route + "/confirm", json={"index": 0, "candidate_token": found["enrichment"]["candidate_token"]})
        self.assertEqual(result.status_code, 200, result.text)
        value = result.json()["enrichment"]
        self.assertEqual(value["candidate"], candidate)
        self.assertEqual(value["lookup_status"], "matched")
        self.assertTrue(value["lyrics"]["synced"])
        self.assertFalse(value["artwork_available"])
        self.assertEqual((await self.request("GET", f"/api/mini/music/tracks/{track['id']}/lyrics")).json()["lyrics"]["source"], "lrclib")

    async def test_whole_lookup_deadline_stops_optional_cover_but_returns_verified_result(self):
        async def blocked(_):
            await asyncio.Event().wait()
        with patch("app.music_enrichment_api.CATALOG_BUDGET_SECONDS", .03), \
             patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", side_effect=blocked):
            result, artwork = await asyncio.wait_for(catalog_lookup(SimpleNamespace()), .5)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["lyrics"]["source"], "lrclib")
        self.assertIsNone(artwork)

    async def test_artwork_available_reports_persisted_thumbnail_and_restore_clears_it(self):
        track, route = await self.prepared()
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=b"private-thumb")):
            created = await self.request("POST", route, json={})
        self.assertTrue(created.json()["enrichment"]["artwork_available"])
        self.assertTrue((await self.request("GET", route)).json()["enrichment"]["artwork_available"])
        restored = await self.request("POST", route + "/restore", json={})
        self.assertFalse(restored.json()["enrichment"]["artwork_available"])
        async with self.sessions() as session:
            self.assertIsNone((await session.get(MusicEnrichment, track["id"])).artwork_data)

    async def test_matched_cache_survives_refresh_outage_and_retries_after_short_ttl(self):
        track, route = await self.prepared()
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=b"verified-thumb")):
            await self.request("POST", route, json={})
        await self.expire_lookup_cache(track["id"])
        for status, retry_after in (("unavailable", 60), ("rate_limited", 120)):
            failure = {"status": status, "reason": "catalog_unavailable", "retry_after": retry_after}
            with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=failure)) as lookup:
                refreshed = await self.request("POST", route, json={"refresh": True})
                value = refreshed.json()["enrichment"]
                self.assertEqual(value["status"], "matched")
                self.assertEqual(value["lookup_status"], status)
                self.assertTrue(value["using_cached_result"])
                self.assertEqual(value["lyrics"], self.matched()["lyrics"])
                self.assertTrue(value["artwork_available"])
                await self.request("POST", route, json={})
                self.assertEqual(lookup.await_count, 1)
            async with self.sessions() as session:
                record = await session.get(MusicEnrichment, track["id"])
                self.assertEqual(record.artwork_data, b"verified-thumb")
                record.checked_at = datetime.now(timezone.utc) - timedelta(seconds=retry_after + 1)
                await session.commit()
            with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())) as lookup, \
                 patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=b"verified-thumb")):
                recovered = await self.request("POST", route, json={})
                self.assertEqual(lookup.await_count, 1)
                self.assertNotIn("using_cached_result", recovered.json()["enrichment"])
            await self.expire_lookup_cache(track["id"])

    async def test_partial_cover_outage_keeps_prior_thumbnail_and_retries_after_sixty_seconds(self):
        track, route = await self.prepared()
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=b"verified-thumb")):
            await self.request("POST", route, json={})
        await self.expire_lookup_cache(track["id"])
        partial = {**self.matched(), "artwork_reason": "catalog_unavailable"}
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=partial)), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            response = await self.request("POST", route, json={"refresh": True})
        self.assertTrue(response.json()["enrichment"]["artwork_available"])
        await self.expire_lookup_cache(track["id"])
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())) as lookup, \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=b"new-thumb")):
            response = await self.request("POST", route, json={})
        self.assertEqual(lookup.await_count, 1)
        self.assertNotIn("artwork_reason", response.json()["enrichment"])

    async def test_changed_fingerprint_and_explicit_mismatch_do_not_keep_old_lyrics(self):
        track, route = await self.prepared()
        for changed in (False, True):
            with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
                 patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
                await self.request("POST", route, json={"refresh": True})
            await self.expire_lookup_cache(track["id"])
            if changed:
                async with self.sessions() as session:
                    row = await session.get(MusicTrack, track["id"])
                    row.duration += 60
                    await session.commit()
            failure = {"status": "unavailable" if changed else "not_found", "lyrics": {"text": ""}}
            with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=failure)):
                result = await self.request("POST", route, json={"refresh": True})
            value = result.json()["enrichment"]
            self.assertNotIn("using_cached_result", value)
            self.assertFalse(value["lyrics"]["text"])
            await self.expire_lookup_cache(track["id"])

    async def test_lyrics_source_is_owner_only_validated_and_requires_saved_transcript(self):
        track, route = await self.prepared()
        endpoint = route.replace("/enrichment", "/lyrics/source")
        for headers, expected in (({}, 401), ({"x-test-owner": "guest"}, 403)):
            self.assertEqual((await self.request("PATCH", endpoint, headers=headers, json={"source": "catalog"})).status_code, expected)
        self.assertEqual((await self.request("PATCH", endpoint, json={"source": "unknown"})).status_code, 422)
        self.assertEqual((await self.request("PATCH", endpoint, json={"source": "catalog"})).status_code, 409)
        self.assertEqual((await self.request("PATCH", "/api/mini/music/tracks/999/lyrics/source", json={"source": "catalog"})).status_code, 404)

    async def test_lyrics_source_switch_is_reversible_idempotent_and_new_transcript_activates(self):
        track, route = await self.prepared()
        lyrics_route = route.replace("/enrichment", "/lyrics")
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            await self.request("POST", route, json={})
        body = {"text": "Owner draft", "source": "on_device_transcription"}
        await self.request("PUT", lyrics_route, json=body)
        original = (await self.request("GET", lyrics_route)).json()["lyrics"]
        response = await self.request("PATCH", lyrics_route + "/source", json={"source": "catalog"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["track"]["id"], track["id"])
        self.assertTrue(response.json()["enrichment"]["owner_lyrics_available"])
        self.assertFalse(response.json()["enrichment"]["owner_lyrics_enabled"])
        self.assertEqual((await self.request("GET", lyrics_route)).json()["lyrics"]["source"], "lrclib")
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, track["id"])
            revision = record.revision
            self.assertEqual({k: v for k, v in record.owner_lyrics.items() if k != "disabled"}, original)
        await self.request("PATCH", lyrics_route + "/source", json={"source": "catalog"})
        async with self.sessions() as session:
            self.assertEqual((await session.get(MusicEnrichment, track["id"])).revision, revision)
        response = await self.request("PATCH", lyrics_route + "/source", json={"source": "owner"})
        self.assertTrue(response.json()["enrichment"]["owner_lyrics_enabled"])
        self.assertEqual((await self.request("GET", lyrics_route)).json()["lyrics"]["text"], "Owner draft")
        await self.request("PATCH", lyrics_route + "/source", json={"source": "catalog"})
        await self.request("PUT", lyrics_route, json={**body, "text": "New draft"})
        self.assertEqual((await self.request("GET", lyrics_route)).json()["lyrics"]["text"], "New draft")
        self.assertTrue((await self.request("GET", route)).json()["enrichment"]["owner_lyrics_enabled"])

    async def test_source_preference_invalidates_pending_catalog_receipt_without_losing_text(self):
        track, route = await self.prepared()
        lyrics_route = route.replace("/enrichment", "/lyrics")
        await self.request("PUT", lyrics_route, json={"text": "Owner draft", "source": "on_device_transcription"})
        await self.expire_lookup_cache(track["id"])
        result = await self.catalog_while(route,
            lambda: self.request("PATCH", lyrics_route + "/source", json={"source": "catalog"}), refresh=True)
        self.assertEqual(result["enrichment"]["status"], "changed")
        async with self.sessions() as session:
            saved = await session.get(MusicEnrichment, track["id"])
            self.assertEqual(saved.owner_lyrics["text"], "Owner draft")
            self.assertTrue(saved.owner_lyrics["disabled"])


if __name__ == "__main__":
    unittest.main()
