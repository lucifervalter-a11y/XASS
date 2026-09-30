from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

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

    async def test_owner_can_store_clear_and_preserve_translation_and_transliteration(self):
        track, route = await self.prepared()
        lyrics_route = route.replace("/enrichment", "/lyrics")
        companion_route = lyrics_route + "/companions"
        self.assertEqual((await self.request("PUT", companion_route, headers={"x-test-owner": "guest"},
                                            json={"translation": "Hello"})).status_code, 403)
        saved = await self.request("PUT", companion_route, json={
            "translation": "  Hello\r\nworld  ", "translation_language": "en",
            "transliteration": "Privet mir", "transliteration_scheme": "user"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["companions"]["translation"],
                         {"text": "Hello\nworld", "language": "en", "source": "owner", "automatic": False})
        self.assertTrue(saved.json()["enrichment"]["lyrics_transliteration_available"])
        await self.request("PUT", lyrics_route, json={"text": "Новый текст", "source": "on_device_transcription"})
        shown = (await self.request("GET", lyrics_route)).json()["lyrics"]
        self.assertEqual(shown["companions"]["transliteration"]["text"], "Privet mir")
        cleared = await self.request("PUT", companion_route, json={"translation": ""})
        self.assertNotIn("translation", cleared.json()["companions"])
        self.assertIn("transliteration", cleared.json()["companions"])

    async def test_timed_lyrics_endpoint_aligns_companions_for_the_native_player_only_when_safe(self):
        track, route = await self.prepared()
        lyrics_route = route.replace("/enrichment", "/lyrics")
        source = "[00:01.00]Первая строка\n[00:03.00]Вторая строка"
        saved = await self.request("PUT", lyrics_route,
                                   json={"text": source, "source": "on_device_transcription"})
        self.assertEqual(saved.status_code, 200, saved.text)
        companions = await self.request("PUT", lyrics_route + "/companions", json={
            "translation": "First line\nSecond line", "translation_language": "en",
            "transliteration": "[00:01.00]Pervaya stroka\n[00:03.00]Vtoraya stroka",
            "transliteration_scheme": "user",
        })
        self.assertEqual(companions.status_code, 200, companions.text)

        timed = await self.request("GET", lyrics_route.replace("/lyrics", "/timed-lyrics"))
        self.assertEqual(timed.status_code, 200, timed.text)
        value = timed.json()["lyrics"]
        self.assertEqual([row["translation"] for row in value["lines"]], ["First line", "Second line"])
        self.assertEqual([row["pronunciation"] for row in value["lines"]],
                         ["Pervaya stroka", "Vtoraya stroka"])
        self.assertEqual(value["companions"]["translation"]["language"], "en")

        # A partial edit remains available to fix, but is not displayed beside
        # the wrong timed rows by the Swift client contract.
        await self.request("PUT", lyrics_route + "/companions", json={"translation": "Only one line"})
        mismatched = (await self.request("GET", lyrics_route.replace("/lyrics", "/timed-lyrics"))).json()["lyrics"]
        self.assertTrue(all("translation" not in row for row in mismatched["lines"]))
        self.assertEqual(mismatched["companions"]["translation"]["text"], "Only one line")
        self.assertEqual([row["pronunciation"] for row in mismatched["lines"]],
                         ["Pervaya stroka", "Vtoraya stroka"])

    async def test_owner_artwork_upload_is_authenticated_bounded_normalized_and_revisioned(self):
        track, route = await self.prepared()
        endpoint = route.replace("/enrichment", "/artwork")
        source = io.BytesIO()
        Image.new("RGBA", (900, 600), (15, 30, 60, 120)).save(source, format="PNG")
        body = source.getvalue()
        for headers, expected in (({}, 401), ({"x-test-owner": "guest"}, 403)):
            response = await self.request("PUT", endpoint, headers={**headers, "content-type": "image/png"}, content=body)
            self.assertEqual(response.status_code, expected)
        self.assertEqual((await self.request("PUT", endpoint, headers={**self.headers, "content-type": "image/svg+xml"},
                                             content=b"<svg/>")).status_code, 415)
        oversized = await self.request("PUT", endpoint, headers={**self.headers, "content-type": "image/png",
            "content-length": str(8 * 1024 * 1024 + 1)}, content=b"")
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual((await self.request("PUT", endpoint, headers={**self.headers, "content-type": "image/png"},
                                             content=b"not an image")).status_code, 400)
        response = await self.request("PUT", endpoint, headers={**self.headers, "content-type": "image/png"}, content=body)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["artwork"]["source"], "owner")
        self.assertFalse(value["artwork"]["automatic"])
        self.assertTrue(value["enrichment"]["artwork_available"])
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, track["id"])
            self.assertTrue(record.artwork_data.startswith(b"\xff\xd8\xff"))
            self.assertLessEqual(len(record.artwork_data), 512 * 1024)
            self.assertEqual(record.result["manual_artwork"]["source"], "owner")
            first_revision = record.revision
        again = await self.request("PUT", endpoint, headers={**self.headers, "content-type": "image/png"}, content=body)
        self.assertGreater(again.json()["artwork"]["revision"], first_revision)
        served = await self.request("GET", endpoint)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["content-type"], "image/jpeg")


if __name__ == "__main__":
    unittest.main()
