"""Private, bounded lyrics from the owner's audio tags; never fetch third-party text."""
from __future__ import annotations

from collections import OrderedDict
import itertools
from pathlib import Path
import re
import threading

import mutagen

from app.services.music_artwork import _BoundedReader, _no_link, MAX_SOURCE_BYTES
from app.services.music_library import track_path

MAX_TEXT_BYTES = 64 * 1024
MAX_LINES = 2000
_cache: OrderedDict[tuple, dict] = OrderedDict()
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(2)
_stamp = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
_meta = re.compile(r"^\[(?:ar|al|ti|au|by|re|ve|length|offset):.*\]$", re.I)


def empty_lyrics():
    return {"text": "", "lines": [], "source": "none", "synced": False}


def parse_lyrics(text: str) -> dict:
    """Plain text or LRC, including repeated timestamps and global millisecond offset."""
    if not isinstance(text, str) or len(text) > MAX_TEXT_BYTES or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        return empty_lyrics()
    text = "".join(char for char in text.replace("\r\n", "\n").replace("\r", "\n")
                   if char in "\n\t" or ord(char) >= 32).strip("\ufeff \n\t")
    rows = text.splitlines()
    if len(rows) > MAX_LINES:
        return empty_lyrics()
    offsets = re.findall(r"^\[offset:([+-]?\d{1,7})\]\s*$", text, re.I | re.M)
    offset = max(-86400, min(86400, int(offsets[-1]) / 1000)) if offsets else 0
    timed, plain = [], []
    for row in rows:
        if _meta.fullmatch(row.strip()):
            continue
        position, stamps = 0, []
        while match := _stamp.match(row, position):
            fraction = float("0." + (match[3] or "0"))
            stamps.append(int(match[1]) * 60 + int(match[2]) + fraction + offset)
            position = match.end()
            if len(stamps) > MAX_LINES:
                return empty_lyrics()
        value = row[position:].strip()
        if value:
            plain.append(value)
        for stamp in stamps:
            if 0 <= stamp <= 86400:
                timed.append({"time": round(stamp, 3), "text": value})
                if len(timed) > MAX_LINES:
                    return empty_lyrics()
    timed.sort(key=lambda row: row["time"])
    body = "\n".join(plain).strip()
    return {"text": body, "lines": timed, "source": "embedded" if body else "none", "synced": bool(timed and body)}


def _from_tags(audio):
    tags = getattr(audio, "tags", None)
    if not tags:
        return empty_lyrics()
    if hasattr(tags, "getall"):
        # ID3 SYLT time stamps are either MPEG frames (unsupported) or milliseconds.
        for frame in itertools.islice(tags.getall("SYLT"), 8):
            if frame.format != 2 or frame.type != 1 or len(frame.text) > MAX_LINES:
                continue
            lines = [{"time": stamp / 1000, "text": value.strip()} for value, stamp in frame.text
                     if isinstance(stamp, int) and 0 <= stamp <= 86400000 and isinstance(value, str)]
            text = "\n".join(row["text"] for row in lines)
            if text.strip() and len(text.encode("utf-8")) <= MAX_TEXT_BYTES:
                return {"text": text, "lines": sorted(lines, key=lambda row: row["time"]), "source": "embedded", "synced": True}
        for frame in itertools.islice(tags.getall("USLT"), 8):
            result = parse_lyrics(frame.text)
            if result["text"]:
                return result
    for key in ("lyrics", "unsyncedlyrics", "\xa9lyr"):
        values = tags.get(key, ()) or ()
        if isinstance(values, str):
            values = [values]
        for value in itertools.islice(values, 8):
            result = parse_lyrics(value)
            if result["text"]:
                return result
    return empty_lyrics()


def embedded_lyrics(root: Path, track) -> dict:
    """Call in a worker; work/read budgets and a small LRU bound repeated polling."""
    try:
        if getattr(track, "deleted", False) or not re.fullmatch(r"[a-f0-9]{64}", str(track.sha256)):
            return empty_lyrics()
        root = Path(root).absolute()
        for part in [root, *root.parents, root / track.storage_name]:
            _no_link(part)
        source = track_path(root, track.storage_name)
        if not source.is_file():
            return empty_lyrics()
        stat = source.stat()
        if not 0 < stat.st_size <= MAX_SOURCE_BYTES or stat.st_size != track.size:
            return empty_lyrics()
        key = (str(source), track.sha256, stat.st_size, stat.st_mtime_ns)
        with _slots:
            with _lock:
                if key in _cache:
                    _cache.move_to_end(key)
                    return _cache[key]
            with source.open("rb") as stream:
                result = _from_tags(mutagen.File(_BoundedReader(stream), easy=False))
            with _lock:
                _cache[key] = result
                while len(_cache) > 64:
                    _cache.popitem(last=False)
            return result
    except (OSError, ValueError, TypeError, AttributeError, mutagen.MutagenError):
        return empty_lyrics()
