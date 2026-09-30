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
from types import SimpleNamespace
from typing import Any

import httpx

from app.services.music_lyrics import parse_lyrics
from app.services.music_enrichment import (
    MusicEnrichmentService,
    _ProviderFailure,
    _candidate as catalog_candidate,
    _match as catalog_match,
    _signature as catalog_signature,
)
from app.services.music_query import clean_search_text, search_names

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
    r"\s*[\(\[\{](?:official|offical|lyric|lyrics|audio|video|music video|hd|hq|4k|"
    r"explicit|clean|visuali[sz]er|премьера|клип|official\s+\w+)[^\)\]\}]*[\)\]\}]", re.I)
_FEAT = re.compile(r"\s*[\(\[]?\s*(?:feat\.?|ft\.?|featuring|при уч\.?)\s+[^\)\]]*[\)\]]?", re.I)
_TRACKNO = re.compile(r"^\s*(?:\d{1,3}\s*[-._)]\s+|\d{1,3}\s+(?=\D))")
_EXT = re.compile(r"\.(mp3|m4a|flac|wav|ogg|opus|aac)$", re.I)
_UNKNOWN = {"", "unknown", "unknown artist", "various artists", "неизвестен", "неизвестный исполнитель", "<unknown>"}


def clean(value: Any) -> str:
    # Site tags ("[mp3xa.cc]"), bare domains and upload noise first; query only.
    text = clean_search_text(_EXT.sub("", str(value or "")))
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
    # Best guess first: cleaned names with "Artist - Title" split out of the title.
    guessed_artist, guessed_title = search_names(title_s, artist_s)
    if guessed_title and guessed_artist:
        add(guessed_artist, guessed_title)
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
    # A provider row can carry perfectly valid LRC for a different, much
    # longer recording. Never stretch or expose timestamps beyond this track.
    if duration and lines and lines[-1]["start"] > duration + 2:
        lines = []
    if lines:
        return {"status": "synced", "synced": True, "lines": lines, "text": "\n".join(l["text"] for l in lines),
                "source": "lrclib", "source_id": row.get("id")}
    if plain.strip():
        return {"status": "plain", "synced": False, "lines": [], "text": plain.strip()[:64000], "source": "lrclib", "source_id": row.get("id")}
    return None


def _norm(value: str) -> str:
    return re.sub(r"[^\w]+", "", clean(value).lower())


def _candidate_from_row(row: dict) -> dict | None:
    if not isinstance(row, dict) or type(row.get("id")) is not int or not 0 < row["id"] < 2**63:
        return None
    title, artist, album = (str(row.get(key) or "").strip() for key in ("trackName", "artistName", "albumName"))
    if not title or not artist or max(len(title), len(artist), len(album)) > 240:
        return None
    try:
        duration = float(row.get("duration") or 0)
    except (TypeError, ValueError):
        return None
    if not 0 < duration <= 86400:
        return None
    return catalog_candidate(title, artist, album, duration, "lrclib", row["id"])


def _verified_row(row: dict | None, signature: dict) -> dict | None:
    candidate = _candidate_from_row(row) if isinstance(row, dict) else None
    if candidate is None or catalog_match(signature, candidate) != "matched":
        return None
    return row


def _lyric_identity(row: dict) -> tuple:
    """Duplicates are safe only when their actual lyric payload is identical."""
    return (bool(row.get("instrumental")), str(row.get("syncedLyrics") or "").strip(),
            str(row.get("plainLyrics") or "").strip())


def _pick_for_signature(rows: list, signature: dict) -> dict | None:
    """Return one verified recording; conflicting provider rows stay ambiguous."""
    matched: list[tuple[dict, dict]] = []
    for row in rows if isinstance(rows, list) else []:
        candidate = _candidate_from_row(row)
        if candidate is not None and catalog_match(signature, candidate) == "matched":
            matched.append((row, candidate))
    if not matched:
        return None
    identities = {_lyric_identity(row) for row, _ in matched}
    if len(identities) != 1:
        return None
    return min(matched, key=lambda pair: abs(pair[1]["duration"] - signature["duration"]))[0]


def pick(rows: list, artist: str, title: str, duration: float, album: str = "", is_excerpt: bool = False) -> dict | None:
    """Compatibility wrapper used by tests and callers outside the service."""
    signature = catalog_signature(SimpleNamespace(title=title, artist=artist, album=album,
                                                   duration=duration, is_excerpt=is_excerpt))
    return _pick_for_signature(rows, signature)


class LrclibClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        # Reuse the hardened provider reader: bounded response bodies, no
        # redirects/compression, Retry-After backoff and request pacing.
        self._provider = MusicEnrichmentService(transport=transport)
        self._lock = asyncio.Lock()

    async def _get(self, path, params):
        return await self._provider._json(LRCLIB + path, params)

    async def lookup(self, title, artist, album, duration, filename="", *, is_excerpt: bool = False,
                     budget: float = 18) -> dict:
        """Bounded total time so the phone's 25 s request never times out."""
        try:
            return await asyncio.wait_for(self._serialized_lookup(title, artist, album, duration, filename,
                                                                   is_excerpt=is_excerpt), budget)
        except (asyncio.TimeoutError, TimeoutError):
            return {"status": "unavailable", "synced": False, "lines": [], "text": ""}

    async def _serialized_lookup(self, title, artist, album, duration, filename="", *, is_excerpt=False) -> dict:
        async with self._lock:
            return await self._lookup(title, artist, album, duration, filename, is_excerpt=is_excerpt)

    async def _lookup(self, title, artist, album, duration, filename="", *, is_excerpt=False) -> dict:
        duration = float(duration or 0)
        signature = catalog_signature(SimpleNamespace(title=title, artist=artist, album=album, duration=duration,
                                                       filename=filename, is_excerpt=is_excerpt))
        if not signature["artist"] or not signature["title"] or not signature["duration"] or signature["cut"]:
            return {"status": "insufficient_metadata", "synced": False, "lines": [], "text": ""}
        guesses = queries(title, artist, filename)
        if not guesses:
            return {"status": "insufficient_metadata", "synced": False, "lines": [], "text": ""}
        fallback = None
        try:
            for guessed_artist, guessed_title in guesses:
                if guessed_artist:
                    params = {"artist_name": guessed_artist, "track_name": guessed_title}
                    if duration > 0:
                        params["duration"] = int(round(duration))
                    if album and clean(album):
                        row = _verified_row(await self._get("/get", {**params, "album_name": clean(album)}), signature)
                        found = result_from_row(row, duration) if row else None
                        if found and found["status"] in {"synced", "instrumental"}:
                            return found
                        fallback = fallback or found
                    row = _verified_row(await self._get("/get", params), signature)
                    found = result_from_row(row, duration) if row else None
                    if found and found["status"] in {"synced", "instrumental"}:
                        return found
                    fallback = fallback or found
                    rows = await self._get("/search", {"track_name": guessed_title, "artist_name": guessed_artist})
                    row = _pick_for_signature(rows or [], signature)
                    found = result_from_row(row, duration) if row else None
                    if found and found["synced"]:
                        return found
                    fallback = fallback or found
                rows = await self._get("/search", {"q": f"{guessed_artist} {guessed_title}".strip()})
                row = _pick_for_signature(rows or [], signature)
                found = result_from_row(row, duration) if row else None
                if found and found["synced"]:
                    return found
                fallback = fallback or found
        except (_ProviderFailure, httpx.HTTPError, ValueError, OSError) as exc:
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


PC_TRANSCRIPTION_LABEL = "Автоматически, может быть с ошибками"


def public(value: dict, track_id: int) -> dict:
    out = {"track_id": track_id, "status": value.get("status", "not_found"), "synced": bool(value.get("synced")),
           "source": value.get("source") or "none", "lines": value.get("lines") or [], "text": value.get("text") or ""}
    if out["source"] == "pc_transcription":
        out.update(automatic=True, label=PC_TRANSCRIPTION_LABEL)
    return out


def from_transcription(value: dict | None) -> dict | None:
    """Finished PC transcription job result: already [{start, end, text}]."""
    if not isinstance(value, dict):
        return None
    lines = [row for row in (value.get("lines") or [])[:MAX_LINES] if isinstance(row, dict) and str(row.get("text") or "").strip()]
    if not lines:
        return None
    return {"status": "synced", "synced": True, "lines": lines, "text": "\n".join(str(row["text"]) for row in lines),
            "source": "pc_transcription"}


async def resolve(track, *, owner: dict | None, embedded: dict | None, enrichment: dict | None,
                  cache: LyricsCache, client: LrclibClient, refresh: bool = False,
                  transcription: dict | None = None) -> dict:
    """Order: owner/embedded/catalog timed lines → LRCLIB → PC transcription → plain text.

    The on-device iPhone transcription (owner, hidden fallback) yields to a
    finished PC transcription, which is generally much better on rap.
    """
    duration = float(getattr(track, "duration", 0) or 0)
    automatic = from_transcription(transcription)
    if automatic and isinstance(owner, dict) and owner.get("source") == "on_device_transcription":
        owner = None
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
                                         getattr(track, "filename", ""),
                                         is_excerpt=getattr(track, "is_excerpt", False) is True)
            cached = cache.put(track.id, print_, looked)
    if cached.get("synced"):
        return public(cached, track.id)
    # A finished transcription is real vocal text. An "instrumental" catalog
    # flag must not hide it, and neither must unsynced filler.
    if automatic:
        return public(automatic, track.id)
    if cached.get("status") == "instrumental":
        return public(cached, track.id)
    if plain:
        return public(plain, track.id)
    return public(cached, track.id)
