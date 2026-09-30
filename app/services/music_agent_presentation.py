"""Stored music presentation sent to an authenticated Windows player.

This module deliberately performs no provider lookup and never reads audio
bytes.  It only formats data that has already been accepted into the owner's
catalog or produced by a completed PC transcription job.
"""
from __future__ import annotations

import math
from typing import Any

from app.music_models import MusicEnrichment
from app.services.synced_lyrics import from_existing, from_transcription
from app.services.transcription_queue import done_result


MAX_AGENT_LYRICS = 8000


def _stamp(value: Any) -> str | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or not 0 <= seconds <= 86400:
        return None
    minutes = int(seconds // 60)
    return f"[{minutes:02d}:{seconds - minutes * 60:05.2f}]"


def render_lyrics(value: dict | None) -> str:
    """Render a trusted stored lyric as bounded LRC/plain text for LyricsPane."""
    if not isinstance(value, dict):
        return ""
    rows = value.get("lines") if value.get("synced") else None
    if isinstance(rows, list):
        rendered: list[str] = []
        size = 0
        for row in rows[:2000]:
            if not isinstance(row, dict):
                continue
            text = " ".join(str(row.get("text") or "").replace("\x00", " ").split())[:500]
            stamp = _stamp(row.get("start", row.get("time")))
            if not text or stamp is None:
                continue
            line = stamp + text
            extra = len(line) + (1 if rendered else 0)
            if size + extra > MAX_AGENT_LYRICS:
                break
            rendered.append(line)
            size += extra
        if rendered:
            return "\n".join(rendered)
    text = value.get("text")
    if not isinstance(text, str):
        return ""
    clean = "".join(character for character in text.replace("\r\n", "\n").replace("\r", "\n")
                    if character in "\n\t" or ord(character) >= 32).strip()
    return clean[:MAX_AGENT_LYRICS]


def select_stored_lyrics(record: MusicEnrichment | None, transcription: dict | None, duration: float) -> str:
    """Match the native player's stored-source priority without doing I/O.

    Owner/catalog timed text wins.  A completed PC transcript then beats plain
    filler, while an old on-device automatic draft yields to the PC result.
    """
    owner = record.owner_lyrics if record and isinstance(record.owner_lyrics, dict) else None
    enrichment = ((record.result or {}).get("lyrics") if record and not record.dismissed
                  and isinstance(record.result, dict) else None)
    automatic = from_transcription(transcription)
    if automatic and isinstance(owner, dict) and owner.get("source") == "on_device_transcription":
        owner = None
    plain = None
    for payload, source in ((owner, "owner"), (enrichment, "catalog")):
        found = from_existing(payload, duration, source)
        if found and found.get("synced"):
            return render_lyrics(found)
        plain = plain or found
    return render_lyrics(automatic or plain)


async def stored_agent_presentation(session, track) -> dict[str, Any]:
    """Return command-safe text plus an artwork availability hint."""
    record = await session.get(MusicEnrichment, track.id)
    transcription = await done_result(session, track.id)
    lyrics = select_stored_lyrics(record, transcription, float(track.duration or 0))
    from app.music_enrichment_api import stored_artwork_available
    return {
        "lyrics": lyrics,
        # The route also serves an already cached embedded thumbnail.  This
        # flag only avoids an unnecessary request when no catalog cover exists;
        # embedded cover extraction remains the agent's local fallback.
        "catalog_artwork": stored_artwork_available(record),
    }
