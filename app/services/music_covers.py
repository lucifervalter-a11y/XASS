"""Album cover for a track whose file has no embedded picture.

The image host and path are built here from a 32-character hash. A URL in the
provider response is never followed.
"""
from __future__ import annotations

import asyncio
import re
import time

import httpx

from app.services.music_artwork import _jpeg_thumbnail
from app.services.music_enrichment import USER_AGENT, _norm
from app.services.music_query import is_unknown_artist, search_names

_DIGEST = re.compile(r"[0-9a-f]{32}")
_MISS_SECONDS = 6 * 3600
_MAX_IMAGE = 2_000_000
# Cyrillic ё and the Latin ë some catalogs use in the same titles.
_YO = str.maketrans({"ё": "е", "Ё": "е", "ë": "е", "Ë": "е"})
# Dropped only for the cover comparison. A remix stays a different recording.
_COVER_NOISE = frozenset({"slowed", "reverb", "sped", "spedup", "nightcore", "slow", "super"})
_EXTRA_REJECT = frozenset({"remix", "live", "edit", "cover", "radio", "extended", "instrumental",
                           "karaoke", "demo", "acoustic", "remaster", "remastered"})
_misses: dict[str, float] = {}
_hits: dict[str, bytes] = {}
_locks: dict[str, asyncio.Lock] = {}
_slots = asyncio.Semaphore(4)


def cover_url(digest: str) -> str | None:
    if not _DIGEST.fullmatch(digest or ""):
        return None
    return f"https://cdn-images.dzcdn.net/images/cover/{digest}/500x500-000000-80-0-0.jpg"


def _cover_text(value: str) -> str:
    words = [word for word in _norm(value).translate(_YO).split() if word not in _COVER_NOISE]
    return " ".join(words)


def _titles_match(want: str, got: str) -> bool:
    """Same title, or one short trailing word such as "vamp" on a long title."""
    if not want or not got:
        return False
    if want == got:
        return True
    short, long = (want, got) if len(want) <= len(got) else (got, want)
    if len(short) < 12 or not long.startswith(short + " "):
        return False
    extra = long[len(short) + 1:]
    return " " not in extra and extra not in _EXTRA_REJECT and len(extra) <= 12


def cover_queries(artist: str, title: str) -> list[str]:
    """Track search, then the title alone. The artist is still required when picking."""
    first = f"{artist} {title}".strip()[:300]
    second = title.strip()[:300]
    if second and _norm(second) != _norm(first):
        return [first, second]
    return [first] if first else []


def pick_cover_digest(rows, title: str, artist: str, album: str = "") -> str | None:
    """Same artist and title. A matching album wins; a remix does not."""
    want_title, want_artist, want_album = _cover_text(title), _cover_text(artist), _norm(album)
    if not want_title or not want_artist:
        return None
    fallback = None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        names = {_cover_text(str(row.get("title") or "")), _cover_text(str(row.get("title_short") or ""))}
        got_artist = _cover_text((row.get("artist") or {}).get("name") if isinstance(row.get("artist"), dict) else "")
        if got_artist != want_artist or not any(_titles_match(want_title, name) for name in names):
            continue
        album_row = row.get("album") if isinstance(row.get("album"), dict) else {}
        digest = str(album_row.get("md5_image") or "")
        if cover_url(digest) is None:
            continue
        if want_album and _norm(album_row.get("title")) == want_album:
            return digest
        fallback = fallback or digest
    return fallback


async def _download(client: httpx.AsyncClient, url: str) -> bytes | None:
    async with client.stream("GET", url, headers={"Accept": "image/jpeg,image/png", "Accept-Encoding": "identity"}) as response:
        if response.status_code != 200 or response.is_redirect:
            return None
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            return None
        length = response.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > _MAX_IMAGE):
            return None
        chunks, size = [], 0
        async for chunk in response.aiter_bytes(16384):
            size += len(chunk)
            if size > _MAX_IMAGE:
                return None
            chunks.append(chunk)
    return b"".join(chunks)


async def display_cover(title: str, artist: str, album: str = "") -> bytes | None:
    artist, title = search_names(title, artist)
    if is_unknown_artist(artist) or not _norm(title):
        return None
    key = f"{_norm(artist)}\n{_norm(title)}\n{_norm(album)}"
    now = time.monotonic()
    if _misses.get(key, 0) > now:
        return None
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _hits.get(key)
        if cached:
            return cached
        if _misses.get(key, 0) > time.monotonic():
            return None
        jpeg = None
        try:
            async with _slots:
                async with httpx.AsyncClient(timeout=httpx.Timeout(6, connect=3), trust_env=False,
                        follow_redirects=False, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
                    rows = []
                    for query in cover_queries(artist, title):
                        response = await client.get("https://api.deezer.com/search", params={"q": query, "limit": 15})
                        if response.status_code != 200:
                            continue
                        payload = response.json()
                        found = payload.get("data") if isinstance(payload, dict) else None
                        if isinstance(found, list):
                            rows.extend(found)
                        digest = pick_cover_digest(rows, title, artist, album)
                        if digest:
                            break
                    else:
                        digest = None
                    url = cover_url(digest or "")
                    if url:
                        raw = await _download(client, url)
                        jpeg = _jpeg_thumbnail(raw) if raw else None
        except (httpx.HTTPError, OSError, ValueError, TypeError):
            jpeg = None
        if jpeg:
            if len(_hits) > 500:
                _hits.clear()
            _hits[key] = jpeg
        else:
            _misses[key] = time.monotonic() + _MISS_SECONDS
            if len(_misses) > 4000:
                _misses.clear()
        return jpeg
