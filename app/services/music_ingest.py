"""Owner-only Telegram music ingestion into the ordinary private MusicTrack library.

Telegram downloads never use the unbounded generic bot download helper. ZIP names
are metadata only: no archive-supplied path is ever used as an extraction target.
Receipts contain track IDs, not Telegram credentials, and survive a service restart.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import struct
import tempfile
import time
import unicodedata
from urllib.parse import quote
import zipfile

import httpx
from sqlalchemy import select

from app.music_models import MusicTrack
from app.services.music_library import FORMATS, content_lock, display_title, filename, inspect_audio, track_path
from app.services.music_storage import lock_content, lock_track

logger = logging.getLogger(__name__)
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024
ZIP_MAX_ENTRIES = 500
ZIP_MAX_TRACKS = 200
ZIP_MAX_EXPANDED = 512 * 1024 * 1024
ZIP_MAX_RATIO = 100
ZIP_MAX_DIRECTORY_BYTES = 1024 * 1024
MAX_BATCH_MESSAGES = 100
MAX_PENDING_MESSAGES = 500
IO_CHUNK = 128 * 1024
IO_DEADLINE = 120.0
RECEIPT_MAX_BYTES = 128 * 1024
NESTED_ARCHIVES = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz"}


class IngestError(ValueError):
    """A safe, user-facing error; never put transport URLs or tokens here."""


def clean_text(value, limit=240):
    value = unicodedata.normalize("NFC", str(value or ""))
    return "".join(char for char in value if not unicodedata.category(char).startswith("C")).strip()[:limit]


@dataclass(frozen=True)
class Attachment:
    chat_id: int
    message_id: int
    file_id: str
    unique_id: str
    name: str
    size: int
    title: str = ""
    artist: str = ""

    def receipt_keys(self):
        identities = [f"message:{self.chat_id}:{self.message_id}"]
        if self.unique_id:
            identities.append(f"file:{self.chat_id}:{self.unique_id}")
        return [hashlib.sha256(identity.encode()).hexdigest() for identity in identities]


def music_attachment(message: dict, owner_id: int) -> Attachment | None:
    """Ignore other users, groups, business chats, voice notes and unrelated files."""
    chat, sender = message.get("chat") or {}, message.get("from") or {}
    if (not owner_id or sender.get("id") != owner_id or sender.get("is_bot")
            or chat.get("id") != owner_id or chat.get("type") != "private"
            or message.get("business_connection_id")):
        return None
    audio = message.get("audio")
    media = audio if isinstance(audio, dict) else message.get("document")
    if not isinstance(media, dict) or not media.get("file_id"):
        return None
    name = clean_text(media.get("file_name") or "").replace("\\", "/").rsplit("/", 1)[-1]
    mime = str(media.get("mime_type") or "").lower()
    if not name and audio:
        suffix = next((extension for extension, content_type in FORMATS.items() if content_type == mime), "")
        name = (clean_text(media.get("title"), 180) or "Аудио") + suffix
    extension = Path(name).suffix.lower()
    if not audio and extension not in FORMATS and extension != ".zip" and not mime.startswith("audio/"):
        return None
    try:
        message_id, size = int(message.get("message_id", 0)), int(media.get("file_size") or 0)
    except (TypeError, ValueError):
        return None
    if message_id <= 0 or size < 0:
        return None
    return Attachment(owner_id, message_id, str(media["file_id"])[:1024],
                      str(media.get("file_unique_id") or "")[:512], name or "Аудиофайл",
                      size, clean_text(media.get("title")), clean_text(media.get("performer")))


@dataclass
class ImportResult:
    added: list[int] = field(default_factory=list)
    existing: list[int] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    ordered: list[int] = field(default_factory=list)
    removed: int = 0
    restored: int = 0

    def merge(self, other):
        self.added.extend(other.added)
        self.existing.extend(other.existing)
        self.skipped += other.skipped
        self.errors.extend(other.errors)
        self.ordered.extend(other.ordered)
        self.removed += other.removed
        self.restored += other.restored


def _no_link(path: Path):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise IngestError("Каталог музыки содержит небезопасную ссылку")


def private_root(settings) -> Path:
    root = Path(settings.music_root).absolute()
    repo = Path(__file__).resolve().parents[2]
    if root == repo or root in repo.parents:
        raise IngestError("Укажите отдельный приватный каталог музыки")
    for part in [root, *root.parents]:
        _no_link(part)
    root = root.resolve()
    if root == repo or root in repo.parents:
        raise IngestError("Укажите отдельный приватный каталог музыки")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        root.chmod(0o700)
    return root


def _private_dir(path: Path):
    _no_link(path)
    path.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _new_file(path: Path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600), "wb")


def _require_space(root: Path, size: int, reserve: int):
    if shutil.disk_usage(root).free < size + reserve:
        raise IngestError("На сервере недостаточно места для музыки")


def _zip_plan(archive: zipfile.ZipFile, max_track_bytes: int):
    infos = archive.infolist()
    if len(infos) > ZIP_MAX_ENTRIES:
        raise IngestError(f"В ZIP больше {ZIP_MAX_ENTRIES} записей")
    total, tracks, skipped, seen = 0, [], 0, set()
    for info in infos:
        name = info.orig_filename
        path = PurePosixPath(name)
        if (not name or len(name) > 1024 or "\\" in name or "\x00" in name or path.is_absolute()
                or any(part in {"..", "."} or ":" in part for part in name.rstrip("/").split("/"))
                or any(not part for part in name.rstrip("/").split("/"))):
            raise IngestError("ZIP содержит небезопасный путь")
        identity = unicodedata.normalize("NFC", name).casefold().rstrip("/")
        if identity in seen:
            raise IngestError("ZIP содержит повторяющиеся пути")
        seen.add(identity)
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or (kind == stat.S_IFDIR and not info.is_dir()):
            raise IngestError("Ссылки и специальные файлы в ZIP запрещены")
        if info.flag_bits & 1:
            raise IngestError("ZIP с паролем не поддерживается")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise IngestError("Сохраните ZIP с обычным сжатием Deflate")
        total += info.file_size
        if (info.file_size < 0 or info.compress_size < 0 or total > ZIP_MAX_EXPANDED
                or info.file_size > max(1, info.compress_size) * ZIP_MAX_RATIO):
            raise IngestError("ZIP превышает безопасный размер или степень сжатия")
        if info.is_dir():
            continue
        extension = path.suffix.lower()
        if extension in NESTED_ARCHIVES:
            raise IngestError("Архивы внутри ZIP не поддерживаются")
        if extension not in FORMATS:
            skipped += 1
            continue
        if not 0 < info.file_size <= max_track_bytes:
            raise IngestError("Аудиофайл внутри ZIP превышает допустимый размер")
        tracks.append(info)
    if len(tracks) > ZIP_MAX_TRACKS:
        raise IngestError(f"В одном ZIP допускается до {ZIP_MAX_TRACKS} аудиофайлов")
    if not tracks:
        raise IngestError("В ZIP нет MP3, WAV, FLAC, OGG или M4A")
    return tracks, skipped, total


def _preflight_zip(source: Path):
    """Bound the central directory before ZipFile allocates all ZipInfo objects.

    ZIP64/multi-disk archives are unnecessary under our 512 MiB/500-entry limit.
    Count both the actual central entries and EOCD's advisory count.
    """
    end_record = struct.Struct("<4s4H2LH")
    size = source.stat().st_size
    with source.open("rb") as handle:
        handle.seek(max(0, size - (65535 + end_record.size)))
        tail = handle.read(65535 + end_record.size)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < end_record.size:
            raise IngestError("ZIP повреждён или не поддерживается")
        _, disk, directory_disk, on_disk, count, directory_size, directory_offset, comment_size = end_record.unpack_from(tail, offset)
        if (disk or directory_disk or on_disk != count or count > ZIP_MAX_ENTRIES
                or directory_size > ZIP_MAX_DIRECTORY_BYTES
                or offset + end_record.size + comment_size != len(tail)
                or directory_offset + directory_size > size - len(tail) + offset):
            raise IngestError("ZIP превышает безопасный размер каталога или число файлов")
        handle.seek(directory_offset)
        directory = handle.read(directory_size)
        position, actual_count = 0, 0
        while position < len(directory):
            if directory[position:position + 4] != b"PK\x01\x02" or len(directory) - position < 46:
                raise IngestError("ZIP содержит некорректный каталог")
            name_size, extra_size, entry_comment_size = struct.unpack_from("<3H", directory, position + 28)
            position += 46 + name_size + extra_size + entry_comment_size
            actual_count += 1
            if actual_count > ZIP_MAX_ENTRIES or position > len(directory):
                raise IngestError("ZIP превышает безопасное число файлов")
        if actual_count != count:
            raise IngestError("ZIP содержит несогласованный каталог")


def unpack_music_zip(source: Path, destination: Path, settings):
    """Validate every entry first, then read audio into random private paths."""
    deadline = time.monotonic() + IO_DEADLINE
    try:
        _preflight_zip(source)
        with zipfile.ZipFile(source) as archive:
            tracks, skipped, declared_total = _zip_plan(archive, settings.music_max_upload_bytes)
            _require_space(destination, declared_total, settings.music_min_free_bytes)
            extracted, actual_total = [], 0
            for info in tracks:
                clean_name = filename(clean_text(PurePosixPath(info.filename).name))
                target = destination / (secrets.token_hex(16) + Path(clean_name).suffix.lower())
                count = 0
                with archive.open(info) as incoming, _new_file(target) as outgoing:
                    while chunk := incoming.read(IO_CHUNK):
                        count += len(chunk)
                        actual_total += len(chunk)
                        if (count > info.file_size or count > settings.music_max_upload_bytes
                                or actual_total > ZIP_MAX_EXPANDED or time.monotonic() > deadline):
                            raise IngestError("ZIP превысил лимит распаковки")
                        _require_space(destination, len(chunk), settings.music_min_free_bytes)
                        outgoing.write(chunk)
                if count != info.file_size:
                    raise IngestError("Неполный аудиофайл в ZIP")
                extracted.append((target, clean_name))
            return extracted, skipped
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise IngestError("ZIP повреждён или не поддерживается") from exc


async def _blocking(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def _copy_private_audio(source: Path, destination: Path, settings):
    _require_space(destination.parent, source.stat().st_size, settings.music_min_free_bytes)
    total, deadline = 0, time.monotonic() + IO_DEADLINE
    with source.open("rb") as incoming, _new_file(destination) as outgoing:
        while chunk := incoming.read(IO_CHUNK):
            total += len(chunk)
            if total > settings.music_max_upload_bytes or time.monotonic() > deadline:
                raise IngestError("Аудиофайл превысил допустимый размер или время обработки")
            _require_space(destination.parent, len(chunk), settings.music_min_free_bytes)
            outgoing.write(chunk)


async def ingest_path(settings, session_factory, source: Path, original_name: str, *, title="", artist="") -> ImportResult:
    """Import an already-private audio/ZIP without consuming the caller's source."""
    root = private_root(settings)
    source = Path(source).absolute()
    for part in [source, *source.parents]:
        _no_link(part)
    if not source.resolve().is_relative_to(root) or not source.is_file():
        raise IngestError("Исходный файл должен находиться в приватном каталоге музыки")
    if not 0 < source.stat().st_size <= settings.music_max_upload_bytes:
        raise IngestError("Исходный файл превышает допустимый размер")
    extension = Path(original_name).suffix.lower()
    if extension not in FORMATS and extension != ".zip":
        raise IngestError("Поддерживаются MP3, WAV, FLAC, OGG, M4A и ZIP с музыкой")
    staging = _private_dir(root / ".telegram-ingest")
    with tempfile.TemporaryDirectory(prefix="incoming-", dir=staging) as working:
        directory = Path(working)
        if extension == ".zip":
            inputs, skipped = await _blocking(unpack_music_zip, source, directory, settings)
        else:
            target = directory / (secrets.token_hex(16) + extension)
            await _blocking(_copy_private_audio, source, target, settings)
            inputs, skipped = [(target, filename(original_name))], 0
        result = ImportResult(skipped=skipped)
        for path, name in inputs:
            try:
                metadata = await _blocking(inspect_audio, path, name)
            except Exception:
                result.errors.append(f"{clean_text(name, 70)}: файл не распознан как поддерживаемое аудио")
                continue
            if extension != ".zip":
                if title and metadata["title"] == display_title(name):
                    metadata["title"] = title
                metadata["artist"] = metadata["artist"] or artist
            for tag in ("title", "artist", "album"):
                metadata[tag] = clean_text(metadata[tag])
            async with content_lock(root, metadata["sha256"]), session_factory() as session:
                await lock_content(session, metadata["sha256"])
                existing = await session.scalar(select(MusicTrack).where(
                    MusicTrack.sha256 == metadata["sha256"], MusicTrack.deleted.is_(False)))
                if existing:
                    await lock_track(session, existing.id)
                    await session.refresh(existing)
                    if existing.deleted:
                        existing = None
                if existing:
                    _no_link(root / existing.storage_name)
                    destination = track_path(root, existing.storage_name)
                    restore = not destination.exists()
                    if restore:
                        path.replace(destination)
                    try:
                        await session.commit()
                    except BaseException:
                        await session.rollback()
                        if restore and destination.is_file() and not path.exists():
                            destination.replace(path)
                        raise
                    result.restored += int(restore)
                    result.existing.append(existing.id)
                    result.ordered.append(existing.id)
                    continue
                storage = secrets.token_hex(16) + Path(name).suffix.lower()
                destination = track_path(root, storage)
                if destination.exists():
                    raise IngestError("Конфликт имени файла, повторите импорт")
                track = MusicTrack(**metadata, filename=name, storage_name=storage)
                session.add(track)
                await session.flush()
                path.replace(destination)
                try:
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    if destination.is_file() and not path.exists():
                        destination.replace(path)
                    raise
                result.added.append(track.id)
                result.ordered.append(track.id)
        return result


async def vk_ingest_url(settings, session_factory, url: str, original_name: str, *,
                        title: str = "", artist: str = "") -> ImportResult:
    """Download a single remote audio URL (e.g. decoded VK CDN link) into the library."""
    root = private_root(settings)
    staging = _private_dir(root / ".vk-ingest")
    max_bytes = getattr(settings, "music_max_upload_bytes", 128 * 1024 * 1024)
    min_free = getattr(settings, "music_min_free_bytes", 512 * 1024 * 1024)
    name = filename(original_name) if Path(original_name).suffix.lower() in FORMATS else filename(original_name + ".mp3")
    dest = staging / (secrets.token_hex(16) + Path(name).suffix.lower())
    headers = {"User-Agent": "Mozilla/5.0 (compatible; XASS-Music/1.0)", "Referer": "https://vk.com/"}
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=True,
                                     headers=headers) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise IngestError(f"Источник вернул {resp.status_code}")
                total = 0
                with _new_file(dest) as out:
                    async for chunk in resp.aiter_bytes(IO_CHUNK):
                        total += len(chunk)
                        if total > max_bytes:
                            raise IngestError("Трек превышает допустимый размер")
                        _require_space(root, len(chunk), min_free)
                        out.write(chunk)
        if total < 1024:
            raise IngestError("Скачанный файл слишком мал")
        return await ingest_path(settings, session_factory, dest, name,
                                 title=title, artist=artist)
    finally:
        dest.unlink(missing_ok=True)


class TelegramMusicIngest:
    def __init__(self, settings, bot_client, session_factory, send_summary, *, debounce_seconds=1.2):
        self.settings, self.bot = settings, bot_client
        self.session_factory = session_factory
        self.send_summary = send_summary
        self.debounce_seconds = debounce_seconds
        self._pending: dict[int, list] = {}
        self._timers: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _schedule(self, chat_id: int):
        existing = self._timers.get(chat_id)
        if existing and not existing.done():
            existing.cancel()
        self._timers[chat_id] = asyncio.create_task(self._flush(chat_id))

    async def _flush(self, chat_id: int):
        try:
            await asyncio.sleep(self.debounce_seconds)
            async with self._lock:
                batch = self._pending.pop(chat_id, [])
                self._timers.pop(chat_id, None)
            if not batch:
                return
            result = ImportResult()
            for att in batch:
                try:
                    res = await self._ingest_one(att)
                    result.merge(res)
                except IngestError as exc:
                    result.errors.append(f"{att.name}: {exc}")
                except Exception:
                    logger.exception("ingest failed for %s", att.name)
                    result.errors.append(f"{att.name}: внутренняя ошибка")
            if result.added or result.existing or result.errors:
                try:
                    await self.send_summary(chat_id, result)
                except Exception:
                    logger.exception("summary send failed")
        except asyncio.CancelledError:
            return

    async def _ingest_one(self, att: Attachment) -> ImportResult:
        # download via bot, then ingest_path
        path = await self._download(att)
        return await ingest_path(self.settings, self.session_factory, path, att.name,
                                 title=att.title, artist=att.artist)

    async def _download(self, att: Attachment) -> Path:
        root = private_root(self.settings)
        staging = _private_dir(root / ".telegram-ingest")
        dest = staging / (secrets.token_hex(16) + Path(att.name).suffix.lower())
        # simplified: real impl uses bot.get_file + download_file with size limits
        raise IngestError("download stub — see original for full bot download")

    async def handle_update(self, update: dict):
        msg = update.get("message") or update.get("business_message")
        if not isinstance(msg, dict):
            return
        att = music_attachment(msg, self.settings.owner_user_id)
        if att is None:
            return
        async with self._lock:
            self._pending.setdefault(att.chat_id, []).append(att)
            if len(self._pending[att.chat_id]) > MAX_PENDING_MESSAGES:
                self._pending[att.chat_id] = self._pending[att.chat_id][-MAX_PENDING_MESSAGES:]
        self._schedule(att.chat_id)
