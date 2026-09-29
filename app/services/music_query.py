"""Search-query normalization for catalog lookups (LRCLIB, MusicBrainz).

Only the text sent to providers is cleaned; stored track metadata never
changes. Removes download-site tags ("[mp3xa.cc]", "(zaycev.net)"), bare
domains, upload noise like "(Official Video)", file extensions and
underscores, and splits "Artist - Title" titles. Musical version names
(remix, live, remastered, feat.) are kept: they decide which recording matches.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

_TLD = (r"(?:cc|ru|net|com|org|info|biz|me|fm|su|ua|by|kz|uz|io|to|tv|club|pro|online|site|xyz|top|"
        r"music|mobi|ws|in|co|uk|de|pl|lv|lt|ee|am|ge|az|md|name|space|website|store|рф)")
_HOST = r"(?:https?://)?(?:www\.)?[\w-]+(?:\.[\w-]+)*\." + _TLD + r"(?:/[^\s\)\]\}]*)?"
# "[mp3xa.cc]", "(zaycev.net)", "{muzmo.ru}", "[Скачано с muzofond.fm]".
_SITE_TAG = re.compile(r"[\(\[\{][^\(\)\[\]\{\}]{0,40}?(?<![\w.])" + _HOST + r"[^\(\)\[\]\{\}]{0,20}[\)\]\}]", re.I)
_DOMAIN = re.compile(r"(?<![\w.@])" + _HOST + r"(?![\w.])", re.I)
_NOISE = re.compile(
    r"[\(\[\{]\s*(?:official(?:\s+(?:music|lyrics?|hd|4k))?\s*(?:video|audio|clip|visuali[sz]er)?|"
    r"(?:music|lyrics?)\s+video|lyrics?|audio|video|hd|hq|4k|clip|visuali[sz]er|"
    r"клип|премьера(?:\s+клипа)?|официальн\w*\s+(?:видео|клип))\s*[\)\]\}]", re.I)
_EXT = re.compile(r"\.(?:mp3|m4a|flac|wav|ogg|opus|aac|wma)$", re.I)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}")
_DASH = re.compile(r"\s+[-–—]\s+")
# "01 - Artist - Title" / "01. Artist - Title". A bare "7 rings" is a title and stays.
_INDEX = re.compile(r"^\d{1,3}\s*[-._)]\s+")
_REUPLOAD = re.compile(r"\b(?:re-?uploads?)\b", re.I)
UNKNOWN_ARTISTS = frozenset({"", "unknown", "unknown artist", "various artists", "va", "неизвестен",
                             "неизвестный исполнитель", "<unknown>", "без исполнителя"})


def _nfc(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFC", value)


def clean_search_text(value: Any) -> str:
    """One title/artist string, cleaned for a provider query."""
    text = _EXT.sub("", _nfc(value).strip()).replace("_", " ")
    for _ in range(3):
        text = _SITE_TAG.sub(" ", text)
        text = _NOISE.sub(" ", text)
    text = _DOMAIN.sub(" ", text)
    text = _EMPTY_BRACKETS.sub(" ", text)
    text = _REUPLOAD.sub(" ", text)
    text = " ".join(text.split())
    # Separators left dangling by a removed tag: "Artist - [site.ru]" -> "Artist".
    text = re.sub(r"^(?:[-–—|•·.,:;]\s*)+|(?:\s*[-–—|•·,:;])+$", "", text).strip()
    return " ".join(text.split())


def _key(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def is_unknown_artist(value: Any) -> bool:
    return clean_search_text(value).casefold() in UNKNOWN_ARTISTS


def search_names(title: Any, artist: Any) -> tuple[str, str]:
    """(artist, title) for a catalog query.

    "Нексюша [mp3xa.cc] - Фенибут" with no artist -> ("Нексюша", "Фенибут").
    With a known artist, a repeated "Artist - " prefix is dropped from the title.
    """
    clean_title = _INDEX.sub("", clean_search_text(title)).strip()
    clean_artist = "" if is_unknown_artist(artist) else clean_search_text(artist)
    parts = _DASH.split(clean_title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        left, right = parts[0].strip(), parts[1].strip()
        if not clean_artist:
            return left, right
        if _key(left) == _key(clean_artist):
            return clean_artist, right
    return clean_artist, clean_title


def filename_artist_title(title: Any, artist: Any) -> tuple[str, str] | None:
    """Artist and title written in an untagged "Artist - Title" filename.

    A tagged file keeps its artist. Returns nothing when there is no separator,
    so a title that merely contains both words is not guessed apart.
    """
    if not is_unknown_artist(artist):
        return None
    cleaned = _INDEX.sub("", clean_search_text(title)).strip()
    # One separator only. "A - B - C" is left whole rather than renaming the file.
    if len(_DASH.split(cleaned)) != 2:
        return None
    found_artist, found_title = search_names(title, artist)
    if not found_artist or not found_title:
        return None
    return found_artist, found_title
