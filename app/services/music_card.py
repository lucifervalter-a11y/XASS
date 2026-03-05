from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

NO_MUSIC_MARKERS = {
    "сейчас ничего не играет",
    "iPhone: нет свежих данных".lower(),
    "нет данных с пк",
    "vk: нет данных",
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
        if lowered == marker:
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


async def build_music_card(query_text: str) -> MusicCard:
    normalized = normalize_track_input(query_text)
    if not normalized:
        return MusicCard(query="", artist="", title="", album="", artwork_url="", album_url="")

    parsed_artist, parsed_title = split_artist_title(normalized)

    payload = {}
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(
                ITUNES_SEARCH_URL,
                params={"term": normalized, "entity": "song", "limit": 1},
            )
            response.raise_for_status()
            payload = response.json() if isinstance(response.json(), dict) else {}
    except Exception:
        payload = {}

    result = None
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict):
                result = first

    artist = parsed_artist
    title = parsed_title
    album = ""
    artwork = ""
    album_url = ""

    if result:
        artist = _clean_text(result.get("artistName")) or artist
        title = _clean_text(result.get("trackName")) or title
        album = _clean_text(result.get("collectionName"))
        artwork = _upgrade_artwork_size(_clean_text(result.get("artworkUrl100")))
        album_url = _clean_text(result.get("collectionViewUrl")) or _clean_text(result.get("trackViewUrl"))

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
    if card.album:
        q = f"{q} {card.album}".strip()

    if not q:
        return {}

    encoded_plus = quote_plus(q)
    encoded_url = quote(q, safe="")
    return {
        "VK": f"https://vk.com/audio?q={encoded_plus}",
        "Shazam": f"https://www.shazam.com/search/{encoded_url}",
        "Google": f"https://www.google.com/search?q={encoded_plus}",
        "Yandex Music": f"https://music.yandex.ru/search?text={encoded_plus}",
    }

