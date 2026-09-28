"""Synced lyrics service: metadata guesses, LRC → timed lines, provider fallbacks (no network)."""
from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.services import synced_lyrics as sl

LRC = "[00:01.00]First line\n[00:04.50]Second line\n[00:07.00]\n[00:10.00]Third"


def transport(routes):
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.url.path, dict(request.url.params)))
        for match, payload in routes:
            if match(request):
                if payload is None:
                    return httpx.Response(404, json={"code": 404})
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler), calls


class SyncedLyricsTests(unittest.IsolatedAsyncioTestCase):
    def test_timed_lines_use_next_start_and_blank_rows_as_end(self):
        lines = sl.timed_lines(LRC, 200)
        self.assertEqual([l["text"] for l in lines], ["First line", "Second line", "Third"])
        self.assertEqual(lines[0]["start"], 1.0)
        self.assertEqual(lines[0]["end"], 4.5)
        self.assertEqual(lines[1]["end"], 7.0)
        self.assertEqual(lines[2]["end"], 18.0)

    def test_queries_split_filenames_and_strip_noise(self):
        self.assertEqual(sl.queries("Кино - Группа крови (Official Video)", "Unknown artist")[0], ("Кино", "Группа крови"))
        self.assertEqual(sl.queries("03. Song", "", "Artist - Song.mp3")[0], ("Artist", "Song"))
        self.assertEqual(sl.queries("Numb [Official Audio]", "Linkin Park")[0], ("Linkin Park", "Numb"))

    async def test_exact_get_returns_synced(self):
        mock, calls = transport([(lambda r: r.url.path == "/api/get", {"id": 7, "syncedLyrics": LRC, "plainLyrics": "x"})])
        result = await sl.LrclibClient(mock).lookup("Song", "Artist", "", 200)
        self.assertEqual(result["status"], "synced")
        self.assertEqual(len(result["lines"]), 3)
        self.assertEqual(calls[0][1]["duration"], "200")

    async def test_search_fallback_prefers_synced_near_duration(self):
        rows = [
            {"id": 1, "trackName": "Song", "artistName": "Artist", "duration": 320, "syncedLyrics": LRC},
            {"id": 2, "trackName": "Song", "artistName": "Artist", "duration": 201, "syncedLyrics": None, "plainLyrics": "plain"},
            {"id": 3, "trackName": "Song", "artistName": "Artist", "duration": 203, "syncedLyrics": LRC},
        ]
        mock, _ = transport([(lambda r: r.url.path == "/api/get", None), (lambda r: r.url.path == "/api/search", rows)])
        result = await sl.LrclibClient(mock).lookup("Song", "Artist", "", 200)
        self.assertEqual(result["source_id"], 3)

    async def test_search_rejects_substring_title_and_artist(self):
        rows = [
            {"id": 1, "trackName": "Song Remix", "artistName": "Artist", "duration": 200, "syncedLyrics": LRC},
            {"id": 2, "trackName": "Song", "artistName": "Artist Project", "duration": 200, "syncedLyrics": LRC},
            {"id": 3, "trackName": "My Song", "artistName": "Artist", "duration": 200, "syncedLyrics": LRC},
        ]
        mock, _ = transport([(lambda r: r.url.path == "/api/get", None), (lambda r: r.url.path == "/api/search", rows)])
        result = await sl.LrclibClient(mock).lookup("Song", "Artist", "", 200)
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["lines"], [])

    async def test_finished_transcription_beats_instrumental_catalog(self):
        track = SimpleNamespace(id=9, title="Song", artist="Artist", album="", duration=200, filename="a.mp3", sha256="1" * 64)
        instrumental = {"status": "instrumental", "synced": False, "lines": [], "text": "", "source": "lrclib"}
        pc = {"lines": [{"start": 1.0, "end": 2.0, "text": "слова"}]}
        client = SimpleNamespace(lookup=unittest.mock.AsyncMock(return_value=instrumental))
        value = await sl.resolve(track, owner=None, embedded=None, enrichment=None, cache=sl.LyricsCache(None),
                                  client=client, transcription=pc, refresh=True)
        self.assertEqual(value["source"], "pc_transcription")
        self.assertEqual(value["lines"][0]["text"], "слова")
        plain = await sl.resolve(track, owner=None, embedded=None, enrichment=None, cache=sl.LyricsCache(None),
                                  client=client, refresh=True)
        self.assertEqual(plain["status"], "instrumental")

    async def test_network_failure_is_unavailable_not_crash(self):
        def boom(request):
            raise httpx.ConnectError("down")
        result = await sl.LrclibClient(httpx.MockTransport(boom)).lookup("Song", "Artist", "", 200)
        self.assertEqual(result["status"], "unavailable")

    async def test_resolve_prefers_owner_sync_and_caches_provider(self):
        track = SimpleNamespace(id=5, title="Song", artist="Artist", album="", duration=200, filename="a.mp3", sha256="0" * 64)
        owner = {"text": "a", "synced": True, "lines": [{"time": 2.0, "text": "Mine"}], "source": "on_device_transcription"}
        with tempfile.TemporaryDirectory() as folder:
            cache = sl.LyricsCache(Path(folder))
            mock, calls = transport([(lambda r: r.url.path == "/api/get", {"id": 7, "syncedLyrics": LRC})])
            client = sl.LrclibClient(mock)
            value = await sl.resolve(track, owner=owner, embedded=None, enrichment=None, cache=cache, client=client)
            self.assertEqual(value["lines"][0]["text"], "Mine")
            self.assertEqual(calls, [])
            value = await sl.resolve(track, owner=None, embedded=None, enrichment=None, cache=cache, client=client)
            self.assertTrue(value["synced"])
            count = len(calls)
            sl._memory.clear()
            again = await sl.resolve(track, owner=None, embedded=None, enrichment=None, cache=cache, client=client)
            self.assertEqual(again["lines"], value["lines"])
            self.assertEqual(len(calls), count, "disk cache must avoid a second provider call")
            self.assertTrue(json.loads((Path(folder) / "5.json").read_text())["synced"])


if __name__ == "__main__":
    unittest.main()
