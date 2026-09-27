"""Conservative, metadata-only public-catalog lookup. Never send or edit audio.

Providers: https://lrclib.net/docs and https://musicbrainz.org/doc/MusicBrainz_API
Artwork: https://musicbrainz.org/doc/Cover_Art_Archive/API
The owner-facing caller persists results and must not auto-apply `candidate` or
`ambiguous` results. A cut can suggest metadata, but never full-track lyrics.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
import json
import math
import re
import time
from types import SimpleNamespace
import unicodedata
from urllib.parse import urlsplit

import httpx

from app.services.music_artwork import _jpeg_thumbnail
from app.services.music_lyrics import empty_lyrics, parse_lyrics

USER_AGENT = "XASS-MusicEnrichment/1.0 (https://github.com/lucifervalter-a11y/XASS)"
MAX_JSON_BYTES = 1024 * 1024
MAX_ARTWORK_BYTES = 2 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 384 * 1024
MAX_RECORDS = 100
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_VARIANTS = re.compile(r"\b(remix|live|acoustic|instrumental|karaoke|demo|edit|remaster(?:ed)?|sped|slowed|nightcore|cover|radio|extended|cut\w*|обрез\w*|ремикс|кавер|минус)\b", re.I)
_CUT = re.compile(r"\b(?:cut(?:\d+(?:sec|s)?)?|clip|snippet|обрез\w*)\b", re.I)
_REUPLOAD = re.compile(r"\b(?:reuploads?|re-uploads?)\b", re.I)


def _text(value):
    if not isinstance(value, str) or len(value) > 480:
        return ""
    return " ".join("".join(c for c in value if ord(c) >= 32 and unicodedata.category(c) not in {"Cf", "Cs"}).split())


def _norm(value):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", _text(value)).casefold()))


def _duration(value, *, scale=1):
    if isinstance(value, bool):
        return 0
    try:
        number = float(value) / scale
        return number if math.isfinite(number) and 0 < number <= 86400 else 0
    except (TypeError, ValueError):
        return 0


def _signature(track):
    title, artist, album = (_text(getattr(track, field, "")) for field in ("title", "artist", "album"))
    # Keep variant qualifiers intact. Only remove explicit upload/cut labels for
    # discovery; the cut flag still forbids automatic lyrics or metadata.
    cut = getattr(track, "is_excerpt", False) is True or bool(_CUT.search(title))
    cleaned = _REUPLOAD.sub(" ", _CUT.sub(" ", title)).strip(" -_()[]")
    if not artist and len(parts := re.split(r"\s+[-–—]\s+", cleaned)) == 2:
        artist, cleaned = map(str.strip, parts)
    return {"title": cleaned, "artist": artist, "album": album, "duration": _duration(getattr(track, "duration", 0)),
            "cut": cut, "query": cleaned if not artist else f"{artist} {cleaned}"}


def _empty(status, reason="", **extra):
    return {"status": status, "reason": reason, "candidate": None, "candidates": [], "lyrics": empty_lyrics(),
            "artwork": {"status": "not_found"}, "provenance": [], **extra}


def _candidate(title, artist, album, duration, source, identity):
    return {"title": title, "artist": artist, "album": album, "duration": duration,
            "source": source, "source_id": identity,
            "source_url": f"https://lrclib.net/lyrics/{identity}" if source == "lrclib" else f"https://musicbrainz.org/recording/{identity}"}


def _match(signature, candidate):
    title, artist = _norm(candidate["title"]), _norm(candidate["artist"])
    if not title or not artist:
        return "reject"
    wanted, known_artist = _norm(signature["title"]), _norm(signature["artist"])
    variant_title = wanted if known_artist else wanted.replace(artist, "", 1).strip()
    if set(_VARIANTS.findall(variant_title)) != set(_VARIANTS.findall(title)):
        return "reject"
    if known_artist:
        if signature["album"] and candidate["album"] and _norm(signature["album"]) != _norm(candidate["album"]):
            return "reject"
        confident = wanted == title and known_artist == artist
        if not confident and (SequenceMatcher(None, wanted, title, autojunk=False).ratio() < .8
                or SequenceMatcher(None, known_artist, artist, autojunk=False).ratio() < .85):
            return "reject"
    else:
        # Unknown filename text is discovery, not an instruction to guess an
        # artist. Require both names in it for automatic attribution.
        confident = wanted in {f"{artist} {title}", f"{title} {artist}"}
        if not confident and max(SequenceMatcher(None, wanted, value, autojunk=False).ratio()
                for value in (title, f"{artist} {title}", f"{title} {artist}")) < .85:
            return "reject"
    if signature["cut"]:
        return "duration_mismatch"
    if not confident:
        return "metadata_needs_confirmation"
    duration = candidate["duration"]
    if not duration or abs(duration - signature["duration"]) > min(2, signature["duration"] * .01):
        return "duration_mismatch"
    return "matched"


def _lrclib_rows(data):
    if not isinstance(data, list) or len(data) > MAX_RECORDS:
        raise ValueError("Invalid provider response")
    rows = []
    for row in data:
        if not isinstance(row, dict) or type(row.get("id")) is not int or not 0 < row["id"] < 2**63:
            continue
        title, artist, album = (_text(row.get(key)) for key in ("trackName", "artistName", "albumName"))
        if not title or not artist or max(len(title), len(artist), len(album)) > 240:
            continue
        candidate = _candidate(title, artist, album, _duration(row.get("duration")), "lrclib", row["id"])
        rows.append((candidate, row))
    return rows


def _musicbrainz_rows(data, album=""):
    if not isinstance(data, dict) or not isinstance(data.get("recordings"), list) or len(data["recordings"]) > MAX_RECORDS:
        raise ValueError("Invalid provider response")
    rows = []
    for row in data["recordings"]:
        if not isinstance(row, dict) or not re.fullmatch(_UUID, str(row.get("id", ""))) or row.get("video") is True:
            continue
        credits = row.get("artist-credit")
        if not isinstance(credits, list) or len(credits) > 16:
            continue
        artist = "".join(_text(credit.get("name") or (credit.get("artist") or {}).get("name")) +
            (credit.get("joinphrase", "") if isinstance(credit.get("joinphrase", ""), str) else "")
            for credit in credits if isinstance(credit, dict) and isinstance(credit.get("artist", {}), dict))
        title, artist = _text(row.get("title")), _text(artist)
        if not title or not artist or max(len(title), len(artist)) > 240:
            continue
        # Disambiguation sometimes carries a version absent from the title.
        if set(_VARIANTS.findall(_norm(row.get("disambiguation")))) - set(_VARIANTS.findall(_norm(title))):
            continue
        releases = row.get("releases", [])
        if not isinstance(releases, list) or len(releases) > 100:
            continue
        albums = {_text(value.get("title")) for value in releases if isinstance(value, dict) and _text(value.get("title"))}
        if album and not any(_norm(value) == _norm(album) for value in albums):
            continue
        candidate_album = next((value for value in sorted(albums) if _norm(value) == _norm(album)), "") if album else next(iter(albums)) if len(albums) == 1 else ""
        candidate = _candidate(title, artist, candidate_album, _duration(row.get("length"), scale=1000), "musicbrainz", row["id"])
        rows.append((candidate, row))
    return rows


def _select(signature, rows):
    eligible, suggestions = {}, {}
    for candidate, raw in rows:
        result = _match(signature, candidate)
        key = candidate["source_id"]
        if result == "matched":
            eligible[key] = (candidate, raw)
        elif result != "reject":
            suggestions[key] = (candidate, raw, result)
    if len(eligible) == 1:
        return "matched", next(iter(eligible.values())), []
    if len(eligible) > 1:
        return "ambiguous", None, [pair[0] for pair in list(eligible.values())[:3]]
    if len(suggestions) == 1:
        candidate, raw, reason = next(iter(suggestions.values()))
        return reason, (candidate, raw), [candidate]
    return ("ambiguous", None, [pair[0] for pair in list(suggestions.values())[:3]]) if suggestions else ("not_found", None, [])


def _lyrics(raw, candidate):
    if raw.get("instrumental") is True:
        return {**empty_lyrics(), "status": "instrumental"}
    for field in ("syncedLyrics", "plainLyrics"):
        value = raw.get(field)
        parsed = parse_lyrics(value) if isinstance(value, str) else empty_lyrics()
        if parsed["text"]:
            # A matching duration is not sufficient when the provider's LRC
            # plainly belongs to a longer recording. Never stretch timestamps.
            if parsed["lines"] and parsed["lines"][-1]["time"] > candidate["duration"] + 2:
                continue
            return {**parsed, "source": "lrclib", "source_url": candidate["source_url"]}
    return empty_lyrics()


def _artwork(raw, album):
    releases = raw.get("releases")
    if not isinstance(releases, list) or len(releases) > 100:
        return {"status": "not_found"}
    releases = [row for row in releases if isinstance(row, dict) and re.fullmatch(_UUID, str(row.get("id", "")))
        and row.get("status") == "Official" and _text(row.get("title"))
        and (not album or _norm(row.get("title")) == _norm(album))]
    # Different albums/compilations are not interchangeable artwork.
    if not releases or len({_norm(row["title"]) for row in releases}) != 1:
        return {"status": "not_found"}
    release = min(releases, key=lambda row: (_text(row.get("date")) or "9999", row["id"]))
    identity = release["id"]
    return {"status": "candidate", "source": "coverartarchive", "release_id": identity,
            "url": f"https://coverartarchive.org/release/{identity}/front-500",
            "source_url": f"https://musicbrainz.org/release/{identity}"}


class _ProviderFailure(Exception):
    def __init__(self, status, retry_after=0):
        self.status, self.retry_after = status, retry_after


class MusicEnrichmentService:
    """Single-process serialized provider access, TTL/LRU and backoff.

    Deploy one worker (the existing XASS deployment). Multiple worker processes
    must coordinate throttling externally rather than multiplying provider RPS.
    The injectable transport/clock are for tests; API hosts are never configurable.
    """
    def __init__(self, *, transport=None, clock=time.monotonic, wall_clock=time.time, sleep=asyncio.sleep):
        self._transport, self._clock, self._wall_clock, self._sleep = transport, clock, wall_clock, sleep
        self._lock = asyncio.Lock()
        self._next = 0.0
        self._blocked = {}
        self._cache = OrderedDict()
        self._cache_bytes = 0

    async def _request(self, url, *, params=None, maximum=MAX_JSON_BYTES, redirects=None):
        host = urlsplit(url).hostname
        remaining = self._blocked.get(host, 0) - self._clock()
        if remaining > 0:
            raise _ProviderFailure("rate_limited", math.ceil(remaining))
        await self._sleep(max(0, self._next - self._clock()))
        try:
            async with asyncio.timeout(12):
                async with httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(8, connect=4),
                        trust_env=False, follow_redirects=False, headers={"User-Agent": USER_AGENT,
                        "Accept": "application/json" if maximum == MAX_JSON_BYTES else "image/jpeg,image/png,image/gif",
                        "Accept-Encoding": "identity"}) as client:
                    for hop in range(4):
                        async with client.stream("GET", url, params=params if hop == 0 else None) as response:
                            if response.status_code in {429, 503}:
                                value = response.headers.get("retry-after", "60")
                                try:
                                    delay = int(value) if value.isdigit() and len(value) < 12 else parsedate_to_datetime(value).timestamp() - self._wall_clock()
                                    delay = max(1, delay)
                                except (ValueError, TypeError, OverflowError):
                                    delay = 60
                                self._blocked[host] = self._clock() + delay
                                raise _ProviderFailure("rate_limited", math.ceil(delay))
                            if response.status_code == 404:
                                return None
                            if response.is_redirect:
                                next_url = response.headers.get("location", "")
                                if not redirects or hop == 3 or not redirects(next_url):
                                    raise _ProviderFailure("unavailable")
                                url = next_url
                                continue
                            if response.status_code != 200 or response.headers.get("content-encoding", "identity").lower() != "identity":
                                raise _ProviderFailure("unavailable")
                            length = response.headers.get("content-length")
                            if length and (not length.isdigit() or len(length) > 10 or int(length) > maximum):
                                raise _ProviderFailure("unavailable")
                            chunks, size = [], 0
                            async for chunk in response.aiter_bytes(chunk_size=16384):
                                size += len(chunk)
                                if size > maximum:
                                    raise _ProviderFailure("unavailable")
                                chunks.append(chunk)
                            return b"".join(chunks)
        except (httpx.HTTPError, OSError, TimeoutError, ValueError) as exc:
            raise _ProviderFailure("unavailable") from exc
        finally:
            self._next = self._clock() + (1.1 if host == "musicbrainz.org" else .35)

    async def _json(self, url, params):
        data = await self._request(url, params=params)
        if data is None:
            return None
        try:
            return json.loads(data)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise _ProviderFailure("unavailable") from exc

    def _put(self, key, result):
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        ttl = 86400 if result["status"] in {"matched", "candidate", "ambiguous"} else 3600
        if result["status"] in {"unavailable", "rate_limited"}:
            ttl = min(60, result.get("retry_after", 60))
        self._cache[key] = (self._clock() + ttl, deepcopy(result), size)
        self._cache_bytes += size
        while len(self._cache) > 64 or self._cache_bytes > 8 * 1024 * 1024:
            _, (_, _, old_size) = self._cache.popitem(last=False)
            self._cache_bytes -= old_size

    async def enrich(self, track):
        signature = _signature(track)
        if getattr(track, "deleted", False) or not _norm(signature["title"]) or not signature["duration"]:
            return _empty("insufficient_metadata")
        key = tuple(signature.values())
        # Coalesce identical concurrent requests without launching parallel
        # lookups. Both providers require sequential, identified requests.
        async with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > self._clock():
                self._cache.move_to_end(key)
                return deepcopy(cached[1])
            if cached:
                self._cache_bytes -= self._cache.pop(key)[2]
            result = await self._enrich(signature)
            self._put(key, result)
            return deepcopy(result)

    async def confirm(self, track, candidate):
        """Resolve one owner-selected stored candidate, never an arbitrary URL.

        Selection resolves ambiguity only. It does not waive metadata/length or
        excerpt checks, and the caller must recheck its DB revision after I/O.
        """
        if not isinstance(candidate, dict) or getattr(track, "deleted", False):
            return _empty("unavailable", "invalid_candidate")
        source, identity = candidate.get("source"), candidate.get("source_id")
        valid_id = (source == "lrclib" and type(identity) is int and 0 < identity < 2**63) or (
            source == "musicbrainz" and isinstance(identity, str) and bool(re.fullmatch(_UUID, identity)))
        title, artist, album = (_text(candidate.get(field)) for field in ("title", "artist", "album"))
        if not valid_id or not _norm(title) or not _norm(artist) or max(len(title), len(artist), len(album)) > 240:
            return _empty("unavailable", "invalid_candidate")
        snapshot = SimpleNamespace(title=title, artist=artist, album=album,
            duration=getattr(track, "duration", 0), is_excerpt=_signature(track)["cut"])
        signature = _signature(snapshot)
        if not signature["duration"]:
            return _empty("insufficient_metadata")
        selected = _candidate(title, artist, album, _duration(candidate.get("duration")), source, identity)
        if signature["cut"]:
            return _empty("candidate", "duration_mismatch", candidate=selected, candidates=[selected])
        if source == "musicbrainz":
            # A recording selection is not a lyrics selection. Ordinary exact
            # matching still decides whether an independent LRCLIB record fits.
            return await self.enrich(snapshot)
        key = ("confirmed", source, identity, *signature.values())
        async with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > self._clock():
                self._cache.move_to_end(key)
                return deepcopy(cached[1])
            if cached:
                self._cache_bytes -= self._cache.pop(key)[2]
            try:
                raw = await self._json(f"https://lrclib.net/api/get/{identity}", None)
                rows = _lrclib_rows([raw]) if isinstance(raw, dict) else []
                if raw is None:
                    result = _empty("not_found")
                elif len(rows) != 1 or rows[0][0]["source_id"] != identity:
                    result = _empty("unavailable", "candidate_changed")
                else:
                    fresh, raw = rows[0]
                    match = _match(signature, fresh)
                    if match == "matched":
                        result = _empty("matched", candidate=fresh, lyrics=_lyrics(raw, fresh),
                            provenance=[{"source": "lrclib", "id": identity, "url": fresh["source_url"]}])
                    else:
                        result = _empty("candidate", "duration_mismatch" if match == "duration_mismatch" else "candidate_changed",
                            candidate=selected, candidates=[selected])
            except _ProviderFailure as exc:
                result = _empty(exc.status, **({"retry_after": exc.retry_after} if exc.retry_after else {}))
            except (ValueError, TypeError):
                result = _empty("unavailable")
            self._put(key, result)
            return deepcopy(result)

    async def _enrich(self, signature):
        result, lrc_choice = _empty("not_found"), None
        try:
            params = {"track_name": signature["title"], "artist_name": signature["artist"]} if signature["artist"] else {"q": signature["query"]}
            if signature["album"]:
                params["album_name"] = signature["album"]
            data = await self._json("https://lrclib.net/api/search", params)
            status, lrc_choice, suggestions = _select(signature, _lrclib_rows([] if data is None else data))
            if status == "matched":
                candidate, raw = lrc_choice
                result = _empty("matched", candidate=candidate, lyrics=_lyrics(raw, candidate))
                result["provenance"].append({"source": "lrclib", "id": candidate["source_id"], "url": candidate["source_url"]})
            elif status != "not_found":
                # A plausible cut/full-recording match is only an explicit
                # metadata suggestion; it must not gain synchronized lyrics.
                return _empty("ambiguous" if status == "ambiguous" else "candidate", status,
                    candidate=lrc_choice[0] if lrc_choice else None, candidates=suggestions)
        except _ProviderFailure as exc:
            result = _empty(exc.status, retry_after=exc.retry_after) if exc.retry_after else _empty(exc.status)
        except (ValueError, TypeError):
            result = _empty("unavailable")
        try:
            known = result["candidate"] or signature
            # Lucene syntax is fixed, with quoted/escaped untrusted text only.
            quote = lambda value: str(value).replace("\\", "\\\\").replace('"', '\\"')
            if known.get("artist"):
                query = f'recording:"{quote(known["title"])}" AND artist:"{quote(known["artist"])}"'
            else:
                # Unknown title may contain artist + title with no separator.
                # Search across both fields, but still use our strict matcher;
                # catalog ranking/score alone never authorizes auto-application.
                words = _norm(signature["query"]).split()
                if not words or len(words) > 20:
                    return result
                query = " AND ".join(f'(recording:"{quote(word)}" OR artist:"{quote(word)}")' for word in words)
            data = await self._json("https://musicbrainz.org/ws/2/recording/", {"query": query, "fmt": "json", "limit": 20})
            status, choice, suggestions = _select(signature if not lrc_choice else {**signature, "title": known["title"], "artist": known["artist"]}, _musicbrainz_rows({"recordings": []} if data is None else data, signature["album"]))
            if status == "matched":
                candidate, raw = choice
                if result["status"] != "matched":
                    result = _empty("matched", candidate=candidate)
                result["artwork"] = _artwork(raw, signature["album"] or (result["candidate"] or {}).get("album", ""))
                result["provenance"].append({"source": "musicbrainz", "id": candidate["source_id"], "url": candidate["source_url"]})
            elif result["status"] != "matched" and status != "not_found":
                result = _empty("ambiguous" if status == "ambiguous" else "candidate", status,
                    candidate=choice[0] if choice else None, candidates=suggestions)
        except _ProviderFailure as exc:
            # Cover failure must not discard matched lyrics. When there was no
            # match at all, distinguish retryable catalog failure from absence.
            if result["status"] != "matched":
                delay = max(result.get("retry_after", 0), exc.retry_after)
                result = _empty("rate_limited" if delay else exc.status, **({"retry_after": delay} if delay else {}))
        except (ValueError, TypeError):
            if result["status"] != "matched":
                result = _empty("unavailable")
        return result

    async def fetch_artwork(self, artwork):
        if not isinstance(artwork, dict) or artwork.get("source") != "coverartarchive":
            return None
        identity = artwork.get("release_id")
        if not isinstance(identity, str) or not re.fullmatch(_UUID, identity):
            return None
        url = f"https://coverartarchive.org/release/{identity}/front-500"
        if artwork.get("url") != url:
            return None
        async with self._lock:
            try:
                data = await self._request(url, maximum=MAX_ARTWORK_BYTES,
                    redirects=lambda value: _artwork_redirect(value, identity))
                if data:
                    result = await asyncio.to_thread(_jpeg_thumbnail, data)
                    return result if result and len(result) <= MAX_THUMBNAIL_BYTES else None
            except _ProviderFailure:
                pass
        return None


def _artwork_redirect(value, identity):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.query or parsed.fragment:
            return False
        if not re.fullmatch(r"(?:archive\.org|s3\.us\.archive\.org|ia\d+\.(?:us|eu)\.archive\.org|dn\d+\.ca\.archive\.org)", parsed.hostname or ""):
            return False
        # CAA's current thumbnail redirect uses _thumb500.jpg; older stored
        # covers use -500.jpg. Keep both bound to the selected release and size.
        return bool(re.fullmatch(rf"/(?:download/|\d+/items/)?mbid-{identity}/mbid-{identity}-\d+(?:-500|_thumb500)?\.jpg", parsed.path))
    except ValueError:
        return False


_service = MusicEnrichmentService()


async def enrich_track(track):
    # A persistent caller may retain is_excerpt=True after the owner confirms
    # cleaned metadata, so later lookups cannot forget a known cut annotation.
    return await _service.enrich(track)


async def fetch_artwork_thumbnail(artwork):
    return await _service.fetch_artwork(artwork)


async def enrich_confirmed_candidate(track, candidate):
    return await _service.confirm(track, candidate)
