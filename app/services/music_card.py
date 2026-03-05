from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import quote

import httpx

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEEZER_SEARCH_URL = "https://api.deezer.com/search"
TOKEN_RE = re.compile(r"[0-9A-Za-z\u0400-\u04FF]+")

NO_MUSIC_MARKERS = {
    "\u0441\u0435\u0439\u0447\u0430\u0441 \u043d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u0438\u0433\u0440\u0430\u0435\u0442",
    "iphone: \u043d\u0435\u0442 \u0441\u0432\u0435\u0436\u0438\u0445 \u0434\u0430\u043d\u043d\u044b\u0445",
    "\u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445 \u0441 \u043f\u043a",
    "vk: \u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445",
    "\u043d\u0435 \u0432 \u0441\u0435\u0442\u0438",
    "\u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e",
    "\u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445",
    "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u043e\u0435 \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435",
    "remote desktop",
    "teamviewer",
    "anydesk",
    "rustdesk",
}

@dataclass(slots=True)
class MusicCard:
    query: str
    artist: str
    title: str
    album: str
    artwork_url: str
    album_url: str


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_track_input(text: str) -> str:
    raw = _clean_text(text)
    if not raw:
        return ""
    lowered = raw.lower()
    for marker in NO_MUSIC_MARKERS:
        if lowered == marker or marker in lowered:
            return ""
    noisy_prefixes = (
        "\u043e\u0442\u043a\u0440\u044b\u0442\u043e \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435:",
        "\u0441\u0435\u0439\u0447\u0430\u0441 \u043d\u0430 \u043f\u043a",
    )
    for prefix in noisy_prefixes:
        if lowered.startswith(prefix):
            return ""
    prefixes = ("iphone:", "vk:", "pc:")
    for prefix in prefixes:
        if lowered.startswith(prefix):
            raw = raw[len(prefix):].strip(" -:")
            break
    raw = re.sub(r"^[^0-9A-Za-z\u0400-\u04FF]+", "", raw).strip()
    lowered = raw.lower()
    noisy_leading_words = (
        "\u043f\u0435\u0440\u0435\u043f\u0438\u0441\u044b\u0432\u0430\u044e ",
        "\u0441\u043b\u0443\u0448\u0430\u044e ",
        "\u0438\u0433\u0440\u0430\u0435\u0442 ",
        "\u0438\u0433\u0440\u0430\u044e ",
        "playing ",
        "listening ",
        "now playing ",
    )
    for marker in noisy_leading_words:
        if lowered.startswith(marker):
            raw = raw[len(marker):].strip(" -:|")
            break
    lowered = raw.lower()
    noisy_substrings = (
        "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u043e\u0435 \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435",
        "remote desktop",
        "teamviewer",
        "anydesk",
        "rustdesk",
    )
    if any(marker in lowered for marker in noisy_substrings):
        return ""
    if re.search(r"\(\d{2,}\s+\d{2,}\s+\d{2,}\)", raw):
        return ""
    if re.search(r"\b[a-z]\d{3,}\(", lowered):
        return ""
    if ":" in raw:
        left, right = raw.split(":", maxsplit=1)
        if len(left.split()) <= 3 and right.strip():
            raw = right.strip(" -:|")
    return raw


def split_artist_title(text: str) -> tuple[str, str]:
    raw = normalize_track_input(text)
    if not raw:
        return "", ""
    separators = (" - ", " \u2014 ", " \u2013 ", "-", "\u2014", "\u2013")
    for sep in separators:
        if sep in raw:
            left, right = raw.split(sep, maxsplit=1)
            artist = _clean_text(left)
            title = _clean_text(right)
            if artist and title:
                return artist, title
    return "", raw


def _upgrade_artwork_size(url: str) -> str:
    raw = _clean_text(url)
    if not raw:
        return ""
    replacements = {
        "100x100bb.jpg": "1200x1200bb.jpg",
        "100x100-75.jpg": "1200x1200-75.jpg",
        "100x100bb.webp": "1200x1200bb.webp",
    }
    upgraded = raw
    for src, dst in replacements.items():
        if src in upgraded:
            upgraded = upgraded.replace(src, dst)
    return upgraded


def _tokenize(value: str) -> list[str]:
    return [item.lower() for item in TOKEN_RE.findall(_clean_text(value))]


def _normalized_intersection_size(left: list[str], right: list[str]) -> int:
    if not left or not right:
        return 0
    return len(set(left) & set(right))


def _score_song_candidate(
    *,
    candidate: dict[str, Any],
    query_text: str,
    query_tokens: list[str],
    parsed_artist_tokens: list[str],
    parsed_title_tokens: list[str],
    artist_title_hints: list[tuple[list[str], list[str]]],
) -> float:
    title = _clean_text(candidate.get("trackName"))
    artist = _clean_text(candidate.get("artistName"))
    album = _clean_text(candidate.get("collectionName"))
    title_tokens = _tokenize(title)
    artist_tokens = _tokenize(artist)
    album_tokens = _tokenize(album)

    score = 0.0
    score += _normalized_intersection_size(query_tokens, title_tokens) * 4.0
    score += _normalized_intersection_size(query_tokens, artist_tokens) * 3.0
    score += _normalized_intersection_size(query_tokens, album_tokens) * 1.2
    if parsed_artist_tokens:
        score += _normalized_intersection_size(parsed_artist_tokens, artist_tokens) * 5.0
    if parsed_title_tokens:
        score += _normalized_intersection_size(parsed_title_tokens, title_tokens) * 5.0
    for artist_hint_tokens, title_hint_tokens in artist_title_hints:
        artist_hit = _normalized_intersection_size(artist_hint_tokens, artist_tokens)
        title_hit = _normalized_intersection_size(title_hint_tokens, title_tokens)
        if artist_hit and title_hit:
            score += 6.0 + artist_hit * 2.0 + title_hit * 2.0

    query_lower = _clean_text(query_text).lower()
    title_lower = title.lower()
    artist_lower = artist.lower()
    if query_lower and query_lower in f"{artist_lower} {title_lower}":
        score += 2.5
    if title_lower and title_lower in query_lower:
        score += 2.0
    if artist_lower and artist_lower in query_lower:
        score += 1.5
    if title and artist:
        score += 0.25
    order_rank = candidate.get("__order")
    if isinstance(order_rank, int) and order_rank >= 0:
        score -= order_rank * 0.01
    return score


def _pick_best_song_candidate(
    *,
    candidates: list[dict[str, Any]],
    query_text: str,
    parsed_artist: str,
    parsed_title: str,
    artist_title_hints: list[tuple[str, str]],
) -> dict[str, Any] | None:
    if not candidates:
        return None
    query_tokens = _tokenize(query_text)
    parsed_artist_tokens = _tokenize(parsed_artist)
    parsed_title_tokens = _tokenize(parsed_title)
    prepared_hints: list[tuple[list[str], list[str]]] = [
        (_tokenize(artist_hint), _tokenize(title_hint))
        for artist_hint, title_hint in artist_title_hints
        if artist_hint and title_hint
    ]
    best: dict[str, Any] | None = None
    best_score = float("-inf")
    for candidate in candidates:
        score = _score_song_candidate(
            candidate=candidate,
            query_text=query_text,
            query_tokens=query_tokens,
            parsed_artist_tokens=parsed_artist_tokens,
            parsed_title_tokens=parsed_title_tokens,
            artist_title_hints=prepared_hints,
        )
        if score > best_score:
            best_score = score
            best = candidate
    return best


def _song_candidate_key(item: dict[str, Any]) -> str:
    track_id = item.get("trackId")
    if track_id is not None:
        return f"track:{track_id}"
    artist = _clean_text(item.get("artistName")).lower()
    title = _clean_text(item.get("trackName")).lower()
    album = _clean_text(item.get("collectionName")).lower()
    return f"{artist}|{title}|{album}"


async def _itunes_search(
    client: httpx.AsyncClient,
    *,
    term: str,
    entity: str,
    limit: int = 1,
) -> list[dict[str, Any]]:
    clean_term = _clean_text(term)
    if not clean_term:
        return []
    try:
        response = await client.get(
            ITUNES_SEARCH_URL,
            params={
                "term": clean_term,
                "entity": entity,
                "limit": max(1, int(limit)),
            },
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return []
    return [item for item in results if isinstance(item, dict)]


async def _deezer_search_first(
    client: httpx.AsyncClient,
    *,
    term: str,
    limit: int = 5,
) -> dict[str, Any] | None:
    clean_term = _clean_text(term)
    if not clean_term:
        return None
    try:
        response = await client.get(
            DEEZER_SEARCH_URL,
            params={
                "q": clean_term,
                "limit": max(1, int(limit)),
            },
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        return None
    first = rows[0]
    if not isinstance(first, dict):
        return None
    return first


async def _itunes_search_first(
    client: httpx.AsyncClient,
    *,
    term: str,
    entity: str,
    limit: int = 1,
) -> dict[str, Any] | None:
    items = await _itunes_search(client, term=term, entity=entity, limit=limit)
    return items[0] if items else None


async def build_music_card(query_text: str) -> MusicCard:
    normalized = normalize_track_input(query_text)
    if not normalized:
        return MusicCard(query="", artist="", title="", album="", artwork_url="", album_url="")

    parsed_artist, parsed_title = split_artist_title(normalized)

    artist = parsed_artist
    title = parsed_title
    album = ""
    artwork = ""
    album_url = ""

    track_result: dict[str, Any] | None = None
    album_result: dict[str, Any] | None = None
    artist_result: dict[str, Any] | None = None

    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            search_terms: list[str] = [normalized]
            artist_title_hints: list[tuple[str, str]] = []
            if parsed_artist and parsed_title:
                search_terms.append(f"{parsed_artist} {parsed_title}")
                search_terms.append(f"{parsed_title} {parsed_artist}")
            if parsed_title:
                search_terms.append(parsed_title)
            if parsed_artist:
                search_terms.append(parsed_artist)
            tokens = normalized.split()
            if len(tokens) >= 2:
                search_terms.append(" ".join(tokens[1:]))
            if len(tokens) >= 3:
                search_terms.append(" ".join(tokens[-2:]))
            if len(tokens) >= 4:
                search_terms.append(" ".join(tokens[-3:]))
            if not parsed_artist and len(tokens) >= 3:
                for tail_len in (2, 3):
                    if len(tokens) <= tail_len:
                        continue
                    artist_hint = " ".join(tokens[-tail_len:])
                    title_hint = " ".join(tokens[:-tail_len])
                    if not artist_hint or not title_hint:
                        continue
                    artist_title_hints.append((artist_hint, title_hint))
                    search_terms.append(f"{artist_hint} - {title_hint}")
                    search_terms.append(f"{artist_hint} {title_hint}")
                    search_terms.append(f"{title_hint} {artist_hint}")

            # Primary resolver: Deezer handles free-form queries better for RU/EN mixes.
            deezer_seen: set[str] = set()
            deezer_result: dict[str, Any] | None = None
            for term in search_terms:
                key = term.strip().lower()
                if not key or key in deezer_seen:
                    continue
                deezer_seen.add(key)
                deezer_result = await _deezer_search_first(client, term=term, limit=8)
                if deezer_result:
                    break

            if deezer_result:
                deezer_artist = deezer_result.get("artist") if isinstance(deezer_result.get("artist"), dict) else {}
                deezer_album = deezer_result.get("album") if isinstance(deezer_result.get("album"), dict) else {}
                artist = _clean_text(deezer_artist.get("name")) or artist
                title = _clean_text(deezer_result.get("title")) or title or normalized
                album = _clean_text(deezer_album.get("title")) or album
                artwork = _clean_text(deezer_album.get("cover_xl")) or _clean_text(deezer_album.get("cover_big")) or artwork

                # Enrich with Apple URL when possible.
                enrich_term = " ".join(item for item in (artist, title) if item).strip() or normalized
                enrich = await _itunes_search_first(client, term=enrich_term, entity="song", limit=3)
                if enrich:
                    album_url = _clean_text(enrich.get("collectionViewUrl")) or _clean_text(enrich.get("trackViewUrl"))
                    if not artwork:
                        artwork = _upgrade_artwork_size(_clean_text(enrich.get("artworkUrl100")))
                    if not album:
                        album = _clean_text(enrich.get("collectionName"))

                if artwork:
                    artwork = _upgrade_artwork_size(artwork)
                final_query = " - ".join(item for item in (artist, title) if item).strip() or normalized
                return MusicCard(
                    query=final_query,
                    artist=artist,
                    title=title,
                    album=album,
                    artwork_url=artwork,
                    album_url=album_url,
                )

            seen_terms: set[str] = set()
            track_candidates: dict[str, dict[str, Any]] = {}
            candidate_order = 0
            for term in search_terms:
                key = term.strip().lower()
                if not key or key in seen_terms:
                    continue
                seen_terms.add(key)
                for item in await _itunes_search(client, term=term, entity="song", limit=25):
                    dedup_key = _song_candidate_key(item)
                    existing = track_candidates.get(dedup_key)
                    if existing is None:
                        with_order = dict(item)
                        with_order["__order"] = candidate_order
                        track_candidates[dedup_key] = with_order
                    else:
                        old_order = existing.get("__order")
                        if not isinstance(old_order, int) or candidate_order < old_order:
                            existing["__order"] = candidate_order
                    candidate_order += 1

            if track_candidates:
                track_result = _pick_best_song_candidate(
                    candidates=list(track_candidates.values()),
                    query_text=normalized,
                    parsed_artist=parsed_artist,
                    parsed_title=parsed_title,
                    artist_title_hints=artist_title_hints,
                )

            if not track_result:
                album_seed = parsed_artist or normalized
                album_result = await _itunes_search_first(client, term=album_seed, entity="album")

            if not track_result and not album_result:
                artist_seed = parsed_artist or normalized
                artist_result = await _itunes_search_first(client, term=artist_seed, entity="musicArtist")

            # Artist-intent heuristic: for short free-form queries like "Cold Carti"
            # prefer artist card when selected track is only an unrelated feature.
            query_tokens = _tokenize(normalized)
            if (
                track_result
                and artist_result
                and not parsed_artist
                and len(query_tokens) <= 4
            ):
                track_artist_tokens = _tokenize(_clean_text(track_result.get("artistName")))
                if _normalized_intersection_size(query_tokens, track_artist_tokens) == 0:
                    track_result = None

            if not track_result and album_result and not _clean_text(album_result.get("artworkUrl100")):
                artist_name = _clean_text(album_result.get("artistName"))
                if artist_name:
                    fallback_album = await _itunes_search_first(client, term=artist_name, entity="album")
                    if fallback_album:
                        album_result = fallback_album

            if not track_result and artist_result:
                artist_name = _clean_text(artist_result.get("artistName"))
                if artist_name:
                    fallback_album = await _itunes_search_first(client, term=artist_name, entity="album")
                    if fallback_album:
                        album_result = fallback_album
    except Exception:
        track_result = None
        album_result = None
        artist_result = None

    if track_result:
        artist = _clean_text(track_result.get("artistName")) or artist
        title = _clean_text(track_result.get("trackName")) or title
        album = _clean_text(track_result.get("collectionName"))
        artwork = _upgrade_artwork_size(_clean_text(track_result.get("artworkUrl100")))
        album_url = _clean_text(track_result.get("collectionViewUrl")) or _clean_text(track_result.get("trackViewUrl"))
    elif album_result:
        artist = _clean_text(album_result.get("artistName")) or artist or normalized
        if not parsed_title:
            title = ""
        album = _clean_text(album_result.get("collectionName"))
        artwork = _upgrade_artwork_size(_clean_text(album_result.get("artworkUrl100")))
        album_url = _clean_text(album_result.get("collectionViewUrl"))
    elif artist_result:
        artist = _clean_text(artist_result.get("artistName")) or artist or normalized
        if not parsed_title:
            title = ""
        album_url = _clean_text(artist_result.get("artistLinkUrl")) or _clean_text(artist_result.get("artistViewUrl"))

    if not artist and not title:
        title = normalized

    final_query = " - ".join(item for item in (artist, title) if item).strip() or normalized
    return MusicCard(
        query=final_query,
        artist=artist,
        title=title,
        album=album,
        artwork_url=artwork,
        album_url=album_url,
    )


def build_search_links(card: MusicCard) -> dict[str, str]:
    q = _clean_text(card.query)
    if card.album and _clean_text(card.album).lower() not in q.lower():
        q = f"{q} {card.album}".strip()

    if not q:
        return {}

    encoded_query = quote(q, safe="")
    encoded_url = encoded_query
    apple_url = _clean_text(card.album_url) or f"https://music.apple.com/search?term={encoded_query}"
    return {
        "VK": f"https://vk.com/audio?section=search&q={encoded_query}",
        "Shazam": f"https://www.shazam.com/search/{encoded_url}",
        "Apple Music": apple_url,
        "Google": f"https://www.google.com/search?q={encoded_query}",
        "Yandex Music": f"https://music.yandex.ru/search?text={encoded_query}",
    }
