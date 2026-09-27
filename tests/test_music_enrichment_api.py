import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.music_models import MusicEnrichment, MusicTrack
from app.services.music_lyrics import empty_lyrics
from test_music_api import MusicApiTests


class MusicEnrichmentApiTests(MusicApiTests):
    async def prepared(self):
        track = await self.upload()
        async with self.sessions() as session:
            row = await session.get(MusicTrack, track["id"])
            row.title, row.artist, row.album, row.duration = "fixture song", "Fixture artist", "Owner album", 180
            await session.commit()
        return track, f"/api/mini/music/tracks/{track['id']}/enrichment"

    def matched(self):
        return {"status": "matched", "candidate": {"title": "Fixture Song", "artist": "Fixture Artist", "album": ""},
            "lyrics": {"text": "Fixture", "lines": [{"time": 2, "text": "Fixture"}], "source": "lrclib", "synced": True},
            "provenance": [{"source": "lrclib", "url": "https://lrclib.net/lyrics/123"}], "artwork": {}}

    async def expire_lookup_cache(self, track_id):
        # Exercise the explicit refresh path without waiting for its rate limit.
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, track_id)
            record.checked_at = datetime.now(timezone.utc) - timedelta(minutes=2)
            await session.commit()

    async def catalog_while(self, route, mutation, *, result=None, refresh=False):
        """A separate owner request commits while the mocked network is pending."""
        entered, release = asyncio.Event(), asyncio.Event()

        async def lookup(_, **_kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return self.matched() if result is None else result

        with patch("app.services.music_enrichment.enrich_track", side_effect=lookup), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            task = asyncio.create_task(self.request("POST", route, json={"refresh": refresh}))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                changed = await asyncio.wait_for(mutation(), timeout=5)
                self.assertEqual(changed.status_code, 200, changed.text)
                release.set()
                completed = await asyncio.wait_for(task, timeout=5)
                self.assertEqual(completed.status_code, 200, completed.text)
                return completed.json()
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_enrichment_auth_for_read_write_restore_confirm_transcript(self):
        for method, route, data in [("GET", "/enrichment", {}), ("POST", "/enrichment", {}),
            ("POST", "/enrichment/restore", {}), ("POST", "/enrichment/confirm", {"index": 0}),
            ("PUT", "/lyrics", {"text": "fixture", "source": "on_device_transcription"})]:
            for headers, status in [({}, 401), ({"x-test-owner": "guest"}, 403)]:
                response = await self.request(method, "/api/mini/music/tracks/1" + route, headers=headers, json=data)
                self.assertEqual(response.status_code, status)

    async def test_metadata_lyrics_art_cached_and_reversible_original_audio_unchanged(self):
        track, route = await self.prepared()
        before = {p.name: p.read_bytes() for p in (self.root / "music").glob("*.wav")}
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())) as lookup, \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=b"jpeg-fixture")):
            response = await self.request("POST", route, json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["track"]["title"], "Fixture Song")
            self.assertEqual(response.json()["track"]["album"], "Owner album")
            self.assertTrue(response.json()["enrichment"]["can_restore"])
            await self.request("POST", route, json={"refresh": True})
            text = await self.request("GET", route.replace("/enrichment", "/lyrics"))
            self.assertTrue(text.json()["lyrics"]["synced"])
            self.assertEqual(lookup.await_count, 1)
            image = await self.request("GET", route.replace("/enrichment", "/artwork"))
            self.assertEqual(image.content, b"jpeg-fixture")
            self.assertEqual(image.headers["cache-control"], "private, no-store")
            restored = await self.request("POST", route + "/restore", json={})
            self.assertEqual(restored.json()["track"]["title"], "fixture song")
            repeated = await self.request("POST", route, json={})
            self.assertEqual(repeated.json()["enrichment"]["status"], "disabled")
            self.assertEqual(lookup.await_count, 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / "music").glob("*.wav")})

    async def test_ambiguous_candidate_requires_owner_confirm_and_never_uses_full_song_timing(self):
        _, route = await self.prepared()
        value = {"status": "candidate", "candidates": [{"title": "Another title", "artist": "Another artist", "album": ""}], "lyrics": empty_lyrics()}
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=value)):
            response = await self.request("POST", route, json={})
        self.assertEqual(response.json()["track"]["title"], "fixture song")
        token = response.json()["enrichment"]["candidate_token"]
        self.assertEqual((await self.request("POST", route + "/confirm", json={"index": 2, "candidate_token": token})).status_code, 409)
        confirmed = await self.request("POST", route + "/confirm", json={"index": 0, "candidate_token": token})
        self.assertEqual(confirmed.json()["track"]["title"], "Another title")
        self.assertFalse(confirmed.json()["enrichment"]["lyrics"]["synced"])

    async def test_manual_edit_wins_and_cannot_be_overwritten_by_restore(self):
        _, route = await self.prepared()
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            await self.request("POST", route, json={})
        await self.request("PATCH", route.replace("/enrichment", ""), json={"title": "My edit"})
        self.assertEqual((await self.request("POST", route + "/restore", json={})).status_code, 409)
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(side_effect=AssertionError("Do not override edit"))):
            response = await self.request("POST", route, json={})
        self.assertEqual(response.json()["track"]["title"], "My edit")

    async def test_edit_while_catalog_request_runs_prevents_stale_apply(self):
        track, route = await self.prepared()
        async def change(_, **_kwargs):
            async with self.sessions() as session:
                row = await session.get(MusicTrack, track["id"])
                row.title = "Concurrent edit"
                await session.commit()
            return self.matched()
        with patch("app.services.music_enrichment.enrich_track", side_effect=change), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            result = await self.request("POST", route, json={})
        self.assertEqual(result.json()["enrichment"]["status"], "changed")
        self.assertEqual(result.json()["track"]["title"], "Concurrent edit")

    async def test_same_value_manual_edit_during_first_lookup_rejects_network_result(self):
        track, route = await self.prepared()
        result = await self.catalog_while(route, lambda: self.request("PATCH", route.replace("/enrichment", ""),
            json={"title": "fixture song", "artist": "Fixture artist", "album": "Owner album"}))
        self.assertEqual(result["enrichment"]["status"], "changed")
        self.assertEqual(result["track"]["title"], "fixture song")
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, track["id"])
            self.assertTrue(record.dismissed)
            self.assertGreater(record.revision, 0)
            self.assertEqual(record.result, {})

    async def test_same_value_edit_during_refresh_preserves_manual_dismissal(self):
        track, route = await self.prepared()
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=b"fixture-cover")):
            initial = await self.request("POST", route, json={})
        self.assertEqual(initial.status_code, 200, initial.text)
        await self.expire_lookup_cache(track["id"])
        late = self.matched()
        late["candidate"]["title"] = "Late catalog revision"
        result = await self.catalog_while(route, lambda: self.request("PATCH", route.replace("/enrichment", ""),
            json={"title": "Fixture Song"}), result=late, refresh=True)
        self.assertEqual(result["enrichment"]["status"], "changed")
        self.assertEqual(result["track"]["title"], "Fixture Song")
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, track["id"])
            self.assertTrue(record.dismissed)
            self.assertIsNone(record.artwork_data)

    async def test_dismiss_unchanged_candidate_during_refresh_cannot_be_reenabled(self):
        track, route = await self.prepared()
        candidate = {"status": "candidate", "candidates": [{"title": "Suggestion", "artist": "Artist", "album": ""}],
                     "lyrics": empty_lyrics()}
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=candidate)):
            initial = await self.request("POST", route, json={})
        self.assertEqual(initial.status_code, 200, initial.text)
        await self.expire_lookup_cache(track["id"])
        result = await self.catalog_while(route, lambda: self.request("POST", route + "/restore", json={}), refresh=True)
        # Identity never changed: only the persisted generation detects this dismissal.
        self.assertEqual(result["enrichment"]["status"], "changed")
        self.assertEqual(result["track"]["title"], "fixture song")
        final = await self.request("GET", route)
        self.assertEqual(final.json()["enrichment"]["status"], "disabled")

    async def test_restore_during_refresh_keeps_original_metadata_and_removes_cover(self):
        track, route = await self.prepared()
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=b"fixture-cover")):
            initial = await self.request("POST", route, json={})
        self.assertEqual(initial.status_code, 200, initial.text)
        await self.expire_lookup_cache(track["id"])
        result = await self.catalog_while(route, lambda: self.request("POST", route + "/restore", json={}), refresh=True)
        self.assertEqual(result["enrichment"]["status"], "changed")
        self.assertEqual(result["track"]["title"], "fixture song")
        self.assertEqual(result["track"]["artist"], "Fixture artist")
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, track["id"])
            self.assertTrue(record.dismissed)
            self.assertIsNone(record.artwork_data)

    async def test_candidate_token_cannot_confirm_a_newer_result_even_with_identical_candidates(self):
        track, route = await self.prepared()
        candidate = {"status": "candidate", "candidates": [{"title": "Suggestion", "artist": "Artist", "album": ""}],
                     "lyrics": empty_lyrics()}
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=candidate)) as lookup:
            first = await self.request("POST", route, json={})
            self.assertEqual(first.status_code, 200, first.text)
            old_token = first.json()["enrichment"]["candidate_token"]
            await self.expire_lookup_cache(track["id"])
            refreshed = await self.request("POST", route, json={"refresh": True})
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            new_token = refreshed.json()["enrichment"]["candidate_token"]
            self.assertEqual(lookup.await_count, 2)
        self.assertNotEqual(old_token, new_token, "A lookup generation, not only result contents, binds confirmation")
        stale = await self.request("POST", route + "/confirm", json={"index": 0, "candidate_token": old_token})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual((await self.request("GET", route)).json()["track"]["title"], "fixture song")
        confirmed = await self.request("POST", route + "/confirm", json={"index": 0, "candidate_token": new_token})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["track"]["title"], "Suggestion")

    async def test_confirmed_lrclib_candidate_persists_selected_timed_lyrics_for_get(self):
        track, route = await self.prepared()
        candidate = {"title": "Selected Fixture", "artist": "Selected Artist", "album": "Selected Album",
                     "duration": 180, "source": "lrclib", "source_id": 456,
                     "source_url": "https://lrclib.net/lyrics/456"}
        alternative = {**candidate, "title": "Other Fixture", "source_id": 123,
                       "source_url": "https://lrclib.net/lyrics/123"}
        ambiguous = {"status": "ambiguous", "candidates": [alternative, candidate], "lyrics": empty_lyrics()}
        selected_lyrics = {"text": "Selected fixture line", "lines": [{"time": 12.5, "text": "Selected fixture line"}],
                           "source": "lrclib", "source_url": candidate["source_url"], "synced": True}
        selected = {"status": "matched", "lyrics": selected_lyrics, "artwork": {},
                    "provenance": [{"source": "lrclib", "id": 456, "url": candidate["source_url"]}]}
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=ambiguous)):
            found = await self.request("POST", route, json={})
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json()["track"]["title"], "fixture song")
        token = found.json()["enrichment"]["candidate_token"]
        with patch("app.services.music_enrichment.enrich_confirmed_candidate", AsyncMock(return_value=selected)) as lookup, \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            confirmed = await self.request("POST", route + "/confirm", json={"index": 1, "candidate_token": token})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        lookup.assert_awaited_once()
        snapshot, actual_candidate = lookup.await_args.args
        self.assertEqual(snapshot.id, track["id"])
        self.assertEqual(snapshot.title, "fixture song")
        self.assertEqual(actual_candidate, candidate)
        self.assertEqual(confirmed.json()["track"]["title"], "Selected Fixture")
        self.assertEqual(confirmed.json()["enrichment"]["lyrics"], selected_lyrics)
        self.assertEqual(confirmed.json()["enrichment"]["lookup_status"], "matched")
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(side_effect=AssertionError("Use selected cache"))):
            loaded = await self.request("GET", route.replace("/enrichment", "/lyrics"))
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.headers["cache-control"], "private, no-store")
        self.assertEqual(loaded.json()["lyrics"], {**selected_lyrics, "status": "matched"})
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, track["id"])
            self.assertEqual(record.result["status"], "confirmed")
            self.assertEqual(record.result["lyrics"], selected_lyrics)
            self.assertNotIn("candidates", record.result)

    async def test_concurrent_restore_or_manual_edit_during_selected_fetch_rejects_confirmation(self):
        track, route = await self.prepared()
        candidate = {"title": "Selected Fixture", "artist": "Selected Artist", "album": "Selected Album",
                     "duration": 180, "source": "lrclib", "source_id": 456,
                     "source_url": "https://lrclib.net/lyrics/456"}
        candidates = {"status": "candidate", "candidates": [candidate], "lyrics": empty_lyrics()}
        for mutation in ("restore", "manual"):
            with self.subTest(mutation=mutation):
                if mutation == "manual":
                    await self.expire_lookup_cache(track["id"])
                with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=candidates)):
                    found = await self.request("POST", route, json={"refresh": True})
                self.assertEqual(found.status_code, 200, found.text)
                token = found.json()["enrichment"]["candidate_token"]
                entered, release = asyncio.Event(), asyncio.Event()

                async def selected_lookup(_, selected_candidate, **_kwargs):
                    self.assertEqual(selected_candidate, candidate)
                    entered.set()
                    await asyncio.wait_for(release.wait(), timeout=5)
                    return self.matched()

                with patch("app.services.music_enrichment.enrich_confirmed_candidate", side_effect=selected_lookup), \
                     patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=b"late-cover")):
                    task = asyncio.create_task(self.request("POST", route + "/confirm",
                        json={"index": 0, "candidate_token": token}))
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=5)
                        if mutation == "restore":
                            changed = await asyncio.wait_for(self.request("POST", route + "/restore", json={}), timeout=5)
                            expected_title = "fixture song"
                        else:
                            changed = await asyncio.wait_for(self.request("PATCH", route.replace("/enrichment", ""),
                                json={"title": "Manual wins"}), timeout=5)
                            expected_title = "Manual wins"
                        self.assertEqual(changed.status_code, 200, changed.text)
                        release.set()
                        stale = await asyncio.wait_for(task, timeout=5)
                        self.assertEqual(stale.status_code, 409, stale.text)
                    finally:
                        release.set()
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                current = await self.request("GET", route)
                self.assertEqual(current.json()["track"]["title"], expected_title)
                self.assertEqual(current.json()["enrichment"]["status"], "disabled")
                async with self.sessions() as session:
                    record = await session.get(MusicEnrichment, track["id"])
                    self.assertTrue(record.dismissed)
                    self.assertIsNone(record.artwork_data)
                    self.assertFalse(record.result["lyrics"]["synced"])

    async def test_manual_edit_refresh_then_restore_returns_manual_baseline_not_upload_tags(self):
        track, route = await self.prepared()
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            initial = await self.request("POST", route, json={})
            self.assertEqual(initial.status_code, 200, initial.text)
            manual = {"title": "My manual title", "artist": "My manual artist", "album": "My manual album"}
            edited = await self.request("PATCH", route.replace("/enrichment", ""), json=manual)
            self.assertEqual(edited.status_code, 200, edited.text)
            await self.expire_lookup_cache(track["id"])
            refreshed = await self.request("POST", route, json={"refresh": True})
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertEqual(refreshed.json()["track"]["title"], "Fixture Song")
        restored = await self.request("POST", route + "/restore", json={})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual({key: restored.json()["track"][key] for key in manual}, manual)

    async def test_known_cut_hint_survives_confirmed_clean_title_and_later_refresh(self):
        track, route = await self.prepared()
        async with self.sessions() as session:
            row = await session.get(MusicTrack, track["id"])
            row.title, row.duration = "fixture song cut99sec", 99
            await session.commit()
        seen_cuts = []
        selected_cuts = []
        candidate = {"title": "Clean catalog title", "artist": "Artist", "album": "", "duration": 180,
                     "source": "lrclib", "source_id": 123, "source_url": "https://lrclib.net/lyrics/123"}

        async def lookup(snapshot, **_kwargs):
            seen_cuts.append(snapshot.is_excerpt)
            return {"status": "candidate", "candidates": [candidate], "lyrics": empty_lyrics()}

        async def selected_lookup(snapshot, selected, **_kwargs):
            self.assertEqual(selected, candidate)
            selected_cuts.append((snapshot.title, snapshot.is_excerpt))
            return {"status": "candidate", "reason": "duration_mismatch", "candidate": selected,
                    "candidates": [selected], "lyrics": empty_lyrics()}

        with patch("app.services.music_enrichment.enrich_track", side_effect=lookup), \
             patch("app.services.music_enrichment.enrich_confirmed_candidate", side_effect=selected_lookup), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            first = await self.request("POST", route, json={})
            self.assertEqual(first.status_code, 200, first.text)
            token = first.json()["enrichment"]["candidate_token"]
            confirmed = await self.request("POST", route + "/confirm", json={"index": 0, "candidate_token": token})
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertEqual(confirmed.json()["track"]["title"], "Clean catalog title")
            await self.expire_lookup_cache(track["id"])
            refreshed = await self.request("POST", route, json={"refresh": True})
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(seen_cuts, [True], "A confirmed selection must not return to general discovery")
        self.assertEqual(selected_cuts, [("fixture song cut99sec", True), ("Clean catalog title", True)],
                         "Confirmation and selected-ID refresh must retain evidence that this is an excerpt")
        self.assertFalse(refreshed.json()["enrichment"]["lyrics"]["synced"])
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, track["id"])
            self.assertTrue(record.original["is_excerpt"])

    async def test_transcription_bounded_timing_private_and_survives_refresh(self):
        track, route = await self.prepared()
        path = route.replace("/enrichment", "/lyrics")
        self.assertEqual((await self.request("PUT", path, json={"source": "lrclib", "text": "fake"})).status_code, 422)
        self.assertEqual((await self.request("PUT", path, json={"source": "on_device_transcription", "text": "[04:00]Too late"})).status_code, 400)
        response = await self.request("PUT", path, json={"source": "on_device_transcription", "text": "[00:01]Owner transcript"})
        self.assertEqual(response.status_code, 200, response.text)
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=self.matched())), \
             patch("app.services.music_enrichment.fetch_artwork_thumbnail", AsyncMock(return_value=None)):
            await self.request("POST", route, json={})
        value = (await self.request("GET", path)).json()["lyrics"]
        self.assertEqual(value["source"], "on_device_transcription")
        self.assertEqual(value["text"], "Owner transcript")
        await self.request("DELETE", route.replace("/enrichment", ""))
        self.assertEqual((await self.request("POST", route, json={})).status_code, 404)

    async def test_catalog_timeout_does_not_break_track_or_playback(self):
        _, route = await self.prepared()
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(side_effect=TimeoutError)):
            response = await self.request("POST", route, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["enrichment"]["status"], "unavailable")
        self.assertEqual(response.json()["track"]["title"], "fixture song")
