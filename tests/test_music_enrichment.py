"""All requests use MockTransport. No owner metadata reaches live providers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from app.services.music_enrichment import (MusicEnrichmentService, MAX_JSON_BYTES,
    MAX_ARTWORK_BYTES, MAX_THUMBNAIL_BYTES, USER_AGENT, _artwork_redirect)

RECORDING = "11111111-2222-3333-4444-555555555555"
RELEASE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


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

    async def test_long_lrc_and_out_of_duration_lrc_never_become_synced(self):
        for synced in ("x" * (64 * 1024 + 1), "[05:00]Wrong timeline", "[00:01]" * 2001 + "amplified"):
            result = await self.service(lyrics=[{**self.lyric, "syncedLyrics": synced}]).enrich(self.track)
            self.assertIs(result["lyrics"]["synced"], False)
            self.assertEqual(result["lyrics"]["text"], self.lyric["plainLyrics"])

    async def test_instrumental_does_not_attach_unrelated_lyrics(self):
        result = await self.service(lyrics=[{**self.lyric, "instrumental": True}]).enrich(self.track)
        self.assertEqual(result["lyrics"]["source"], "none")

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
        self.assertEqual(len(self.requests), 2)

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
        self.assertEqual(len(self.requests), 132)

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


if __name__ == "__main__":
    unittest.main()
