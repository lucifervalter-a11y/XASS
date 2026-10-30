"""Read-only, bounded library audit and conservative duplicate candidates.

Neither names, Chromaprint similarity nor duration proves recording identity.
All candidate groups require manual listening; this module cannot delete files.
Silence correction is measured at -60 dBFS, not inferred from duration mismatch.
"""
from __future__ import annotations

from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import wave

import mutagen

from app.services.music_artwork import (_BoundedReader, _cached_artwork, _embedded_candidates,
                                      _no_link, CACHE_VERSION, MAX_SOURCE_BYTES)
from app.services.music_library import track_path
from app.services.music_lyrics import _from_tags

MAX_AUDIT_TRACKS = 20
AUDIT_SECONDS = 20
_slots = threading.BoundedSemaphore(2)
_editions = re.compile(r"\b(live|acoustic|remaster(?:ed)?|radio[\s_-]+edit|extended(?:[\s_-]+mix)?|demo|instrumental|remix|karaoke|slowed|sped[\s_-]*up|nightcore|концерт\w*|ремикс|кавер)\b", re.I)
_domain = re.compile(r"(?:https?://|www\.|\b[a-z0-9-]+\.(?:com|net|org|ru|cc|site|su|me|io)\b)", re.I)


class AuditBusy(ValueError):
    pass


def norm(value):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", str(value)).casefold()))


def edition_markers(*values):
    values = " ".join(str(value).replace("_", " ") for value in values)
    markers = {norm(x).replace("remastered", "remaster") for x in _editions.findall(values)}
    # Do not collapse Live in Berlin into Live in Paris, or 2011/2020 masters.
    # Qualifiers are protective evidence, never stripped from the stored tags.
    for qualifier in re.findall(r"\[([^\]]{1,240})\]|\(([^)]{1,240})\)", values):
        text = qualifier[0] or qualifier[1]
        if _editions.search(text):
            markers.add(norm(text).replace("remastered", "remaster"))
    for match in re.finditer(r"\b(?:remaster(?:ed)?\s*(?:in\s*)?(?:19|20)\d{2}|(?:19|20)\d{2}\s*remaster(?:ed)?)\b", values, re.I):
        markers.add(norm(match.group()).replace("remastered", "remaster"))
    return sorted(markers)


def catalog_identity(track):
    return {field: getattr(track, field) for field in ("title", "artist", "album")}


def revision_token(track, enrichment):
    value = {**catalog_identity(track), "id": track.id, "sha256": track.sha256,
             "duration": track.duration, "deleted": track.deleted,
             "revision": enrichment.revision if enrichment is not None else None}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def messy_reasons(title, artist, filename=""):
    reasons = []
    if _domain.search(title) or _domain.search(artist):
        reasons.append("website_in_tags")
    if re.match(r"^\s*(?:track[\s_.-]*\d+|\d{1,3}[.)_-]\s*)", title, re.I):
        reasons.append("track_number_prefix")
    artist_key, title_key = norm(artist), norm(title)
    if artist_key and title_key.startswith(artist_key + " "):
        reasons.append("artist_repeated_in_title")
    if not title.strip() or (not artist.strip() and norm(title) == norm(Path(filename).stem)):
        reasons.append("filename_fallback")
    return reasons


def _finite(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 86400 else None


def _silence_bounds(pcm, sample_rate):
    samples = array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    # Amplitude 10^(-60/20), relative to signed 16-bit full scale.
    active = [i for i, value in enumerate(samples) if abs(value) > 32]
    if not active:
        return None
    return active[0] / sample_rate, (len(samples) - 1 - active[-1]) / sample_rate


def measured_silence(path, duration, *, deadline):
    """Measure at most 6 s at each boundary; unknown is never treated as zero.

    WAV/16-bit PCM works without extra programs. Other containers use optional
    local ffmpeg. All-silent or >6-second padding stays explicitly unmeasured.
    """
    window = min(6.0, duration)
    if time.monotonic() >= deadline or duration <= 0:
        return {"status": "budget_exhausted"}
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getsampwidth() != 2 or audio.getnchannels() != 1:
                raise wave.Error("requires mono PCM")
            rate, count = audio.getframerate(), audio.getnframes()
            if rate > 192000:
                raise wave.Error("sample rate budget")
            edge_frames = min(count, int(window * rate))
            first = audio.readframes(edge_frames)
            audio.setpos(max(0, count - edge_frames))
            last = audio.readframes(edge_frames)
            measured_duration = count / rate
    except (OSError, EOFError, wave.Error):
        executable = shutil.which("ffmpeg")
        if not executable:
            return {"status": "decoder_unavailable"}
        decoded = []
        for offset in (0, max(0, duration - window)):
            remaining = deadline - time.monotonic()
            if remaining <= .1:
                return {"status": "budget_exhausted"}
            try:
                result = subprocess.run([executable, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                    "-ss", str(offset), "-i", str(path), "-t", str(window), "-vn", "-ac", "1", "-ar", "8000",
                    "-f", "s16le", "pipe:1"], capture_output=True, timeout=min(3, remaining),
                    creationflags=0x08000000 if os.name == "nt" else 0)
            except (OSError, subprocess.TimeoutExpired):
                return {"status": "decoder_unavailable"}
            if result.returncode or not 0 < len(result.stdout) <= 96000:
                return {"status": "decoder_unavailable"}
            decoded.append(result.stdout)
        first, last = decoded
        rate, measured_duration = 8000, duration
    leading, trailing = _silence_bounds(first, rate), _silence_bounds(last, rate)
    if leading is None or trailing is None:
        return {"status": "unmeasured_silent_boundary"}
    start, end = leading[0], trailing[1]
    effective = measured_duration - start - end
    if effective <= 0:
        return {"status": "unmeasured_silent_boundary"}
    return {"status": "measured", "threshold_dbfs": -60, "window_seconds": window,
            "leading_seconds": round(start, 4), "trailing_seconds": round(end, 4),
            "effective_duration": round(effective, 4)}


def local_fingerprint(path, deadline):
    """Local-only Chromaprint. No AcoustID upload/key and no remote downloader."""
    executable = os.environ.get("XASS_FPCALC") or shutil.which("fpcalc")
    if not executable:
        return {"status": "unavailable"}
    remaining = deadline - time.monotonic()
    if remaining <= .1:
        return {"status": "budget_exhausted"}
    try:
        result = subprocess.run([executable, "-json", "-raw", "-length", "120", str(path)],
            capture_output=True, timeout=min(5, remaining), creationflags=0x08000000 if os.name == "nt" else 0)
        # Some fpcalc/FFmpeg combinations report terminal EOF as exit 3 after
        # printing a valid result. Do not accept arbitrary partial decode errors.
        terminal_eof = (result.returncode == 3 and getattr(result, "stderr", b"").strip()
                        == b"ERROR: Error decoding audio frame (End of file)")
        if (result.returncode != 0 and not terminal_eof) or len(result.stdout) > 65536:
            return {"status": "unavailable"}
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or _finite(data.get("duration")) is None:
            return {"status": "unavailable"}
        raw = data.get("fingerprint")
        if (not isinstance(raw, list) or not 30 <= len(raw) <= 2000
                or any(type(x) is not int or not -(2**31) <= x < 2**32 for x in raw)):
            return {"status": "unavailable"}
        return {"status": "measured", "source": "local_chromaprint", "window_seconds": 120,
                "duration": data["duration"], "_raw": raw,
                **({"decoder_notice": "terminal_eof"} if terminal_eof else {})}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"status": "unavailable"}


def fingerprint_similarity(left, right, *, deadline=None):
    """Bounded time-shift search (about +/-3s), candidate evidence only.

    Chromaprint raw subfingerprints are 32-bit; an arbitrary equality or high
    match does NOT prove file identity, recording version or complete duration.
    """
    a, b = left.get("_raw", []), right.get("_raw", [])
    if len(a) < 30 or len(b) < 30:
        return None
    best = None
    for shift in range(-24, 25):
        if deadline is not None and time.monotonic() >= deadline:
            return None  # No partial score presented as a completed comparison.
        x, y = a[max(0, shift):], b[max(0, -shift):]
        overlap = min(len(x), len(y))
        if overlap < 30 or overlap < min(len(a), len(b)) * .8:
            continue
        similarity = 1 - sum(((i ^ j) & 0xFFFFFFFF).bit_count() for i, j in zip(x[:overlap], y[:overlap])) / (32 * overlap)
        if best is None or similarity > best["similarity"]:
            best = {"similarity": round(similarity, 4), "shift_frames": shift, "overlap_frames": overlap}
    return best


def cached_lyrics_available(root, track):
    """Read the existing timed-lyrics cache without creating directories/fetching."""
    from app.services.synced_lyrics import fingerprint as lyrics_fingerprint, FOUND_TTL
    cache = root.parent / "music-lyrics-cache"
    path = cache / f"{track.id}.json"
    try:
        for part in (cache, *cache.parents, path):
            _no_link(part)
        if not path.is_file() or not 0 < path.stat().st_size <= 512 * 1024:
            return False
        with path.open("rb") as stream:
            data = stream.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            return False
        value = json.loads(data)
        return (isinstance(value, dict) and value.get("fingerprint") == lyrics_fingerprint(track)
                and value.get("status") in {"synced", "plain", "instrumental"}
                and 0 <= time.time() - float(value.get("cached_at") or 0) <= FOUND_TTL
                and (value.get("status") == "instrumental" or bool(str(value.get("text") or "").strip())))
    except (OSError, ValueError, TypeError):
        return False


def inspect_track(root, track, enrichment, *, acoustic=False, known_lyrics=False, deadline):
    result = {"id": track.id, **catalog_identity(track), "filename": track.filename, "size": track.size,
              "duration": track.duration, "codec": None, "bitrate": None,
              "revision_token": revision_token(track, enrichment), "edition_markers": edition_markers(track.title, track.album, track.filename),
              "issues": [], "messy_reasons": messy_reasons(track.title, track.artist, track.filename),
              "audio_status": "unavailable", "silence": {"status": "unavailable"},
              "fingerprint": {"status": "not_requested"}, "_sha256": track.sha256}
    stored = enrichment.result if enrichment and isinstance(enrichment.result, dict) else {}
    owner = enrichment.owner_lyrics if enrichment and isinstance(enrichment.owner_lyrics, dict) else {}
    manual = stored.get("manual_artwork") if isinstance(stored.get("manual_artwork"), dict) else {}
    has_art = bool(enrichment and enrichment.artwork_data and (not enrichment.dismissed or manual.get("source") == "owner"))
    lyrics = stored.get("lyrics", {}) if not enrichment or not enrichment.dismissed else {}
    has_lyrics = bool(known_lyrics or (owner.get("text") and not owner.get("disabled")) or (isinstance(lyrics, dict) and lyrics.get("text")))
    try:
        for part in (root, *root.parents, root / ".artwork"):
            _no_link(part)
        if re.fullmatch(r"[a-f0-9]{64}", str(track.sha256)):
            has_art = has_art or _cached_artwork(root / ".artwork" / f"{track.sha256}-{CACHE_VERSION}.jpg")
        has_lyrics = has_lyrics or cached_lyrics_available(root, track)
    except (OSError, ValueError, TypeError):
        pass
    try:
        for part in (root, *root.parents, root / track.storage_name):
            _no_link(part)
        source = track_path(root, track.storage_name)
        before = source.stat()
        if not source.is_file() or before.st_nlink != 1 or not 0 < before.st_size <= MAX_SOURCE_BYTES or before.st_size != track.size:
            raise ValueError("unsafe audio")
        if time.monotonic() >= deadline:
            result["audio_status"] = "budget_exhausted"
        else:
            with source.open("rb") as stream:
                audio = mutagen.File(_BoundedReader(stream), easy=False)
            if audio is None or not getattr(audio, "info", None):
                raise ValueError("invalid audio")
            result["audio_status"] = "inspected"
            result["codec"] = type(audio).__name__.lower()
            bitrate = getattr(audio.info, "bitrate", None)
            result["bitrate"] = int(bitrate) if type(bitrate) in (int, float) and math.isfinite(bitrate) and 0 < bitrate < 100_000_000 else None
            duration = _finite(getattr(audio.info, "length", None))
            if duration:
                result["duration"] = round(duration, 4)
                result["silence"] = measured_silence(source, duration, deadline=deadline)
            has_art = has_art or next(iter(_embedded_candidates(audio)), None) is not None
            has_lyrics = has_lyrics or bool(_from_tags(audio).get("text"))
            if acoustic:
                result["fingerprint"] = local_fingerprint(source, deadline)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError("audio changed")
    except (OSError, ValueError, TypeError, AttributeError, mutagen.MutagenError):
        result["audio_status"] = "unavailable"
        result["silence"] = {"status": "unavailable"}
        result["fingerprint"] = {"status": "unavailable"}
    # Unknown/offloaded files are not asserted to lack embedded art/lyrics.
    result["artwork_available"], result["lyrics_available"] = has_art, has_lyrics
    if result["audio_status"] == "inspected":
        if not has_art:
            result["issues"].append("no_artwork")
        if not has_lyrics:
            result["issues"].append("no_lyrics")
    if result["messy_reasons"]:
        result["issues"].append("messy_tags")
    return result


def candidate_groups(rows, *, deadline=None):
    groups = []
    # Pair-sized groups avoid claiming transitive identity from A~B and B~C.
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            if left["edition_markers"] != right["edition_markers"]:
                continue
            same_names = bool(norm(left["title"]) and norm(left["artist"]) and
                              norm(left["title"]) == norm(right["title"]) and norm(left["artist"]) == norm(right["artist"]))
            acoustic = fingerprint_similarity(left["fingerprint"], right["fingerprint"], deadline=deadline)
            same_bytes = left.get("_sha256") == right.get("_sha256") and bool(left.get("_sha256"))
            if not same_names and not same_bytes and not (acoustic and acoustic["similarity"] >= .9):
                continue
            measured = all(row["silence"].get("status") == "measured" for row in (left, right))
            durations = [row["silence"]["effective_duration"] if measured else row["duration"] for row in (left, right)]
            delta = abs(durations[0] - durations[1]) if all(_finite(x) for x in durations) else None
            groups.append({"kind": "candidate", "track_ids": [left["id"], right["id"]],
                "manual_review_required": True, "identical_recording": None,
                "evidence": {"same_names": same_names, "ingested_sha256_match": same_bytes,
                    "chromaprint": acoustic, "duration_difference": round(delta, 4) if delta is not None else None,
                    "duration_basis": "measured_without_boundary_silence" if measured else "untrimmed",
                    "duration_warning": delta is None or delta > 3},
                "reason": "Кандидаты, не доказательство одинаковой записи. Сравните звук и редакции вручную."})
    return groups


def audit_tracks(root, pairs, *, acoustic=False, known_lyrics_ids=()):
    if len(pairs) > MAX_AUDIT_TRACKS:
        raise ValueError("audit track limit")
    if not _slots.acquire(blocking=False):
        raise AuditBusy("Аудит уже выполняется. Повторите позже.")
    try:
        deadline = time.monotonic() + AUDIT_SECONDS
        rows = [inspect_track(root, track, record, acoustic=acoustic, known_lyrics=track.id in known_lyrics_ids,
                              deadline=deadline) for track, record in pairs]
        groups = candidate_groups(rows, deadline=deadline)
        acoustic_complete = not acoustic or time.monotonic() < deadline
        for row in rows:
            row.pop("_sha256", None)
            row["fingerprint"].pop("_raw", None)
        return {"tracks": rows, "candidate_groups": groups, "automatic_deletion": False,
                "scope": "selected_tracks", "audio_files_modified": False,
                "acoustic_comparison_complete": acoustic_complete,
                "categories": {name: [row["id"] for row in rows if name in row["issues"]]
                               for name in ("no_artwork", "no_lyrics", "messy_tags")}}
    finally:
        _slots.release()
