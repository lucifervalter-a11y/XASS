"""Explicit local-library review. No silent tag replacement or duplicate deletion.

Every write is staged next to its target and journaled before atomic replacement.
The complete original file is retained for exact undo, including uncommon tags.
Provider match scores are never presented as probabilities of correctness.

Match score contract v1 (ordinal evidence rule, NOT calibrated probability):
95/green: AcoustID source_score >= .90, both durations known and within 1s,
matching version markers, no unresolved album/year/track/artwork evidence.
75/yellow: matching catalog names/versions and duration within 1s, no conflict.
65/yellow: AcoustID >= .60 but incomplete duration/evidence, or a catalog
match requiring conflict review. 40/red: weak/unknown catalog match, filename
heuristic or local AI transcription. Manual edits have no numeric score.
An artwork proposal is never marked verified by a recording fingerprint: it
requires review. Existing year/track evidence must be resolved for green.
All levels require explicit apply. Red additionally requires manual_choice=True.
"""
from __future__ import annotations

import base64
import contextlib
from difflib import SequenceMatcher
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

MAX_FILE = 256 * 1024 * 1024
MAX_BACKUPS = 2 * 1024 * 1024 * 1024
MAX_TRACKS = 1000
MAX_TEXT = 65536
EXTENSIONS = {".mp3", ".flac", ".ogg", ".opus", ".wav", ".m4a"}
_last_musicbrainz_request = 0.0
VARIANTS = re.compile(r"\b(live|remaster(?:ed)?|acoustic|radio\s+edit|extended(?:\s+mix)?|demo|instrumental|remix|karaoke|slowed|sped|nightcore|концерт\w*|ремикс|кавер)\b", re.I)


class CurationError(ValueError):
    """Safe, owner-facing reason, never raw provider/file exception text."""


def plain(value, maximum=480):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise CurationError("Недопустимый текст.")
    return unicodedata.normalize("NFC", "".join(c for c in value if ord(c) >= 32 or c in "\n\t")).strip()


def norm(value):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def variants(value):
    return sorted(set(x.casefold().replace("remastered", "remaster") for x in VARIANTS.findall(value)))


def match_rating(*, source_score=None, difference=None, versions_match=True, conflicts=(), catalog=False):
    """Fixed auditable policy points; provider similarity is returned separately."""
    source_score = source_score if type(source_score) in (int, float) and math.isfinite(source_score) and 0 <= source_score <= 1 else None
    close = type(difference) in (int, float) and math.isfinite(difference) and 0 <= difference <= 1
    if source_score is not None and source_score >= .9 and close and versions_match and not conflicts:
        score = 95
    elif versions_match and ((source_score is not None and source_score >= .6) or (source_score is None and catalog and close)):
        score = 65 if conflicts or not close or (source_score is not None and source_score < .9) else 75
    else:
        score = 40
    return {"match_score": score, "confidence": "high" if score >= 90 else "review" if score >= 60 else "weak",
            "source_score": source_score, "score_contract": 1}


def safe(path):
    from native_music_ownership import safe_path
    result = Path(path)
    if not result.is_absolute() or str(result).startswith("\\\\"):
        raise CurationError("Выберите локальную папку с музыкой.")
    try:
        return safe_path(result)
    except ValueError:
        raise CurationError("Ссылки и перенаправленные папки не обрабатываются.") from None


def identity(path):
    path = safe(path)
    stat = path.stat()
    if not path.is_file() or not 0 < stat.st_size <= MAX_FILE or stat.st_nlink != 1:
        raise CurationError("Файл недоступен, слишком велик или имеет жёсткие ссылки.")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    after = path.stat()
    if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise CurationError("Файл изменился во время проверки. Повторите разбор.")
    return digest.hexdigest()


def artwork_preview(data):
    if not isinstance(data, (bytes, bytearray)) or len(data) > 8 * 1024 * 1024:
        return ""
    from music_player import _jpeg_artwork
    from PIL import Image
    art = _jpeg_artwork(bytes(data))
    if not art:
        return ""
    with Image.open(io.BytesIO(art)) as image:
        image.thumbnail((192, 192))
        output = io.BytesIO(); image.save(output, "JPEG", quality=70)
    value = output.getvalue()
    return base64.b64encode(value).decode("ascii") if len(value) <= 48 * 1024 else ""


def _read_tags(path):
    import mutagen
    from mutagen.id3 import ID3
    audio = mutagen.File(path)
    if audio is None or getattr(audio, "info", None) is None:
        raise CurationError("Формат аудио не распознан.")
    tags = audio.tags or {}
    def first(*keys):
        for key in keys:
            values = tags.get(key)
            if values:
                text = getattr(values, "text", values)
                value = text[0] if isinstance(text, (list, tuple)) else text
                if isinstance(value, str):
                    return plain(value[:MAX_TEXT], MAX_TEXT)
        return ""
    title, artist, album = first("TIT2", "title", "\xa9nam"), first("TPE1", "artist", "\xa9ART"), first("TALB", "album", "\xa9alb")
    year, track_number = first("date", "year", "\xa9day"), first("TRCK", "tracknumber")
    if isinstance(tags, ID3) and tags.get("TDRC"):
        year = str(tags["TDRC"].text[0])[:32]
    if tags.get("trkn") and isinstance(tags["trkn"][0], (list, tuple)):
        track_number = str(tags["trkn"][0][0])
    lyrics = first("lyrics", "unsyncedlyrics", "\xa9lyr")
    if isinstance(tags, ID3):
        lyrics = next((str(x.text)[:MAX_TEXT] for x in tags.getall("USLT") if x.text), lyrics)
    art = bool(getattr(audio, "pictures", None) or tags.get("covr") or tags.get("metadata_block_picture") or tags.get("coverart"))
    if isinstance(tags, ID3):
        art = art or bool(tags.getall("APIC"))
    preview = ""
    try:
        if isinstance(tags, ID3) and tags.getall("APIC"):
            preview = artwork_preview(tags.getall("APIC")[0].data)
        elif getattr(audio, "pictures", None):
            preview = artwork_preview(audio.pictures[0].data)
        elif tags.get("covr"):
            preview = artwork_preview(tags["covr"][0])
        elif tags.get("metadata_block_picture"):
            from mutagen.flac import Picture
            raw = tags["metadata_block_picture"][0]
            if len(raw) < 12 * 1024 * 1024:
                preview = artwork_preview(Picture(base64.b64decode(raw)).data)
    except (ValueError, TypeError, IndexError):
        pass
    duration = float(getattr(audio.info, "length", 0))
    if not math.isfinite(duration) or not 0 <= duration <= 86400:
        duration = 0
    return {"title": title[:480], "artist": artist[:480], "album": album[:480], "lyrics": lyrics[:MAX_TEXT],
            "year": year[:32], "track_number": track_number[:32], "artwork": art, "cover_preview": preview,
            "duration": round(duration, 3), "variants": variants(title + " " + path.stem), "filename": path.stem}


def write_tags(path, changes):
    """Only touch reviewed fields; all other tags survive on the staged copy."""
    import mutagen
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, USLT, APIC
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    audio = mutagen.File(path)
    if audio is None:
        raise CurationError("Формат не поддерживает изменение тегов.")
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    for name in ("title", "artist", "album", "lyrics"):
        if name not in changes:
            continue
        value = changes[name]
        if isinstance(tags, ID3):
            cls, key = {"title": (TIT2, "TIT2"), "artist": (TPE1, "TPE1"), "album": (TALB, "TALB"), "lyrics": (USLT, "USLT")}[name]
            tags.delall(key)
            tags.add(cls(encoding=3, text=value, **({"lang": "xxx", "desc": ""} if name == "lyrics" else {})))
        elif isinstance(audio, MP4):
            tags[{"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "lyrics": "\xa9lyr"}[name]] = [value]
        else:
            tags[name] = [value]
    if changes.get("artwork_data"):
        art = base64.b64decode(changes["artwork_data"], validate=True)
        if isinstance(tags, ID3):
            tags.delall("APIC"); tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Front", data=art))
        elif isinstance(audio, MP4):
            tags["covr"] = [MP4Cover(art, imageformat=MP4Cover.FORMAT_JPEG)]
        else:
            picture = Picture(); picture.type = 3; picture.mime = "image/jpeg"; picture.data = art
            if isinstance(audio, FLAC):
                audio.clear_pictures(); audio.add_picture(picture)
            else:
                tags["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    audio.save()


def _json_get(url, params=None):
    global _last_musicbrainz_request
    import httpx
    if url.startswith("https://musicbrainz.org/"):
        time.sleep(max(0, 1.05 - (time.monotonic() - _last_musicbrainz_request)))
        _last_musicbrainz_request = time.monotonic()
    # Fixed public providers only; never reuse an agent credential or cookie jar.
    with httpx.Client(timeout=6, follow_redirects=False, trust_env=False,
                      headers={"User-Agent": "XASS-LibraryReview/1.0 (https://github.com/lucifervalter-a11y/XASS)"}) as client:
        deadline = time.monotonic() + 12
        with client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            body = bytearray()
            for block in response.iter_bytes():
                body.extend(block)
                if len(body) > 1024 * 1024 or time.monotonic() > deadline:
                    raise CurationError("Ответ каталога слишком велик.")
            return json.loads(body)


def lookup_lyrics(tags):
    """Reuse the project's conservative LRCLIB matching rule: exact names/length.

    Matching text and duration alone remains 'review', never fingerprint proof.
    No artwork is guessed from the first album search hit.
    """
    title, artist = tags["title"], tags["artist"]
    if not title or not artist:
        return {"status": "insufficient_metadata", "candidates": []}
    rows = _json_get("https://lrclib.net/api/search", {"track_name": title, "artist_name": artist})
    if not isinstance(rows, list) or len(rows) > 100:
        raise CurationError("Каталог вернул неподдерживаемый ответ.")
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int or not 0 < row["id"] < 2**63:
            continue
        got_title, got_artist, album = (plain(str(row.get(key) or "")[:480]) for key in ("trackName", "artistName", "albumName"))
        if norm(got_title) != norm(title) or norm(got_artist) != norm(artist) or variants(got_title) != tags["variants"]:
            continue
        duration = row.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (float, int)) or not math.isfinite(duration):
            continue
        difference = abs(duration - tags["duration"])
        exact_album = not tags["album"] or norm(tags["album"]) == norm(album)
        # No full-length synchronized lyrics on a different edit/cut.
        close = tags["duration"] > 0 and difference <= min(1.0, tags["duration"] * .01) and exact_album
        changes = {"title": got_title, "artist": got_artist}
        if album:
            changes["album"] = album
        lyrics = row.get("syncedLyrics") or row.get("plainLyrics") or ""
        if close and isinstance(lyrics, str) and len(lyrics) <= MAX_TEXT:
            stamps = [(int(a) * 60 + float(b)) for a, b in re.findall(r"\[(\d{1,3}):(\d{2}(?:\.\d{1,3})?)\]", lyrics)]
            if not stamps or max(stamps) <= duration + 2:
                changes["lyrics"] = plain(lyrics, MAX_TEXT)
        conflicts = [] if exact_album else ["Альбом отличается от исходного"]
        candidates.append({"changes": changes, "source": "LRCLIB", "source_id": str(row["id"]),
                           **match_rating(difference=difference if tags["duration"] > 0 else None, conflicts=conflicts, catalog=True),
                           "evidence": ["Название и исполнитель совпадают", f"Разница длительности: {difference:.1f} с", "Акустический отпечаток не проверялся", *conflicts]})
        if len(candidates) == 5:
            break
    return {"status": "found" if candidates else "not_found", "candidates": candidates}


def cover_thumbnail(release):
    """Fetch only a selected MusicBrainz release's bounded public CAA image."""
    if not re.fullmatch(r"[0-9a-f-]{36}", release):
        return ""
    import httpx
    from music_player import _jpeg_artwork
    url = f"https://coverartarchive.org/release/{release}/front-500"
    with httpx.Client(timeout=6, follow_redirects=False, trust_env=False) as client:
        deadline = time.monotonic() + 12
        for _ in range(3):
            if time.monotonic() > deadline:
                return ""
            with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = response.headers.get("location", "")
                    parsed = urlsplit(url)
                    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or not (parsed.hostname == "archive.org" or (parsed.hostname or "").endswith(".archive.org")):
                        return ""
                    continue
                if response.status_code != 200:
                    return ""
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024 or time.monotonic() > deadline:
                        return ""
                art = _jpeg_artwork(bytes(body))
                return base64.b64encode(art).decode("ascii") if art else ""
    return ""


def search_terms(tags):
    title = tags["title"] or tags.get("filename", "")
    title = re.sub(r"\.(?:mp3|flac|ogg|opus|wav|m4a)$", "", title, flags=re.I).replace("_", " ")
    title = re.sub(r"^\d{1,3}\s*[.)-]\s+", "", title).strip()
    artist = tags["artist"]
    parts = re.split(r"\s+[-–—]\s+", title)
    if len(parts) == 2 and not artist:
        artist, title = parts
    return {**tags, "title": title, "artist": artist, "variants": sorted(set(tags.get("variants", [])) | set(variants(title)))}


def local_fingerprint(path):
    """The explicitly installed fpcalc works locally, without a provider key."""
    executable = os.environ.get("XASS_FPCALC", "")
    if not executable:
        return None
    binary = safe(Path(executable))
    if not binary.is_file():
        raise CurationError("Локальный fpcalc не найден. Проверьте настройку XASS_FPCALC.")
    result = subprocess.run([str(binary), "-json", "-length", "120", str(path)], capture_output=True, timeout=45,
                            creationflags=0x08000000 if os.name == "nt" else 0)
    if result.returncode != 0 or len(result.stdout) > 65536:
        raise CurationError("Локальный акустический отпечаток не удалось получить.")
    fingerprint = json.loads(result.stdout)
    duration = fingerprint.get("duration")
    value = fingerprint.get("fingerprint")
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 86400 or not isinstance(value, str) or not 1 < len(value) < 60000:
        raise CurationError("Некорректный акустический отпечаток.")
    return {"duration": duration, "fingerprint": value}


def acoustic_recordings(path, fingerprint=None):
    """Optional owner's provider key. No implicit executable or key install."""
    key = os.environ.get("XASS_ACOUSTID_KEY", "")
    if not key or len(key) > 128:
        return [], "AcoustID не настроен: нет ключа. Локальное сравнение отпечатков не требует ключа; доступен поиск по тегам."
    fingerprint = fingerprint or local_fingerprint(path)
    if fingerprint is None:
        return [], "Акустическое распознавание не настроено: нет локального fpcalc. Доступен поиск по тегам."
    duration, value = fingerprint["duration"], fingerprint["fingerprint"]
    import httpx
    with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
        deadline = time.monotonic() + 12
        with client.stream("POST", "https://api.acoustid.org/v2/lookup", data={"client": key, "duration": str(round(duration)), "fingerprint": value, "meta": "recordings"}) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 1024 * 1024 or time.monotonic() > deadline:
                    raise CurationError("Ответ AcoustID слишком велик.")
            body = json.loads(content)
    if body.get("status") != "ok" or not isinstance(body.get("results"), list):
        raise CurationError("AcoustID не подтвердил результат.")
    ids = []
    for row in body["results"][:10]:
        score = row.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            continue
        for recording in row.get("recordings", [])[:10]:
            identity_ = recording.get("id", "")
            if re.fullmatch(r"[0-9a-f-]{36}", identity_) and identity_ not in [x[0] for x in ids]:
                ids.append((identity_, score))
    return ids[:3], "AcoustID сравнил акустический отпечаток. Его оценка сходства не является вероятностью правильного распознавания."


def lookup(tags, acoustic=None):
    """Real provider candidates; no title, cover or lyric is silently applied."""
    query = search_terms(tags)
    deadline = time.monotonic() + 48
    candidates, warnings = [], []
    if query["title"] != tags["title"] or query["artist"] != tags["artist"]:
        candidates.append({"changes": {"title": query["title"], "artist": query["artist"]}, "source": "Имя файла",
                           "source_id": "", **match_rating(), "evidence": ["Разделители имени файла разобраны локально", "Название и исполнитель не подтверждены каталогом"]})
    try:
        candidates.extend(lookup_lyrics(query)["candidates"])
    except Exception:
        warnings.append("LRCLIB сейчас недоступен; другие результаты сохранены.")
    try:
        if acoustic:
            rows = []
            for recording, score in acoustic:
                if time.monotonic() > deadline:
                    break
                row = _json_get(f"https://musicbrainz.org/ws/2/recording/{recording}", {"fmt": "json", "inc": "artists+releases"})
                row["acoustid_score"] = score; rows.append(row)
        else:
            def quoted(value):
                return value.replace("\\", "\\\\").replace('"', '\\"')
            expression = f'recording:"{quoted(query["title"])}"'
            if query["artist"]:
                expression += f' AND artist:"{quoted(query["artist"])}"'
            body = _json_get("https://musicbrainz.org/ws/2/recording", {"fmt": "json", "limit": "8", "query": expression})
            rows = body.get("recordings", [])
        if not isinstance(rows, list) or len(rows) > 100:
            raise CurationError("Некорректный ответ MusicBrainz.")
        count = 0
        for row in rows:
            if time.monotonic() > deadline:
                warnings.append("Лимит времени поиска достигнут; найденные варианты сохранены.")
                break
            if not isinstance(row, dict) or not re.fullmatch(r"[0-9a-f-]{36}", str(row.get("id", ""))):
                continue
            title = plain(str(row.get("title") or "")[:480])
            artist = "".join(str(credit.get("name") or (credit.get("artist") or {}).get("name") or "") + str(credit.get("joinphrase") or "")
                             for credit in row.get("artist-credit", [])[:16] if isinstance(credit, dict))[:480]
            if not title or not artist or variants(title + " " + str(row.get("disambiguation") or "")) != query["variants"]:
                continue
            if "acoustid_score" not in row and (SequenceMatcher(None, norm(title), norm(query["title"])).ratio() < .65
                    or (query["artist"] and SequenceMatcher(None, norm(artist), norm(query["artist"])).ratio() < .7)):
                continue
            duration = row.get("length")
            difference = abs(duration / 1000 - tags["duration"]) if type(duration) in (int, float) and math.isfinite(duration) and duration > 0 and tags["duration"] > 0 else None
            releases = [r for r in row.get("releases", [])[:50] if isinstance(r, dict) and r.get("status") == "Official"
                        and re.fullmatch(r"[0-9a-f-]{36}", str(r.get("id", ""))) and r.get("title")]
            if query["album"]:
                releases = [r for r in releases if norm(str(r["title"])) == norm(query["album"])]
            albums = {norm(str(r["title"])) for r in releases}
            release = releases[0] if len(albums) == 1 and releases else None
            changes = {"title": title, "artist": artist}
            evidence = ["Версионные пометки сохранены", "Длительность каталога неизвестна" if difference is None else f"Разница длительности: {difference:.1f} с"]
            if "acoustid_score" in row:
                evidence.append(f"Оценка сходства AcoustID: {row['acoustid_score']:.3f}; не вероятность")
            else:
                evidence.append("Только поиск по тегам, акустический отпечаток не проверялся")
            candidate = {"changes": changes, "source": "AcoustID / MusicBrainz" if "acoustid_score" in row else "MusicBrainz",
                         "source_id": row["id"], "evidence": evidence}
            if difference is not None and difference <= 1 and time.monotonic() < deadline:
                try:
                    lyric_choices = lookup_lyrics({**query, "title": title, "artist": artist, "album": ""})["candidates"]
                    usable = [choice for choice in lyric_choices if choice["changes"].get("lyrics")]
                    # Ambiguous lyrics versions remain separate suggestions.
                    texts = {choice["changes"]["lyrics"] for choice in usable}
                    if len(texts) == 1:
                        changes["lyrics"] = next(iter(texts))
                        candidate["evidence"].append("Текст: LRCLIB, название и длительность выбранной записи совпадают")
                        candidate["lyric_source"] = "LRCLIB"
                except Exception:
                    pass
            if release:
                changes["album"] = str(release["title"])[:480]
                try:
                    art = cover_thumbnail(release["id"])
                    if art:
                        candidate["artwork_data"] = art
                        candidate["evidence"].append("Обложка: Cover Art Archive, выбранный официальный альбом " + changes["album"])
                except Exception:
                    warnings.append("Обложку выбранного альбома получить не удалось.")
            conflicts = []
            if query["album"] and not release:
                conflicts.append("Исходный альбом не подтверждён выбранным изданием")
            if tags.get("year"):
                original_year = re.match(r"\d{4}", tags["year"])
                release_year = re.match(r"\d{4}", str((release or {}).get("date", "")))
                if not original_year or not release_year or original_year[0] != release_year[0]:
                    conflicts.append("Год отличается или не подтверждён каталогом")
            if tags.get("track_number"):
                conflicts.append("Номер трека в этом издании не подтверждён каталогом")
            if candidate.get("artwork_data"):
                conflicts.append("Обложка издания требует просмотра: акустический отпечаток её не подтверждает")
            candidate.update(match_rating(source_score=row.get("acoustid_score"), difference=difference, conflicts=conflicts, catalog=True))
            candidate["evidence"].extend(conflicts)
            candidates.append(candidate); count += 1
            if count == 3:
                break
    except Exception:
        warnings.append("MusicBrainz сейчас недоступен; остальные результаты сохранены.")
    return {"status": "found" if candidates else "not_found", "candidates": candidates[:8], "warnings": warnings}


class CurationService:
    def __init__(self, data, *, provider=lookup):
        self.root = safe(Path(data)) / "music-curation"
        safe(self.root); self.root.mkdir(exist_ok=True)
        self.provider = provider

    @contextlib.contextmanager
    def database(self):
        from native_music_ownership import FileLock
        lock = FileLock(self.root / "review.lock")
        if not lock.acquire():
            raise CurationError("Разбор медиатеки уже выполняет действие. Повторите позже.")
        try:
            db_path = safe(self.root / "review.sqlite")
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY, path TEXT NOT NULL, digest TEXT, info TEXT);
                CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, track TEXT NOT NULL, digest TEXT NOT NULL, changes TEXT NOT NULL, evidence TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS edits(id TEXT PRIMARY KEY, path TEXT NOT NULL, before_hash TEXT NOT NULL, after_hash TEXT NOT NULL, backup TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS fingerprints(track TEXT PRIMARY KEY, digest TEXT NOT NULL, fingerprint TEXT NOT NULL, duration REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS scan(id INTEGER PRIMARY KEY CHECK(id=1), folder TEXT NOT NULL);""")
            try:
                yield db
            finally:
                db.close()
        finally:
            lock.release()

    def start(self, folder):
        folder = safe(Path(plain(folder, 32760)))
        if not folder.is_dir():
            raise CurationError("Папка не найдена.")
        paths, visited, truncated = [], 0, False
        for current, dirs, files in os.walk(folder, followlinks=False):
            visited += len(dirs) + len(files)
            dirs[:] = sorted(d for d in dirs if not (Path(current) / d).is_symlink()
                             and not getattr((Path(current) / d).lstat(), "st_file_attributes", 0) & 0x400)
            for name in sorted(files):
                path = Path(current) / name
                if path.suffix.lower() not in EXTENSIONS:
                    continue
                try:
                    safe(path)
                    if path.stat().st_nlink != 1:
                        continue
                except (OSError, CurationError):
                    continue
                paths.append(path)
                if len(paths) >= MAX_TRACKS:
                    truncated = True; break
            if truncated or visited > 20000:
                truncated = True; break
        with self.database() as db:
            db.execute("DELETE FROM proposals"); db.execute("DELETE FROM fingerprints"); db.execute("DELETE FROM tracks")
            db.execute("INSERT OR REPLACE INTO scan VALUES(1, ?)", (str(folder),))
            rows = [{"id": uuid.uuid4().hex, "name": str(path.relative_to(folder))} for path in paths]
            db.executemany("INSERT INTO tracks(id,path) VALUES(?,?)", [(row["id"], str(path)) for row, path in zip(rows, paths)])
            db.commit()
        return {"tracks": rows, "total": len(rows), "truncated": truncated}

    def _track(self, db, key):
        key = plain(key, 32)
        row = db.execute("SELECT * FROM tracks WHERE id=?", (key,)).fetchone()
        if row is None:
            raise CurationError("Список файлов изменился. Повторите разбор.")
        path = safe(Path(row["path"]))
        folder = safe(Path(db.execute("SELECT folder FROM scan WHERE id=1").fetchone()[0]))
        if not path.is_relative_to(folder):
            raise CurationError("Файл вне выбранной папки.")
        return row, path

    def inspect(self, key):
        with self.database() as db:
            row, path = self._track(db, key)
            digest = identity(path)
            info = _read_tags(path)
            if identity(path) != digest:
                raise CurationError("Файл изменился во время чтения тегов.")
            issues = []
            for field, label in (("title", "Нет названия"), ("artist", "Нет исполнителя"), ("artwork", "Нет обложки"), ("lyrics", "Нет текста")):
                if not info[field]:
                    issues.append(label)
            if info["lyrics"] and not re.search(r"\[\d{1,3}:\d{2}(?:[.:]\d+)?\]", info["lyrics"]):
                issues.append("Текст без таймингов")
            if "_" in info["title"] or re.search(r"\.(mp3|flac|wav|ogg|m4a)$", info["title"], re.I):
                issues.append("Проверьте название")
            db.execute("UPDATE tracks SET digest=?,info=? WHERE id=?", (digest, json.dumps(info), key)); db.commit()
            cached = [self._proposal_view(p["id"], info, json.loads(p["changes"]), json.loads(p["evidence"]))
                      for p in db.execute("SELECT * FROM proposals WHERE track=? AND digest=? LIMIT 8", (key, digest))]
            return {"id": key, "name": path.name, "issues": issues, "tags": info, "digest": digest, "proposals": cached}

    def candidates(self, key):
        with self.database() as db:
            row, path = self._track(db, key)
            if not row["digest"] or identity(path) != row["digest"]:
                raise CurationError("Файл изменился. Сначала повторите проверку.")
            before = json.loads(row["info"])
        # Slow, opt-in provider lookup holds no database lock; re-check on insert.
        acoustic, acoustic_notice = [], "Акустический отпечаток не проверялся."
        if self.provider is lookup:
            try:
                fingerprint = local_fingerprint(path)
                if fingerprint:
                    self._save_fingerprint(key, row["digest"], fingerprint)
                acoustic, acoustic_notice = acoustic_recordings(path, fingerprint) if fingerprint else ([], "Локальный fpcalc не настроен. Доступен поиск по тегам.")
            except Exception:
                acoustic_notice = "AcoustID недоступен. Результаты поиска по тегам требуют проверки."
            result = self.provider(before, acoustic)
        else:
            result = self.provider(before)
        with self.database() as db:
            # Replacing catalog suggestions never changes audio or undo history.
            db.execute("DELETE FROM proposals WHERE track=?", (key,)); db.commit()
        views = []
        for candidate in result.get("candidates", [])[:8]:
            try:
                evidence = {k: candidate[k] for k in ("source", "source_id", "confidence", "evidence")}
                evidence.update({k: candidate[k] for k in ("match_score", "source_score", "score_contract") if k in candidate})
                if candidate.get("lyric_source"):
                    evidence["lyric_source"] = candidate["lyric_source"]
                views.append(self.propose(key, candidate["changes"], evidence=evidence, artwork_data=candidate.get("artwork_data", ""), expected_digest=row["digest"]))
            except CurationError as error:
                if str(error) != "Новых изменений нет.":
                    raise
        return {"status": result["status"], "proposals": views, "notices": [acoustic_notice, *result.get("warnings", [])]}

    def _save_fingerprint(self, key, digest, fingerprint):
        with self.database() as db:
            current, path = self._track(db, key)
            if current["digest"] != digest or identity(path) != digest:
                raise CurationError("Файл изменился во время сравнения звука. Повторите проверку.")
            # Only a local comparison key is persisted, never the provider key.
            value = hashlib.sha256(("chromaprint:120:" + fingerprint["fingerprint"]).encode()).hexdigest()
            db.execute("INSERT OR REPLACE INTO fingerprints VALUES(?,?,?,?)", (key, digest, value, fingerprint["duration"])); db.commit()

    def fingerprint(self, key):
        with self.database() as db:
            row, path = self._track(db, key)
            if not row["digest"] or identity(path) != row["digest"]:
                raise CurationError("Файл изменился. Сначала повторите проверку.")
        fingerprint = local_fingerprint(path)
        if fingerprint is None:
            return {"available": False, "notice": "Локальный fpcalc не настроен. Укажите XASS_FPCALC; ничего не скачивалось и в сеть не отправлялось."}
        self._save_fingerprint(key, row["digest"], fingerprint)
        return {"available": True, "notice": "Первые 120 секунд сравниваются локально. Совпадение — только кандидат для прослушивания, не разрешение на удаление."}

    def propose(self, key, changes, *, evidence=None, artwork_path="", artwork_data="", expected_digest=None):
        if not isinstance(changes, dict) or set(changes) - {"title", "artist", "album", "lyrics"}:
            raise CurationError("Недопустимые поля правки.")
        changes = {k: plain(v, MAX_TEXT if k == "lyrics" else 480) for k, v in changes.items()}
        if artwork_path:
            from music_player import _jpeg_artwork
            art_path = safe(Path(plain(artwork_path, 32760)))
            if not art_path.is_file() or not 0 < art_path.stat().st_size <= 8 * 1024 * 1024:
                raise CurationError("Выберите изображение до 8 МБ.")
            art = _jpeg_artwork(art_path.read_bytes())
            if not art:
                raise CurationError("Не удалось прочитать изображение.")
            changes["artwork_data"] = base64.b64encode(art).decode("ascii")
        elif artwork_data:
            from music_player import _jpeg_artwork
            if not isinstance(artwork_data, str) or len(artwork_data) > 1024 * 1024:
                raise CurationError("Обложка слишком велика.")
            art = _jpeg_artwork(base64.b64decode(artwork_data, validate=True))
            if not art:
                raise CurationError("Обложка каталога не распознана.")
            changes["artwork_data"] = base64.b64encode(art).decode("ascii")
        evidence = evidence or {"source": "Ручная правка", "source_id": "", "confidence": "manual", "evidence": ["Поля выбраны пользователем"]}
        if evidence["confidence"] != "manual" and "match_score" not in evidence:
            # Old/custom proposals never become green without the evidence rule.
            evidence = {**evidence, "match_score": 65 if evidence["confidence"] == "review" else 40,
                        "source_score": None, "score_contract": 1}
        with self.database() as db:
            row, path = self._track(db, key)
            if not row["digest"] or identity(path) != row["digest"] or (expected_digest is not None and row["digest"] != expected_digest):
                raise CurationError("Файл изменился. Повторите проверку перед правкой.")
            before = json.loads(row["info"])
            changes = {k: v for k, v in changes.items() if k == "artwork_data" or before.get(k) != v}
            if not changes:
                raise CurationError("Новых изменений нет.")
            proposal = uuid.uuid4().hex
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,?)", (proposal, key, row["digest"], json.dumps(changes), json.dumps(evidence))); db.commit()
            return self._proposal_view(proposal, before, changes, evidence)

    @staticmethod
    def _proposal_view(key, before, changes, evidence):
        if evidence.get("confidence") != "manual" and "match_score" not in evidence:
            evidence = {**evidence, "match_score": 40, "confidence": "weak", "source_score": None, "score_contract": 1}
        after = {**before, **{k: v for k, v in changes.items() if k != "artwork_data"}}
        if "artwork_data" in changes:
            after["artwork"] = True
            after["cover_preview"] = artwork_preview(base64.b64decode(changes["artwork_data"]))
        return {"id": key, "before": before, "after": after, "artwork_changed": "artwork_data" in changes,
                "field_sources": {k.replace("artwork_data", "artwork"): ("Cover Art Archive" if k == "artwork_data" and evidence["source"] != "Ручная правка" else evidence.get("lyric_source", evidence["source"]) if k == "lyrics" else evidence["source"]) for k in changes}, **evidence}

    def transcribe(self, key, model_path):
        with self.database() as db:
            row, path = self._track(db, key)
            if not row["digest"] or identity(path) != row["digest"]:
                raise CurationError("Файл изменился. Повторите проверку перед расшифровкой.")
        # Reuse the explicitly configured offline model and karaoke line builder.
        # No installer, model download, microphone or server job is started here.
        from voice_capture import WhisperTranscriber
        from transcribe_runner import build_lines
        model = safe(Path(plain(model_path, 32760)))
        recognizer = WhisperTranscriber(str(model))
        segments, _ = recognizer.model.transcribe(str(path), beam_size=5, word_timestamps=True,
                                                vad_filter=False, condition_on_previous_text=False)
        lines = build_lines(segments)
        if not lines:
            raise CurationError("Локальная модель не распознала текст уверенно. Исходный текст сохранён.")
        rendered = []
        for line in lines:
            stamp = line["start"]
            if not math.isfinite(stamp) or not 0 <= stamp <= 86400:
                continue
            rendered.append(f"[{int(stamp) // 60:02d}:{stamp % 60:05.2f}]{line['text']}")
            if len("\n".join(rendered)) > MAX_TEXT:
                raise CurationError("Распознанный текст превышает допустимый размер.")
        return self.propose(key, {"lyrics": "\n".join(rendered)}, expected_digest=row["digest"], evidence={"source": "Локальный Whisper", "source_id": model.name,
            "confidence": "weak", "evidence": ["Текст и тайминги распознаны на этом ПК", "Результат модели может содержать ошибки; сравните перед записью", "Исходный текст не меняется до подтверждения"]})

    def duplicates(self):
        with self.database() as db:
            rows = list(db.execute("SELECT * FROM tracks WHERE info IS NOT NULL"))
            fingerprints = {row["track"]: row for row in db.execute("SELECT * FROM fingerprints")}
        exact, possible, acoustic = {}, {}, {}
        for row in rows:
            info = json.loads(row["info"])
            value = {"id": row["id"], "name": Path(row["path"]).name, "duration": info["duration"], "variants": info["variants"]}
            exact.setdefault(row["digest"], []).append(value)
            if info["title"] and info["artist"]:
                possible.setdefault((norm(info["title"]), norm(info["artist"]), tuple(info["variants"])), []).append(value)
            fingerprint = fingerprints.get(row["id"])
            if fingerprint is not None and fingerprint["digest"] == row["digest"]:
                acoustic.setdefault((fingerprint["fingerprint"], tuple(info["variants"])), []).append({**value, "duration": fingerprint["duration"]})
        exact_sets = {tuple(sorted(x["id"] for x in group)) for group in exact.values() if len(group) > 1}
        result = [{"kind": "exact_bytes", "reason": "Файлы совпадают побайтово (SHA-256). Ничего не удалено.", "tracks": group}
                  for group in exact.values() if len(group) > 1]
        for group in possible.values():
            if len(group) > 1 and tuple(sorted(x["id"] for x in group)) not in exact_sets:
                result.append({"kind": "candidate", "reason": "Совпали названия и версии. Звук не сравнивался; разница длительности не доказывает дубликат или другую запись.", "tracks": group})
        for matches in acoustic.values():
            # Cluster conservatively: no group spans more than one second.
            group = []
            for value in sorted(matches, key=lambda item: item["duration"]) + [None]:
                if group and (value is None or value["duration"] - group[0]["duration"] > 1):
                    if len(group) > 1 and tuple(sorted(x["id"] for x in group)) not in exact_sets:
                        result.append({"kind": "fingerprint_candidate", "reason": "Совпали локальные отпечатки первых 120 с и длительность (±1 с). Это не побайтовая копия: прослушайте обе версии. Файлы не удаляются.", "tracks": group})
                    group = []
                if value is not None:
                    group.append(value)
        return {"groups": result[:200], "truncated": len(result) > 200}

    def _recover(self, db):
        for row in db.execute("SELECT * FROM edits WHERE state IN ('prepared','undo_prepared')").fetchall():
            try:
                current = identity(Path(row["path"]))
                if current == row["after_hash"]:
                    state = "applied"
                elif current == row["before_hash"]:
                    state = "reverted" if row["state"] == "undo_prepared" else "not_applied"
                else:
                    state = "conflict"
            except (OSError, CurationError):
                state = "conflict"
            db.execute("UPDATE edits SET state=? WHERE id=?", (state, row["id"]))
        db.commit()

    def history(self):
        with self.database() as db:
            self._recover(db)
            return {"edits": [{"id": row["id"], "name": Path(row["path"]).name, "state": row["state"], "created": row["created"]}
                              for row in db.execute("SELECT * FROM edits ORDER BY created DESC LIMIT 100")]}

    def apply(self, proposal, confirmed, manual_choice=False):
        if confirmed is not True:
            raise CurationError("Сначала подтвердите просмотренную правку.")
        with self.database() as db:
            self._recover(db)
            pending = db.execute("SELECT * FROM proposals WHERE id=?", (plain(proposal, 32),)).fetchone()
            if pending is None:
                raise CurationError("Правка уже применена или устарела.")
            evidence = json.loads(pending["evidence"])
            if evidence.get("confidence") != "manual" and evidence.get("match_score", 40) < 60 and manual_choice is not True:
                raise CurationError("Слабое совпадение: сначала явно выберите этот вариант вручную после сравнения.")
            _, path = self._track(db, pending["track"])
            before_hash = identity(path)
            if before_hash != pending["digest"]:
                raise CurationError("Файл был изменён после просмотра. Ничего не записано.")
            used = sum(p.stat().st_size for p in self.root.glob("*.original") if p.is_file())
            if used + path.stat().st_size > MAX_BACKUPS:
                raise CurationError("Журнал оригиналов достиг 2 ГБ. Правка не выполнена; оригиналы сохранены.")
            edit = uuid.uuid4().hex
            backup = self.root / (edit + ".original")
            shutil.copy2(path, backup)
            with backup.open("r+b") as stream:
                os.fsync(stream.fileno())
            if identity(backup) != before_hash:
                raise CurationError("Оригинал изменился во время копирования. Правка отменена.")
            fd, stage_name = tempfile.mkstemp(prefix=".xass-curation-", suffix=path.suffix, dir=path.parent)
            os.close(fd); stage = Path(stage_name)
            try:
                shutil.copy2(backup, stage)
                if identity(stage) != before_hash:
                    raise CurationError("Подготовленная копия оригинала повреждена. Правка отменена.")
                write_tags(stage, json.loads(pending["changes"]))
                after_hash = identity(stage)
                with stage.open("r+b") as stream:
                    os.fsync(stream.fileno())
                if identity(backup) != before_hash:
                    raise CurationError("Оригинал в журнале изменился. Правка отменена.")
                # Commit redo/undo evidence BEFORE the atomic rename. On killed
                # processes recovery identifies the actual state from full hashes.
                db.execute("INSERT INTO edits VALUES(?,?,?,?,?,?,?)", (edit, str(path), before_hash, after_hash, backup.name, "prepared", time.time())); db.commit()
                if identity(path) != before_hash:
                    raise CurationError("Файл изменился во время подготовки. Правка отменена.")
                os.replace(stage, safe(path))
                db.execute("UPDATE edits SET state='applied' WHERE id=?", (edit,))
                db.execute("DELETE FROM proposals WHERE track=?", (pending["track"],))
                db.commit()
                return {"id": edit, "state": "applied"}
            finally:
                stage.unlink(missing_ok=True)

    def undo(self, edit, confirmed):
        if confirmed is not True:
            raise CurationError("Подтвердите восстановление оригинала.")
        with self.database() as db:
            self._recover(db)
            row = db.execute("SELECT * FROM edits WHERE id=?", (plain(edit, 32),)).fetchone()
            if row is None or row["state"] != "applied":
                raise CurationError("Эта правка не доступна для отката.")
            path = safe(Path(row["path"]))
            if identity(path) != row["after_hash"]:
                raise CurationError("После правки файл изменён. Откат заблокирован, чтобы сохранить новые изменения.")
            backup = safe(self.root / row["backup"])
            if backup.parent != self.root or identity(backup) != row["before_hash"]:
                raise CurationError("Оригинал в журнале повреждён. Откат заблокирован.")
            fd, stage_name = tempfile.mkstemp(prefix=".xass-curation-undo-", suffix=path.suffix, dir=path.parent)
            os.close(fd); stage = Path(stage_name)
            try:
                shutil.copy2(backup, stage)
                with stage.open("r+b") as stream:
                    os.fsync(stream.fileno())
                if identity(stage) != row["before_hash"]:
                    raise CurationError("Подготовленная копия оригинала повреждена. Откат заблокирован.")
                db.execute("UPDATE edits SET state='undo_prepared' WHERE id=?", (edit,)); db.commit()
                if identity(path) != row["after_hash"]:
                    raise CurationError("Файл изменился во время отката. Ничего не заменено.")
                os.replace(stage, path)
                db.execute("UPDATE edits SET state='reverted' WHERE id=?", (edit,)); db.commit()
                return {"id": edit, "state": "reverted"}
            finally:
                stage.unlink(missing_ok=True)

    def request(self, action, request):
        try:
            if action == "desktop_curation_start":
                return self.start(request.get("folder"))
            if action == "desktop_curation_inspect":
                return self.inspect(request.get("id"))
            if action == "desktop_curation_candidates":
                return self.candidates(request.get("id"))
            if action == "desktop_curation_transcribe":
                return self.transcribe(request.get("id"), request.get("model_path"))
            if action == "desktop_curation_fingerprint":
                return self.fingerprint(request.get("id"))
            if action == "desktop_curation_propose":
                digest = plain(request.get("digest"), 64)
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise CurationError("Сначала заново выберите проверенный трек.")
                return self.propose(request.get("id"), request.get("changes"), artwork_path=request.get("artwork_path", ""), expected_digest=digest)
            if action == "desktop_curation_duplicates":
                return self.duplicates()
            if action == "desktop_curation_history":
                return self.history()
            if action == "desktop_curation_apply":
                return self.apply(request.get("id"), request.get("confirmed"), request.get("manual_choice", False))
            if action == "desktop_curation_undo":
                return self.undo(request.get("id"), request.get("confirmed"))
            raise CurationError("Неизвестное действие.")
        except CurationError as error:
            return {"error": str(error)}
        except Exception:
            return {"error": "Действие не завершено. Проверьте доступ к файлу или каталогу; оригинал и журнал сохранены."}
