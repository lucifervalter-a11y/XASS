"""All requests use MockTransport. No owner metadata reaches live providers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
import io
from types import SimpleNamespace
import unittest
import unicodedata
from unittest.mock import patch

import httpx
from PIL import Image

from app.services.music_enrichment import (MusicEnrichmentService, MAX_JSON_BYTES,
    MAX_ARTWORK_BYTES, MAX_THUMBNAIL_BYTES, USER_AGENT, _artwork, _artwork_redirect)

RECORDING = "11111111-2222-3333-4444-555555555555"
RELEASE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GROUP = "12345678-1234-1234-1234-123456789abc"
REISSUE = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


class Clock:
    def __init__(self):
        self.value = 0
        self.delays = []

    def now(self):
        return self.value

    async def sleep(self, seconds):
        self.delays.append(seconds)
        self.value += seconds


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


class MusicEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.track = SimpleNamespace(title="Fixture Song", artist="Fixture Artist", album="Fixture Album",
            duration=180, filename="private-owner-file.wav", sha256="f" * 64, deleted=False)
        self.lyric = {"id": 123, "trackName": "Fixture Song", "artistName": "Fixture Artist", "albumName": "Fixture Album",
            "duration": 180, "instrumental": False, "syncedLyrics": "[00:01.00]First fixture line\n[00:05.00]Second fixture line",
            "plainLyrics": "First fixture line\nSecond fixture line"}
        self.recording = {"id": RECORDING, "title": "Fixture Song", "length": 180000,
            "artist-credit": [{"artist": {"name": "Fixture Artist"}}],
            "releases": [{"id": RELEASE, "title": "Fixture Album", "status": "Official", "date": "2020-01-01"}]}
        self.requests = []
        self.clock = Clock()

    def service(self, lyrics=None, recordings=None, handler=None):
        lyrics = [deepcopy(self.lyric)] if lyrics is None else lyrics
        recordings = [deepcopy(self.recording)] if recordings is None else recordings
        def handle(request):
            self.requests.append(request)
            if handler:
                response = handler(request)
                if response is not None:
                    return response
            return httpx.Response(200, json=lyrics if request.url.host == "lrclib.net" else {"recordings": recordings})
        return MusicEnrichmentService(transport=httpx.MockTransport(handle), clock=self.clock.now,
            wall_clock=lambda: 1_700_000_000, sleep=self.clock.sleep)

    async def test_exact_match_returns_lyrics_catalog_provenance_and_bounded_artwork_descriptor(self):
        original = deepcopy(vars(self.track))
        result = await self.service().enrich(self.track)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidate"]["title"], "Fixture Song")
        self.assertEqual(result["lyrics"]["source"], "lrclib")
        self.assertIs(result["lyrics"]["synced"], True)
        self.assertEqual(result["lyrics"]["lines"][1]["time"], 5)
        self.assertEqual({row["source"] for row in result["provenance"]}, {"lrclib", "musicbrainz"})
        self.assertEqual(result["artwork"]["url"], f"https://coverartarchive.org/release/{RELEASE}/front-500")
        self.assertEqual(vars(self.track), original)
        for request in self.requests:
            self.assertEqual(request.headers["user-agent"], USER_AGENT)
            self.assertEqual(request.headers["accept-encoding"], "identity")
            self.assertNotIn(self.track.filename, str(request.url))
            self.assertNotIn(self.track.sha256, str(request.url))
            self.assertEqual(request.content, b"")
            self.assertNotIn("authorization", request.headers)
        self.assertGreaterEqual(self.clock.delays[-1], .35)

    async def test_missing_artist_full_query_can_match_only_when_both_names_are_present(self):
        self.track.artist, self.track.album = "", ""
        self.track.title = "Fixture Artist Fixture Song"
        result = await self.service().enrich(self.track)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(dict(self.requests[0].url.params), {"q": "Fixture Artist Fixture Song"})

    async def test_artist_title_filename_is_safe_structured_discovery(self):
        self.track.artist = ""
        self.track.title = "Fixture Artist - Fixture Song"
        self.assertEqual((await self.service().enrich(self.track))["status"], "matched")

    async def test_title_only_offers_confirmation_without_guessing_artist_or_lyrics(self):
        self.track.artist = ""
        result = await self.service().enrich(self.track)
        self.assertEqual((result["status"], result["reason"]), ("candidate", "metadata_needs_confirmation"))
        self.assertEqual(result["candidate"]["artist"], "Fixture Artist")
        self.assertEqual(result["lyrics"]["source"], "none")
        self.assertEqual(len(self.requests), 1)

    async def test_close_typo_can_only_offer_confirmation_never_automatic_lyrics(self):
        self.track.title = "Fixture Sogn"
        result = await self.service().enrich(self.track)
        self.assertEqual((result["status"], result["reason"]), ("candidate", "metadata_needs_confirmation"))
        self.assertEqual(result["candidates"], [result["candidate"]])
        self.assertEqual(result["lyrics"]["text"], "")
        self.track.title = "Fixture Song Remix"
        self.assertEqual((await self.service(recordings=[]).enrich(self.track))["status"], "not_found")

    async def test_cyrillic_cut_reupload_suggests_metadata_but_never_full_track_lyrics(self):
        self.track.title, self.track.artist, self.track.album, self.track.duration = "урал гайсин священная война reUploads cut99sec", "", "", 99
        self.lyric.update(trackName="Священная война", artistName="Урал Гайсин", albumName="", duration=240)
        result = await self.service().enrich(self.track)
        self.assertEqual((result["status"], result["reason"]), ("candidate", "duration_mismatch"))
        self.assertEqual(result["lyrics"]["text"], "")
        self.assertNotIn("cut99sec", str(self.requests[0].url.params))
        self.assertNotIn("reUploads", str(self.requests[0].url.params))

    async def test_wrong_duration_is_suggestion_not_an_automatic_match(self):
        self.lyric["duration"] = 183
        result = await self.service().enrich(self.track)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["reason"], "duration_mismatch")
        self.assertEqual(result["lyrics"]["lines"], [])
        self.assertEqual(result["lyrics"]["text"], "")
        # The length is unconfirmed, so timed lyrics stay off. The cover still
        # belongs to this exact title and artist.
        self.assertEqual(result["artwork"]["status"], "candidate")
        self.assertEqual(result["artwork"]["release_id"], RELEASE)
        self.assertIn("musicbrainz.org", {request.url.host for request in self.requests})

    async def test_untagged_filename_duration_mismatch_still_loads_a_cover(self):
        self.track.artist = self.track.album = ""
        self.track.title = "Fixture Artist Fixture Song"
        self.track.duration = 99
        self.lyric["albumName"] = ""
        result = await self.service().enrich(self.track)
        self.assertEqual((result["status"], result["reason"]), ("candidate", "duration_mismatch"))
        self.assertEqual(result["lyrics"]["text"], "")
        self.assertEqual(result["artwork"]["release_id"], RELEASE)

    async def test_persisted_excerpt_hint_survives_cleaned_title_and_matching_duration(self):
        self.track.is_excerpt = True
        result = await self.service().enrich(self.track)
        self.assertEqual((result["status"], result["reason"]), ("candidate", "duration_mismatch"))
        self.assertEqual(result["lyrics"]["lines"], [])

    async def test_remix_live_artist_and_album_mismatch_are_rejected(self):
        for change in ({"trackName": "Fixture Song (Remix)"}, {"trackName": "Fixture Song Live"},
                       {"artistName": "Another Artist"}, {"albumName": "Another Album"}):
            with self.subTest(change=change):
                result = await self.service(lyrics=[{**self.lyric, **change}], recordings=[]).enrich(self.track)
                self.assertEqual(result["status"], "not_found")
                self.assertIsNone(result["candidate"])

    async def test_ambiguous_records_are_not_resolved_by_result_order(self):
        result = await self.service(lyrics=[self.lyric, {**self.lyric, "id": 124, "plainLyrics": "Other words"}]).enrich(self.track)
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["candidate"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(result["lyrics"]["text"], "")
        self.assertEqual(result["artwork"]["status"], "not_found")
        self.assertTrue(all(request.url.host == "lrclib.net" for request in self.requests))

    async def test_long_lrc_and_out_of_duration_lrc_never_become_synced(self):
        for synced in ("x" * (64 * 1024 + 1), "[05:00]Wrong timeline", "[00:01]" * 2001 + "amplified"):
            result = await self.service(lyrics=[{**self.lyric, "syncedLyrics": synced}]).enrich(self.track)
            self.assertIs(result["lyrics"]["synced"], False)
            self.assertEqual(result["lyrics"]["text"], self.lyric["plainLyrics"])

    async def test_instrumental_does_not_attach_unrelated_lyrics(self):
        result = await self.service(lyrics=[{**self.lyric, "instrumental": True}]).enrich(self.track)
        self.assertEqual(result["lyrics"]["source"], "none")
        self.assertEqual(result["lyrics"]["status"], "instrumental")

    def selected(self):
        return {"title": "Fixture Song", "artist": "Fixture Artist", "album": "Fixture Album", "duration": 180,
            "source": "lrclib", "source_id": 123, "source_url": "https://untrusted.invalid/not-used"}

    async def test_confirmed_lrclib_id_resolves_ambiguity_without_repeating_search(self):
        service = self.service(handler=lambda req: httpx.Response(200, json=self.lyric))
        result = await service.confirm(self.track, self.selected())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["lyrics"]["source"], "lrclib")
        self.assertIs(result["lyrics"]["synced"], True)
        self.assertEqual(result["provenance"], [{"source": "lrclib", "id": 123, "url": "https://lrclib.net/lyrics/123"}])
        self.assertEqual([str(req.url) for req in self.requests if req.url.host == "lrclib.net"], ["https://lrclib.net/api/get/123"])
        self.assertEqual([req.url.host for req in self.requests], ["lrclib.net", "musicbrainz.org"])

    async def test_confirm_missing_invalid_id_and_unknown_source_never_fetch(self):
        service = self.service()
        for change in ({"source_id": None}, {"source_id": True}, {"source_id": "123"},
                       {"source_id": "../../private"}, {"source_id": 2**63}, {"source_id": -1},
                       {"source": "anything"}, {"source": "musicbrainz", "source_id": "bad"}):
            result = await service.confirm(self.track, {**self.selected(), **change})
            self.assertEqual(result["reason"], "invalid_candidate")
            self.assertEqual(result["lyrics"]["text"], "")
        self.assertEqual(self.requests, [])

    async def test_confirm_rechecks_fetched_id_title_artist_album_and_duration(self):
        for change in ({"id": 124}, {"trackName": "Different Song"}, {"artistName": "Another Artist"},
                       {"albumName": "Wrong Album"}, {"trackName": "Fixture Sogn"}, {"duration": 220}):
            with self.subTest(change=change):
                service = self.service(handler=lambda req: httpx.Response(200, json={**self.lyric, **change}))
                result = await service.confirm(self.track, self.selected())
                self.assertNotEqual(result["status"], "matched")
                self.assertEqual(result["lyrics"]["text"], "")
                self.assertEqual(result["lyrics"]["lines"], [])

    async def test_confirm_uses_owner_selected_metadata_but_original_audio_duration(self):
        self.track.title = "Mistyped Fxituer title"
        self.track.artist = ""
        service = self.service(handler=lambda req: httpx.Response(200, json=self.lyric))
        result = await service.confirm(self.track, self.selected())
        self.assertEqual(result["status"], "matched")
        self.track.duration = 99
        result = await service.confirm(self.track, self.selected())
        self.assertEqual(result["reason"], "duration_mismatch")
        self.assertEqual(result["lyrics"]["text"], "")

    async def test_empty_structured_search_uses_one_broader_query(self):
        def handle(request):
            if request.url.host != "lrclib.net":
                return None
            if request.url.params.get("q"):
                return httpx.Response(200, json=[self.lyric])
            return httpx.Response(200, json=[])
        result = await self.service(recordings=[], handler=handle).enrich(self.track)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["lyrics"]["text"], self.lyric["plainLyrics"])
        lrclib = [request for request in self.requests if request.url.host == "lrclib.net"]
        self.assertEqual(len(lrclib), 2)
        self.assertEqual(lrclib[0].url.params["track_name"], "Fixture Song")
        self.assertEqual(lrclib[1].url.params["q"], "Fixture Artist Fixture Song")

    async def test_confirm_duration_drift_keeps_lyrics_off_and_keeps_the_cover(self):
        self.track.duration = 99
        result = await self.service(handler=lambda request: httpx.Response(200, json=self.lyric) if request.url.host == "lrclib.net" else None).confirm(self.track, self.selected())
        self.assertEqual(result["reason"], "duration_mismatch")
        self.assertEqual(result["lyrics"]["text"], "")
        self.assertEqual(result["lyrics"]["lines"], [])
        self.assertEqual(result["artwork"]["release_id"], RELEASE)

    async def test_confirm_known_cut_never_fetches_or_adds_full_recording_lyrics(self):
        service = self.service()
        self.track.is_excerpt = True
        result = await service.confirm(self.track, self.selected())
        self.assertEqual(result["reason"], "duration_mismatch")
        self.assertEqual(result["lyrics"]["text"], "")
        del self.track.is_excerpt
        self.track.title = "Fixture Song cut99sec"
        self.assertEqual((await service.confirm(self.track, self.selected()))["reason"], "duration_mismatch")
        self.assertEqual(self.requests, [])

    async def test_confirm_cache_is_bound_to_id_and_file_signature(self):
        service = self.service(handler=lambda req: httpx.Response(200, json={**self.lyric, "id": int(req.url.path.rsplit("/", 1)[1])}))
        first, second = await asyncio.gather(service.confirm(self.track, self.selected()), service.confirm(self.track, self.selected()))
        self.assertEqual(len([req for req in self.requests if req.url.host == "lrclib.net"]), 1)
        first["lyrics"]["lines"].clear()
        self.assertEqual(len(second["lyrics"]["lines"]), 2)
        await service.confirm(self.track, {**self.selected(), "source_id": 124})
        self.track.duration = 99
        await service.confirm(self.track, self.selected())
        self.assertEqual(len([req for req in self.requests if req.url.host == "lrclib.net"]), 3)

    async def test_confirm_not_found_rate_limit_and_oversized_response_are_bounded(self):
        for bad, expected in ((httpx.Response(404), "not_found"),
                              (httpx.Response(429, headers={"Retry-After": "120"}), "rate_limited"),
                              (httpx.Response(200, content=b"x" * (MAX_JSON_BYTES + 1)), "unavailable")):
            service = self.service(handler=lambda req: bad)
            result = await service.confirm(self.track, self.selected())
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["lyrics"]["text"], "")
        service = self.service(handler=lambda req: httpx.Response(429, headers={"Retry-After": "120"}))
        self.requests.clear()
        await service.confirm(self.track, self.selected())
        result = await service.confirm(self.track, {**self.selected(), "source_id": 124})
        self.assertEqual((result["status"], result["retry_after"]), ("rate_limited", 120))
        self.assertEqual(len(self.requests), 1)

    async def test_confirm_musicbrainz_does_not_force_an_ambiguous_lyrics_record(self):
        service = self.service(lyrics=[self.lyric, {**self.lyric, "id": 124}])
        result = await service.confirm(self.track, {**self.selected(), "source": "musicbrainz", "source_id": RECORDING})
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["lyrics"]["text"], "")
        self.assertEqual(self.requests[0].url.path, "/api/search")

    async def test_musicbrainz_fallback_and_millisecond_duration(self):
        result = await self.service(lyrics=[]).enrich(self.track)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidate"]["source"], "musicbrainz")
        self.assertEqual(result["candidate"]["duration"], 180)
        self.assertEqual(result["lyrics"]["source"], "none")

    async def test_musicbrainz_full_filename_fallback_queries_artist_and_title(self):
        self.track.artist, self.track.album = "", ""
        self.track.title = "Fixture Artist Fixture Song"
        result = await self.service(lyrics=[]).enrich(self.track)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidate"]["artist"], "Fixture Artist")
        query = self.requests[1].url.params["query"]
        self.assertIn('recording:"fixture" OR artist:"fixture"', query)

    async def test_musicbrainz_wrong_known_album_is_not_an_automatic_match(self):
        self.recording["releases"][0]["title"] = "Wrong Album"
        self.assertEqual((await self.service(lyrics=[]).enrich(self.track))["status"], "not_found")

    async def test_musicbrainz_live_disambiguation_and_ambiguous_album_art_are_rejected(self):
        result = await self.service(lyrics=[], recordings=[{**self.recording, "disambiguation": "live recording"}]).enrich(self.track)
        self.assertEqual(result["status"], "not_found")
        self.track.album = ""
        self.lyric["albumName"] = ""
        self.recording["releases"].append({"id": RECORDING, "title": "Compilation", "status": "Official"})
        result = await self.service().enrich(self.track)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["artwork"]["status"], "not_found")

    def grouped_releases(self, primary="Album"):
        group = {"id": GROUP, "title": "Original Album", "primary-type": primary, "secondary-types": []}
        return [
            {"id": RELEASE, "title": "Original Album", "status": "Official", "release-group": deepcopy(group)},
            {"id": REISSUE, "title": "Original Album Deluxe", "status": "Official", "release-group": deepcopy(group)},
            {"id": RECORDING, "title": "Compilation", "status": "Official", "release-group": {
                "id": RECORDING, "title": "Compilation", "primary-type": "Album", "secondary-types": ["Compilation"]}},
        ]

    def test_unique_primary_album_or_ep_group_covers_reissues_not_compilations(self):
        for primary in ("Album", "EP"):
            releases = self.grouped_releases(primary)
            result = _artwork({"releases": releases}, "")
            self.assertEqual(result["release_group_id"], GROUP)
            self.assertEqual(result["release_ids"], [RELEASE, REISSUE])
            self.assertEqual(result["url"], f"https://coverartarchive.org/release-group/{GROUP}/front-500")
            self.assertEqual(result["source_url"], f"https://musicbrainz.org/release-group/{GROUP}")
            # An explicitly known album still selects its own release, as before.
            specific = _artwork({"releases": releases}, "Original Album")
            self.assertEqual(specific["release_id"], RELEASE)
            self.assertNotIn("release_group_id", specific)

    def test_group_cover_keeps_multiple_primary_groups_and_incomplete_metadata_ambiguous(self):
        for mutation in (
            lambda rows: rows[2]["release-group"].update({"secondary-types": []}),
            lambda rows: rows[2]["release-group"].update({"primary-type": "EP", "secondary-types": []}),
            lambda rows: rows[1].pop("release-group"),
            lambda rows: rows[1]["release-group"].update({"id": "../../private"}),
            lambda rows: rows[1]["release-group"].update({"primary-type": None}),
            lambda rows: rows[1]["release-group"].update({"primary-type": []}),
            lambda rows: rows[1]["release-group"].update({"secondary-types": "Live"}),
            lambda rows: rows[1]["release-group"].update({"secondary-types": [None]}),
            lambda rows: rows[1]["release-group"].update({"title": "Conflicting group name"}),
            lambda rows: rows[1]["release-group"].update({"secondary-types": ["Live"]}),
        ):
            releases = self.grouped_releases()
            mutation(releases)
            self.assertEqual(_artwork({"releases": releases}, "")["status"], "not_found")

    def test_group_cover_excludes_live_remix_compilation_single_and_unofficial_releases(self):
        for excluded in ("Compilation", "Live", "Remix"):
            releases = self.grouped_releases()
            releases[2]["release-group"]["secondary-types"] = [excluded]
            self.assertEqual(_artwork({"releases": releases}, "")["release_group_id"], GROUP)
            for row in releases[:2]:
                row["release-group"]["secondary-types"] = [excluded]
            self.assertEqual(_artwork({"releases": releases}, "")["status"], "not_found")
        releases = self.grouped_releases()
        releases[2]["release-group"].update({"primary-type": "Single", "secondary-types": []})
        self.assertEqual(_artwork({"releases": releases}, "")["release_group_id"], GROUP)
        releases[2].update({"status": "Bootleg", "release-group": {}})
        self.assertEqual(_artwork({"releases": releases}, "")["release_group_id"], GROUP)

    async def test_exact_match_and_duration_only_candidate_use_group_art_without_relaxing_lyrics(self):
        self.track.album = self.lyric["albumName"] = ""
        self.recording["releases"] = self.grouped_releases()
        matched = await self.service().enrich(self.track)
        self.assertEqual(matched["status"], "matched")
        self.assertTrue(matched["lyrics"]["synced"])
        self.assertEqual(matched["artwork"]["release_group_id"], GROUP)
        self.track.duration = 99
        candidate = await self.service().enrich(self.track)
        self.assertEqual(candidate["status"], "candidate")
        self.assertFalse(candidate["lyrics"]["text"])
        self.assertEqual(candidate["artwork"]["release_group_id"], GROUP)

    async def test_group_art_follows_only_known_member_release_to_sanitized_thumbnail(self):
        descriptor = _artwork({"releases": self.grouped_releases()}, "")
        buffer = io.BytesIO()
        Image.new("RGB", (600, 600), "blue").save(buffer, "PNG")
        member = f"https://coverartarchive.org/release/{REISSUE}/front-500"
        target = f"https://archive.org/download/mbid-{REISSUE}/mbid-{REISSUE}-123_thumb500.jpg"
        def handle(request):
            if "/release-group/" in request.url.path:
                return httpx.Response(307, headers={"Location": member})
            if request.url.host == "coverartarchive.org":
                return httpx.Response(307, headers={"Location": target})
            return httpx.Response(200, content=buffer.getvalue())
        result = await self.service(handler=handle).fetch_artwork(descriptor)
        self.assertTrue(result.startswith(b"\xff\xd8\xff"))
        self.assertLessEqual(len(result), MAX_THUMBNAIL_BYTES)
        self.assertEqual(str(self.requests[0].url), descriptor["url"])
        self.assertEqual(len(self.requests), 3)
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (512, 512))

    async def test_group_art_invalid_descriptors_and_redirects_fail_closed(self):
        descriptor = _artwork({"releases": self.grouped_releases()}, "")
        for change in ({"release_group_id": "../../private"}, {"release_id": RELEASE},
                {"release_ids": []}, {"release_ids": ["../../private"]}, {"release_ids": [RELEASE, RELEASE]},
                {"release_ids": [RELEASE] * 101}, {"release_ids": "not-a-list"},
                {"url": "https://evil.invalid/art"}):
            service = self.service()
            self.assertIsNone(await service.fetch_artwork({**descriptor, **change}))
        self.assertEqual(self.requests, [])
        for target in (f"https://archive.org/download/mbid-{RECORDING}/mbid-{RECORDING}-123-500.jpg",
                f"https://coverartarchive.org/release/{RECORDING}/front-500",
                "http://127.0.0.1/private", "https://evil.invalid/art",
                f"https://user@archive.org/download/mbid-{RELEASE}/mbid-{RELEASE}-123-500.jpg",
                f"https://coverartarchive.org/release/{RELEASE}/front-500?next=private"):
            self.requests.clear()
            service = self.service(handler=lambda request: httpx.Response(307, headers={"Location": target}))
            self.assertIsNone(await service.fetch_artwork(descriptor))
            self.assertEqual(len(self.requests), 1)

    async def test_group_art_oversized_body_never_reaches_decoder(self):
        descriptor = _artwork({"releases": self.grouped_releases()}, "")
        service = self.service(handler=lambda request: httpx.Response(200, content=b"x" * (MAX_ARTWORK_BYTES + 1)))
        with patch("app.services.music_enrichment._jpeg_thumbnail") as decode:
            self.assertIsNone(await service.fetch_artwork(descriptor))
            decode.assert_not_called()

    async def test_positive_negative_cache_coalescing_and_defensive_copies(self):
        service = self.service()
        first, second = await asyncio.gather(service.enrich(self.track), service.enrich(self.track))
        self.assertEqual(len(self.requests), 2)
        first["lyrics"]["lines"].clear()
        self.assertEqual(len(second["lyrics"]["lines"]), 2)
        self.assertEqual(len((await service.enrich(self.track))["lyrics"]["lines"]), 2)
        self.assertEqual(len(self.requests), 2)
        self.clock.value += 86401
        await service.enrich(self.track)
        self.assertEqual(len(self.requests), 4)
        self.requests.clear()
        service = self.service(lyrics=[], recordings=[])
        await service.enrich(self.track)
        await service.enrich(self.track)
        self.assertEqual(len(self.requests), 3)

    async def test_rate_limit_retry_after_is_honored_across_different_queries(self):
        service = self.service(recordings=[], handler=lambda req: httpx.Response(429, headers={"Retry-After": "120"}) if req.url.host == "lrclib.net" else None)
        first = await service.enrich(self.track)
        self.track.title = "Another Song"
        second = await service.enrich(self.track)
        self.assertEqual(first["status"], "rate_limited")
        self.assertGreaterEqual(second["retry_after"], 118)
        self.assertEqual(sum(req.url.host == "lrclib.net" for req in self.requests), 1)
        self.clock.value += 121
        self.track.title = "Third Song"
        await service.enrich(self.track)
        self.assertEqual(sum(req.url.host == "lrclib.net" for req in self.requests), 2)

    async def test_http_date_retry_after_is_honored(self):
        until = format_datetime(datetime.fromtimestamp(1_700_000_090, timezone.utc), usegmt=True)
        service = self.service(recordings=[], handler=lambda req: httpx.Response(429, headers={"Retry-After": until}) if req.url.host == "lrclib.net" else None)
        result = await service.enrich(self.track)
        self.assertEqual(result["retry_after"], 90)

    async def test_timeout_redirect_malformed_compressed_and_oversized_json_fail_closed(self):
        responses = [httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}),
            httpx.Response(200, content=b"{invalid"), httpx.Response(200, json={"unexpected": True}),
            httpx.Response(200, content=b"x" * (MAX_JSON_BYTES + 1)),
            httpx.Response(200, content=b"{}", headers={"Content-Length": str(MAX_JSON_BYTES + 1)})]
        for bad in responses:
            with self.subTest(status=bad.status_code):
                self.requests.clear()
                service = self.service(recordings=[], handler=lambda req: bad if req.url.host == "lrclib.net" else None)
                self.assertEqual((await service.enrich(self.track))["status"], "unavailable")
                self.assertTrue(all(req.url.host in {"lrclib.net", "musicbrainz.org"} for req in self.requests))
        def timeout(req):
            if req.url.host == "lrclib.net":
                raise httpx.ReadTimeout("fixture timeout")
        self.assertEqual((await self.service(recordings=[], handler=timeout).enrich(self.track))["status"], "unavailable")

    async def test_chunked_json_and_encoded_body_budgets_apply_before_parsing(self):
        stream = ChunkStream([b"x" * 16384] * (MAX_JSON_BYTES // 16384 + 5))
        service = self.service(recordings=[], handler=lambda req: httpx.Response(200, stream=stream) if req.url.host == "lrclib.net" else None)
        self.assertEqual((await service.enrich(self.track))["status"], "unavailable")
        self.assertEqual(stream.reads, MAX_JSON_BYTES // 16384 + 1)
        compressed = ChunkStream([b"not-real-gzip"])
        service = self.service(recordings=[], handler=lambda req: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=compressed) if req.url.host == "lrclib.net" else None)
        self.assertEqual((await service.enrich(self.track))["status"], "unavailable")
        self.assertEqual(compressed.reads, 0)

    async def test_cache_entry_count_is_bounded_and_manual_title_change_invalidates(self):
        service = self.service(lyrics=[], recordings=[])
        for index in range(66):
            self.track.title = "Song " + str(index)
            await service.enrich(self.track)
        self.assertEqual(len(service._cache), 64)
        self.assertLessEqual(service._cache_bytes, 8 * 1024 * 1024)
        self.assertEqual(len(self.requests), 198)

    async def test_catalog_invalid_ids_and_nonfinite_duration_never_gain_auto_match(self):
        result = await self.service(lyrics=[{**self.lyric, "id": "../../path"}], recordings=[]).enrich(self.track)
        self.assertEqual(result["status"], "not_found")
        result = await self.service(lyrics=[{**self.lyric, "duration": "nan"}], recordings=[]).enrich(self.track)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["lyrics"]["text"], "")

    async def test_missing_duration_deleted_or_unbounded_metadata_make_no_requests(self):
        for values in ({"duration": float("nan")}, {"duration": 0}, {"deleted": True}, {"title": "x" * 481}):
            track = SimpleNamespace(**{**vars(self.track), **values})
            self.assertEqual((await self.service().enrich(track))["status"], "insufficient_metadata")
        self.assertEqual(self.requests, [])

    async def test_bare_503_does_not_silence_the_catalog_for_a_minute(self):
        def handle(request):
            if request.url.host == "lrclib.net":
                return httpx.Response(503)
            return httpx.Response(200, json={"recordings": []})
        service = self.service(lyrics=[], recordings=[], handler=handle)
        result = await service.enrich(self.track)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("retry_after", result)
        self.clock.value += 6
        self.requests.clear()
        again = await service.enrich(self.track, refresh=True)
        self.assertEqual(again["status"], "unavailable")
        self.assertTrue(any(request.url.host == "lrclib.net" for request in self.requests))

    async def test_provider_unavailable_does_not_discard_matched_lyrics(self):
        result = await self.service(handler=lambda req: httpx.Response(503) if req.url.host == "musicbrainz.org" else None).enrich(self.track)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["lyrics"]["source"], "lrclib")

    async def test_musicbrainz_rate_limit_without_lyrics_is_retryable_not_not_found(self):
        result = await self.service(lyrics=[], handler=lambda req: httpx.Response(503, headers={"Retry-After": "45"}) if req.url.host == "musicbrainz.org" else None).enrich(self.track)
        self.assertEqual((result["status"], result["retry_after"]), ("rate_limited", 45))

    def artwork(self):
        return {"source": "coverartarchive", "release_id": RELEASE,
            "url": f"https://coverartarchive.org/release/{RELEASE}/front-500"}

    async def test_artwork_redirect_whitelist_and_sanitized_jpeg_output(self):
        buffer = io.BytesIO()
        Image.new("RGB", (900, 900), "red").save(buffer, "PNG")
        target = f"https://archive.org/download/mbid-{RELEASE}/mbid-{RELEASE}-123-500.jpg"
        def handle(req):
            return httpx.Response(307, headers={"Location": target}) if req.url.host == "coverartarchive.org" else httpx.Response(200, content=buffer.getvalue())
        result = await self.service(handler=handle).fetch_artwork(self.artwork())
        self.assertLessEqual(len(result), MAX_THUMBNAIL_BYTES)
        self.assertTrue(result.startswith(b"\xff\xd8\xff"))
        with Image.open(io.BytesIO(result)) as decoded:
            self.assertEqual(decoded.size, (512, 512))

    async def test_actual_caa_thumb500_redirect_format_is_accepted_only_for_matching_release(self):
        target = f"https://archive.org/download/mbid-{RELEASE}/mbid-{RELEASE}-46026879213_thumb500.jpg"
        final = f"https://dn710003.ca.archive.org/0/items/mbid-{RELEASE}/mbid-{RELEASE}-46026879213_thumb500.jpg"
        self.assertIs(_artwork_redirect(target, RELEASE), True)
        self.assertIs(_artwork_redirect(final, RELEASE), True)
        for invalid in (target.replace("_thumb500", "_thumb1200"), target.replace(RELEASE, RECORDING),
                        target.replace(".jpg", ".svg"), target + "?url=http://127.0.0.1",
                        final.replace("ca.archive.org", "ca.archive.org.evil.invalid"),
                        final.replace("dn710003.ca.archive.org", "anything.archive.org")):
            self.assertIs(_artwork_redirect(invalid, RELEASE), False)
        buffer = io.BytesIO()
        Image.new("RGB", (500, 500), "blue").save(buffer, "JPEG")
        def handle(req):
            if req.url.host == "coverartarchive.org":
                return httpx.Response(307, headers={"Location": target})
            if req.url.host == "archive.org":
                return httpx.Response(302, headers={"Location": final})
            return httpx.Response(200, content=buffer.getvalue())
        result = await self.service(handler=handle).fetch_artwork(self.artwork())
        self.assertTrue(result.startswith(b"\xff\xd8\xff"))

    async def test_arbitrary_initial_artwork_url_or_id_never_fetches(self):
        service = self.service()
        for change in ({"url": "http://127.0.0.1/"}, {"url": "https://evil.invalid/cover.jpg"},
                       {"release_id": "../../private"}, {"source": "untrusted"}):
            self.assertIsNone(await service.fetch_artwork({**self.artwork(), **change}))
        self.assertEqual(self.requests, [])

    async def test_redirects_to_private_wrong_release_userinfo_and_http_are_never_followed(self):
        good = f"https://archive.org/download/mbid-{RELEASE}/mbid-{RELEASE}-123-500.jpg"
        bad_values = ["http://127.0.0.1/private", good.replace("https:", "http:"),
            good.replace("archive.org", "archive.org.evil.invalid"), good.replace("archive.org", "user@archive.org"),
            good.replace(RELEASE, RECORDING), good + "?redirect=evil", good.replace("archive.org", "archive.org:444"),
            f"https://archive.org/download/mbid-{RELEASE}/../private"]
        for value in bad_values:
            self.assertIs(_artwork_redirect(value, RELEASE), False)
            self.requests.clear()
            service = self.service(handler=lambda req: httpx.Response(307, headers={"Location": value}))
            self.assertIsNone(await service.fetch_artwork(self.artwork()))
            self.assertEqual(len(self.requests), 1)

    async def test_oversized_artwork_never_invokes_image_decoder(self):
        service = self.service(handler=lambda req: httpx.Response(200, content=b"x" * (MAX_ARTWORK_BYTES + 1)))
        with patch("app.services.music_enrichment._jpeg_thumbnail") as decode:
            self.assertIsNone(await service.fetch_artwork(self.artwork()))
            decode.assert_not_called()

    async def test_nfd_structured_filename_and_known_export_suffix_query_normalized_nfc(self):
        self.track.title = unicodedata.normalize("NFD", "Певец Йота - Речной путь [facetext]")
        self.track.artist = self.track.album = ""
        original = self.track.title
        lyric = {**self.lyric, "trackName": "Речной путь", "artistName": "Певец Йота", "albumName": ""}
        result = await self.service(lyrics=[lyric], recordings=[]).enrich(self.track)
        self.assertEqual(result["status"], "matched")
        self.assertTrue(result["lyrics"]["synced"])
        self.assertEqual(self.requests[0].url.params["track_name"], "Речной путь")
        self.assertEqual(self.requests[0].url.params["artist_name"], "Певец Йота")
        self.assertEqual(self.track.title, original, "Discovery does not mutate original metadata")

    async def test_legacy_underscores_and_spaced_cut_duration_offer_only_confirmation(self):
        for cut in ("cut99sec", "cut 99sec", "cut 99 sec"):
            self.requests.clear()
            self.track.title = ("Fixture Artist - Fixture Song reUploads " + cut).replace(" ", "_")
            self.track.artist = self.track.album = ""
            self.track.duration = 99
            result = await self.service().enrich(self.track)
            self.assertEqual(result["status"], "candidate", cut)
            self.assertEqual(result["reason"], "duration_mismatch")
            self.assertEqual(self.requests[0].url.params["track_name"], "Fixture Song")
            self.assertEqual(result["lyrics"]["text"], "")

    async def test_only_balanced_explicit_promo_suffix_is_removed_and_versions_remain(self):
        self.track.artist = self.track.album = ""
        self.track.title = "Fixture Artist - Fixture Song (telegram @fixture_channel)"
        self.assertEqual((await self.service().enrich(self.track))["status"], "matched")
        for suffix in (" [Live]", " (Remix)", " (telegram @fixture_channel]"):
            self.requests.clear()
            self.track.title = "Fixture Artist - Fixture Song" + suffix
            result = await self.service().enrich(self.track)
            self.assertNotEqual(result["status"], "matched", suffix)
            self.assertIn(suffix.strip().replace("_", " "), self.requests[0].url.params["track_name"])

    async def test_optional_catalog_outage_cache_retries_after_sixty_seconds(self):
        failed = True
        def handle(request):
            if request.url.host == "musicbrainz.org" and failed:
                return httpx.Response(503)
        service = self.service(handler=handle)
        first = await service.enrich(self.track)
        self.assertEqual(first["status"], "matched")
        self.assertEqual(first["artwork_reason"], "catalog_unavailable")
        self.requests.clear()
        self.clock.value += 59
        self.assertEqual(await service.enrich(self.track), first)
        self.assertEqual(self.requests, [])
        failed = False
        self.clock.value += 2
        recovered = await service.enrich(self.track)
        self.assertEqual(recovered["artwork"]["status"], "candidate")
        self.assertNotIn("artwork_reason", recovered)
        self.assertTrue(self.requests)

    async def test_explicit_refresh_revalidates_positive_negative_and_selected_cache(self):
        def handle(request):
            if request.url.host == "lrclib.net":
                return httpx.Response(200, json=self.lyric if "/get/" in request.url.path else [self.lyric])
        service = self.service(handler=handle)
        original = await service.enrich(self.track)
        self.lyric["syncedLyrics"] = "[00:01.00]Updated fixture"
        self.assertEqual((await service.enrich(self.track))["lyrics"], original["lyrics"])
        self.assertIn("Updated fixture", (await service.enrich(self.track, refresh=True))["lyrics"]["text"])
        selected = await service.confirm(self.track, self.selected())
        self.lyric["syncedLyrics"] = "[00:01.00]Updated selected fixture"
        self.assertEqual((await service.confirm(self.track, self.selected()))["lyrics"], selected["lyrics"])
        self.assertIn("Updated selected fixture", (await service.confirm(self.track, self.selected(), refresh=True))["lyrics"]["text"])
        rows = []
        def negative(request):
            return httpx.Response(200, json=rows if request.url.host == "lrclib.net" else {"recordings": []})
        service = self.service(handler=negative)
        self.assertEqual((await service.enrich(self.track))["status"], "not_found")
        rows.append(self.lyric)
        self.assertEqual((await service.enrich(self.track))["status"], "not_found")
        self.assertEqual((await service.enrich(self.track, refresh=True))["status"], "matched")

    async def test_explicit_refresh_never_clears_provider_retry_after(self):
        service = self.service(handler=lambda request: httpx.Response(429, headers={"Retry-After": "120"}))
        await service.enrich(self.track)
        calls = len(self.requests)
        result = await service.enrich(self.track, refresh=True)
        self.assertEqual(result["status"], "rate_limited")
        self.assertGreater(result["retry_after"], 100)
        self.assertEqual(len(self.requests), calls)

    async def test_optional_slow_catalog_cannot_exhaust_verified_lyrics_budget(self):
        async def handle(request):
            if request.url.host == "lrclib.net":
                return httpx.Response(200, json=[self.lyric])
            await asyncio.Event().wait()
        service = MusicEnrichmentService(transport=httpx.MockTransport(handle), clock=self.clock.now, sleep=self.clock.sleep)
        deadline = asyncio.get_running_loop().time() + .25
        result = await asyncio.wait_for(service.enrich(self.track, deadline=deadline), .5)
        self.assertEqual(result["status"], "matched")
        self.assertTrue(result["lyrics"]["synced"])
        self.assertEqual(result["artwork_reason"], "catalog_unavailable")

    async def test_selected_lrclib_keeps_owner_id_and_adds_only_exact_catalog_artwork(self):
        def handle(request):
            return httpx.Response(200, json=self.lyric) if request.url.host == "lrclib.net" else None
        result = await self.service(handler=handle).confirm(self.track, self.selected())
        self.assertEqual(result["candidate"]["source_id"], 123)
        self.assertEqual(result["artwork"]["release_id"], RELEASE)
        self.assertEqual([row["source"] for row in result["provenance"]], ["lrclib", "musicbrainz"])
        for change in ({"title": "Fixture Song (Live)"}, {"length": 220000},
                       {"artist-credit": [{"name": "Another artist"}]}):
            result = await self.service(recordings=[{**self.recording, **change}], handler=handle).confirm(self.track, self.selected())
            self.assertEqual(result["status"], "matched")
            self.assertEqual(result["candidate"]["source_id"], 123)
            self.assertEqual(result["artwork"]["status"], "not_found")
            self.assertTrue(result["lyrics"]["synced"])

    async def test_provider_lock_wait_is_inside_budget_and_cancellation_propagates(self):
        service = self.service()
        await service._lock.acquire()
        try:
            result = await service.enrich(self.track, deadline=asyncio.get_running_loop().time() + .02)
        finally:
            service._lock.release()
        self.assertEqual(result["reason"], "catalog_busy")
        self.assertEqual(self.requests, [])
        entered = asyncio.Event()
        async def delayed(request):
            entered.set()
            await asyncio.Event().wait()
        service = MusicEnrichmentService(transport=httpx.MockTransport(delayed))
        task = asyncio.create_task(service.enrich(self.track))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(service._lock.locked())


if __name__ == "__main__":
    unittest.main()
