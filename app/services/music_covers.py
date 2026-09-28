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
_misses: dict[str, float] = {}
_hits: dict[str, bytes] = {}
_locks: dict[str, asyncio.Lock] = {}
_slots = asyncio.Semaphore(4)


def cover_url(digest: str) -> str | None:
    if not _DIGEST.fullmatch(digest or ""):
        return None
    return f"https://cdn-images.dzcdn.net/images/cover/{digest}/500x500-000000-80-0-0.jpg"


def pick_cover_digest(rows, title: str, artist: str, album: str = "") -> str | None:
    """Exact title and artist. A matching album wins; a remix does not."""
    want_title, want_artist, want_album = _norm(title), _norm(artist), _norm(album)
    if not want_title or not want_artist:
        return None
    fallback = None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        names = {_norm(row.get("title")), _norm(row.get("title_short"))}
        got_artist = _norm((row.get("artist") or {}).get("name") if isinstance(row.get("artist"), dict) else "")
        if want_title not in names or got_artist != want_artist:
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
                    response = await client.get("https://api.deezer.com/search", params={"q": f"{artist} {title}"[:300], "limit": 10})
                    if response.status_code == 200:
                        payload = response.json()
                        digest = pick_cover_digest(payload.get("data") if isinstance(payload, dict) else None, title, artist, album)
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
