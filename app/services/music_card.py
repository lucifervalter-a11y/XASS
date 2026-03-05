from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

NO_MUSIC_MARKERS = {
    "сейчас ничего не играет",
    "iPhone: нет свежих данных".lower(),
    "нет данных с пк",
    "vk: нет данных",
    "не в сети",
    "не указано",
    "нет данных",
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
        "открыто приложение:",
        "сейчас на пк",
    )
    for prefix in noisy_prefixes:
        if lowered.startswith(prefix):
            return ""
    prefixes = ("iphone:", "vk:", "pc:")
    for prefix in prefixes:
        if lowered.startswith(prefix):
            raw = raw[len(prefix):].strip(" -:")
            break
    return raw


def split_artist_title(text: str) -> tuple[str, str]:
    raw = normalize_track_input(text)
    if not raw:
        return "", ""
    separators = (" - ", " — ", " – ", "-", "—", "–")
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


async def _itunes_search_first(
    client: httpx.AsyncClient,
    *,
    term: str,
    entity: str,
    limit: int = 1,
) -> dict[str, Any] | None:
    clean_term = _clean_text(term)
    if not clean_term:
        return None
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
        return None
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    return first


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
            if parsed_artist and parsed_title:
                search_terms.append(f"{parsed_artist} {parsed_title}")
            if parsed_title:
                search_terms.append(parsed_title)
            if parsed_artist:
                search_terms.append(parsed_artist)

            seen_terms: set[str] = set()
            for term in search_terms:
                key = term.strip().lower()
                if not key or key in seen_terms:
                    continue
                seen_terms.add(key)
                track_result = await _itunes_search_first(client, term=term, entity="song")
                if track_result:
                    break

            if not track_result:
                album_seed = parsed_artist or normalized
                album_result = await _itunes_search_first(client, term=album_seed, entity="album")

            if not track_result and not album_result:
                artist_seed = parsed_artist or normalized
                artist_result = await _itunes_search_first(client, term=artist_seed, entity="musicArtist")

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
