"""Time-synced lyrics (Apple Music style) for every track, with a server cache.

Order: owner/embedded LRC → cached result → LRCLIB exact get → LRCLIB search.
LRCLIB (https://lrclib.net/docs) is free and needs no key. Only public
metadata (artist, title, album, duration) leaves the server, never audio.
Results are cached per track fingerprint so the phone can prefetch on every
play without hammering the provider.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from app.services.music_lyrics import parse_lyrics

LOG = logging.getLogger(__name__)
USER_AGENT = "XASS-SyncedLyrics/1.0 (https://github.com/lucifervalter-a11y/XASS)"
LRCLIB = "https://lrclib.net/api"
FOUND_TTL = 30 * 86400
MISS_TTL = 12 * 3600
UNAVAILABLE_TTL = 10 * 60
MAX_LINES = 2000
_memory: dict[str, dict] = {}
_locks: dict[int, asyncio.Lock] = {}

_NOISE = re.compile(
    r"\s*[\(\[\{](?:official|offical|lyric|lyrics|audio|video|music video|hd|hq|4k|remaster(?:ed)?[^\)\]\}]*|"
    r"explicit|clean|visuali[sz]er|премьера|клип|official\s+\w+)[^\)\]\}]*[\)\]\}]", re.I)
_FEAT = re.compile(r"\s*[\(\[]?\s*(?:feat\.?|ft\.?|featuring|при уч\.?)\s+[^\)\]]*[\)\]]?", re.I)
_TRACKNO = re.compile(r"^\s*(?:\d{1,3}\s*[-._)]\s+|\d{1,3}\s+(?=\D))")
_EXT = re.compile(r"\.(mp3|m4a|flac|wav|ogg|opus|aac)$", re.I)
_UNKNOWN = {"", "unknown", "unknown artist", "various artists", "неизвестен", "неизвестный исполнитель", "<unknown>"}


def clean(value: Any) -> str:
    text = _EXT.sub("", str(value or "")).replace("_", " ")
    text = _NOISE.sub("", text)
    text = _FEAT.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" -–—.")


def queries(title: Any, artist: Any, filename: Any = "") -> list[tuple[str, str]]:
    """Ordered (artist, title) guesses, including "Artist - Title" filenames."""
    out: list[tuple[str, str]] = []

    def add(a: str, t: str):
        a, t = clean(a), clean(_TRACKNO.sub("", t))
        if t and (a, t) not in out:
            out.append((a, t))

    title_s, artist_s = str(title or ""), str(artist or "")
    if artist_s.strip().lower() not in _UNKNOWN:
        add(artist_s, title_s)
    for raw in (title_s, Path(str(filename or "")).stem):
        raw = _TRACKNO.sub("", _EXT.sub("", raw).replace("_", " "))
        for sep in (" - ", " – ", " — "):
            if sep in raw:
                left, right = raw.split(sep, 1)
                add(left, right)
                if artist_s.strip().lower() not in _UNKNOWN:
                    add(artist_s, right)
                break
    if artist_s.strip().lower() in _UNKNOWN:
        add("", title_s)
    return out[:4]


def timed_lines(lrc: str, duration: float) -> list[dict]:
    """LRC → [{start, end, text}]; blank rows only end the previous line."""
    parsed = parse_lyrics(lrc or "")
    rows = sorted(parsed.get("lines") or [], key=lambda row: row["time"])
    out: list[dict] = []
    for index, row in enumerate(rows):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start = float(row["time"])
        following = next((float(r["time"]) for r in rows[index + 1:] if float(r["time"]) > start), None)
        end = following if following is not None else (min(start + 8, duration) if duration and duration > start else start + 8)
        out.append({"start": round(start, 3), "end": round(max(end, start + 0.2), 3), "text": text[:500]})
        if len(out) >= MAX_LINES:
            break
    return out


def result_from_row(row: dict, duration: float) -> dict | None:
    if not isinstance(row, dict):
        return None
    if row.get("instrumental"):
        return {"status": "instrumental", "synced": False, "lines": [], "text": "", "source": "lrclib", "source_id": row.get("id")}
    synced = row.get("syncedLyrics") if isinstance(row.get("syncedLyrics"), str) else ""
    plain = row.get("plainLyrics") if isinstance(row.get("plainLyrics"), str) else ""
    lines = timed_lines(synced, duration) if synced else []
    if lines:
        return {"status": "synced", "synced": True, "lines": lines, "text": "\n".join(l["text"] for l in lines),
                "source": "lrclib", "source_id": row.get("id")}
    if plain.strip():
        return {"status": "plain", "synced": False, "lines": [], "text": plain.strip()[:64000], "source": "lrclib", "source_id": row.get("id")}
    return None


def _norm(value: str) -> str:
    return re.sub(r"[^\w]+", "", clean(value).lower())


def pick(rows: list, artist: str, title: str, duration: float) -> dict | None:
    """Best search row: title must match; prefer synced and nearest duration."""
    want_t, want_a = _norm(title), _norm(artist)
    best, best_score = None, None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        got_t = _norm(row.get("trackName") or row.get("name") or "")
        got_a = _norm(row.get("artistName") or "")
        if not got_t or not want_t or not (got_t == want_t or want_t in got_t or got_t in want_t):
            continue
        if want_a and got_a and not (want_a in got_a or got_a in want_a):
            continue
        diff = abs(float(row.get("duration") or 0) - duration) if duration else 0
        if duration and diff > 12:
            continue
        synced = bool(row.get("syncedLyrics"))
        score = (0 if synced else 1, 0 if got_t == want_t else 1, diff)
        if best_score is None or score < best_score:
            best, best_score = row, score
    return best


class LrclibClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    async def _get(self, client, path, params):
        response = await client.get(LRCLIB + path, params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def lookup(self, title, artist, album, duration, filename="", *, budget: float = 18) -> dict:
        """Bounded total time so the phone's 25 s request never times out."""
        try:
            return await asyncio.wait_for(self._lookup(title, artist, album, duration, filename), budget)
        except (asyncio.TimeoutError, TimeoutError):
            return {"status": "unavailable", "synced": False, "lines": [], "text": ""}

    async def _lookup(self, title, artist, album, duration, filename="") -> dict:
        duration = float(duration or 0)
        guesses = queries(title, artist, filename)
        if not guesses:
            return {"status": "insufficient_metadata", "synced": False, "lines": [], "text": ""}
        fallback = None
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(7, connect=4), trust_env=False,
                                         follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
                for guessed_artist, guessed_title in guesses:
                    if guessed_artist:
                        params = {"artist_name": guessed_artist, "track_name": guessed_title}
                        if duration > 0:
                            params["duration"] = int(round(duration))
                        if album and clean(album):
                            found = result_from_row(await self._get(client, "/get", {**params, "album_name": clean(album)}), duration)
                            if found and found["synced"]:
                                return found
                            fallback = fallback or found
                        found = result_from_row(await self._get(client, "/get", params), duration)
                        if found and found["status"] in {"synced", "instrumental"}:
                            return found
                        fallback = fallback or found
                        rows = await self._get(client, "/search", {"track_name": guessed_title, "artist_name": guessed_artist})
                        found = result_from_row(pick(rows or [], guessed_artist, guessed_title, duration), duration)
                        if found and found["synced"]:
                            return found
                        fallback = fallback or found
                    rows = await self._get(client, "/search", {"q": f"{guessed_artist} {guessed_title}".strip()})
                    found = result_from_row(pick(rows or [], guessed_artist, guessed_title, duration), duration)
                    if found and found["synced"]:
                        return found
                    fallback = fallback or found
        except (httpx.HTTPError, ValueError, OSError) as exc:
            LOG.info("LRCLIB lookup unavailable: %s", type(exc).__name__)
            if fallback:
                return fallback
            return {"status": "unavailable", "synced": False, "lines": [], "text": ""}
        return fallback or {"status": "not_found", "synced": False, "lines": [], "text": ""}


def fingerprint(track) -> str:
    raw = "|".join(str(getattr(track, key, "") or "") for key in ("sha256", "title", "artist", "album", "duration"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class LyricsCache:
    def __init__(self, directory: Path | None):
        self.directory = directory
        if directory is not None:
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError:
                self.directory = None

    def _path(self, track_id: int) -> Path | None:
        return self.directory / f"{int(track_id)}.json" if self.directory else None

    def get(self, track_id: int, print_: str) -> dict | None:
        value = _memory.get(f"{track_id}:{print_}")
        path = self._path(track_id)
        if value is None and path and path.is_file():
            try:
                value = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                value = None
        if not isinstance(value, dict) or value.get("fingerprint") != print_:
            return None
        ttl = FOUND_TTL if value.get("status") in {"synced", "plain", "instrumental"} else (
            UNAVAILABLE_TTL if value.get("status") == "unavailable" else MISS_TTL)
        if time.time() - float(value.get("cached_at") or 0) > ttl:
            return None
        return value

    def put(self, track_id: int, print_: str, value: dict) -> dict:
        stored = {**value, "fingerprint": print_, "cached_at": time.time()}
        _memory[f"{track_id}:{print_}"] = stored
        if len(_memory) > 2000:
            _memory.pop(next(iter(_memory)))
        path = self._path(track_id)
        if path:
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(stored, ensure_ascii=False), "utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
        return stored


def from_existing(lyrics: dict | None, duration: float, source: str) -> dict | None:
    """Owner/embedded/enrichment payloads ({text, lines:[{time,text}], synced})."""
    if not isinstance(lyrics, dict) or lyrics.get("disabled"):
        return None
    rows = lyrics.get("lines") or []
    if lyrics.get("synced") and rows:
        lrc = "\n".join(f"[{int(r['time'] // 60):02d}:{r['time'] % 60:05.2f}]{r.get('text') or ''}"
                        for r in rows if isinstance(r, dict) and isinstance(r.get("time"), (int, float)))
        lines = timed_lines(lrc, duration)
        if lines:
            return {"status": "synced", "synced": True, "lines": lines, "text": "\n".join(l["text"] for l in lines),
                    "source": lyrics.get("source") or source}
    text = str(lyrics.get("text") or "").strip()
    if text:
        return {"status": "plain", "synced": False, "lines": [], "text": text[:64000], "source": lyrics.get("source") or source}
    return None


def public(value: dict, track_id: int) -> dict:
    return {"track_id": track_id, "status": value.get("status", "not_found"), "synced": bool(value.get("synced")),
            "source": value.get("source") or "none", "lines": value.get("lines") or [], "text": value.get("text") or ""}


async def resolve(track, *, owner: dict | None, embedded: dict | None, enrichment: dict | None,
                  cache: LyricsCache, client: LrclibClient, refresh: bool = False) -> dict:
    duration = float(getattr(track, "duration", 0) or 0)
    plain = None
    for payload, source in ((owner, "owner"), (embedded, "embedded"), (enrichment, "lrclib")):
        found = from_existing(payload, duration, source)
        if found and found["synced"]:
            return public(found, track.id)
        plain = plain or found
    lock = _locks.setdefault(track.id, asyncio.Lock())
    async with lock:
        print_ = fingerprint(track)
        cached = None if refresh else cache.get(track.id, print_)
        if cached is None:
            looked = await client.lookup(track.title, track.artist, getattr(track, "album", ""), duration,
                                         getattr(track, "filename", ""))
            cached = cache.put(track.id, print_, looked)
    if cached.get("synced") or cached.get("status") == "instrumental":
        return public(cached, track.id)
    if plain:
        return public(plain, track.id)
    return public(cached, track.id)
