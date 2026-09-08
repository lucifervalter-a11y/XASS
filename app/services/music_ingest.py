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
    # A cancelled coroutine must not remove its private staging directory while
    # a worker thread is still reading/writing there.
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
    """Import an already-private audio/ZIP without consuming the caller's source.

    Use a *new* session_factory, not an upload request's open transaction. The
    caller authenticates the owner and owns upload completion/receipt handling.
    Returns ordered track IDs, added/existing IDs, restored count and safe errors.
    """
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
                    # Same DB-row writer lock used by eviction/restore operations.
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


class TelegramMusicIngest:
    def __init__(self, settings, bot_client, session_factory, send_summary, *, debounce_seconds=1.2):
        self.settings, self.bot = settings, bot_client
        self.sessions, self.send_summary = session_factory, send_summary
        self.debounce_seconds = debounce_seconds
        self.pending: list[Attachment] = []
        self.pending_keys: set[str] = set()
        self.task: asyncio.Task | None = None
        self.last_enqueued = 0.0
        self.overflow = 0
        self.closed = False
        self.started = False
        self._jobs: dict[str, Attachment] = {}
        self._import_lock = asyncio.Lock()

    def _journal_directory(self):
        root = private_root(self.settings)
        receipts = _private_dir(root / ".telegram-ingest")
        return _private_dir(receipts / "pending")

    @staticmethod
    def _atomic_json(target: Path, payload: dict):
        _no_link(target)
        temporary = target.parent / (secrets.token_hex(16) + ".tmp")
        try:
            with _new_file(temporary) as handle:
                handle.write(json.dumps(payload, ensure_ascii=False).encode())
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
            if os.name != "nt":
                descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_pending(self):
        directory = self._journal_directory()
        items = []
        for path in directory.iterdir():
            if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name):
                continue
            _no_link(path)
            with path.open("rb") as handle:
                content = handle.read(8193)
            if len(content) > 8192:
                raise IngestError("Повреждена очередь импорта музыки")
            try:
                item = Attachment(**json.loads(content))
            except (ValueError, TypeError) as exc:
                raise IngestError("Повреждена очередь импорта музыки") from exc
            if (type(item.chat_id) is not int or item.chat_id != self.settings.owner_user_id
                    or type(item.message_id) is not int or item.message_id <= 0
                    or type(item.size) is not int or item.size < 0
                    or any(not isinstance(getattr(item, key), str) or len(getattr(item, key)) > limit
                           for key, limit in (("file_id", 1024), ("unique_id", 512), ("name", 240), ("title", 240), ("artist", 240)))
                    or path.stem != item.receipt_keys()[0]):
                raise IngestError("Очередь музыки содержит некорректную запись")
            receipt = directory.parent / path.name
            _no_link(receipt)
            if receipt.is_file():
                # The DB commit and receipt already happened before the crash.
                # Do not import again or send a second completion notification.
                path.unlink()
                continue
            items.append(item)
            if len(items) > MAX_PENDING_MESSAGES:
                raise IngestError("В очереди импорта слишком много файлов")
        return sorted(items, key=lambda item: item.message_id)

    async def start(self):
        if self.started or self.closed:
            return
        self.started = True
        if not self.settings.owner_user_id:
            return
        # No directory creation at every app startup when nobody imported music.
        journal = Path(self.settings.music_root).absolute() / ".telegram-ingest" / "pending"
        if not journal.exists() and not journal.is_symlink():
            return
        try:
            items = await _blocking(self._read_pending)
        except Exception as exc:
            logger.warning("Telegram music queue recovery failed (%s)", type(exc).__name__)
            await self.send_summary(self.settings.owner_user_id, "Не удалось восстановить очередь музыки. Файлы библиотеки не изменены; проверьте приватный каталог музыки.")
            return
        for item in items:
            keys = item.receipt_keys()
            self.pending.append(item)
            self.pending_keys.update(keys)
            self._jobs[keys[0]] = item
        if self.pending:
            self.last_enqueued = time.monotonic()
            self.task = asyncio.create_task(self._run())

    def enqueue(self, message: dict) -> bool:
        item = music_attachment(message, self.settings.owner_user_id)
        if item is None or self.closed:
            return False
        keys = item.receipt_keys()
        if any(key in self.pending_keys for key in keys):
            return True
        if len(self._jobs) >= MAX_PENDING_MESSAGES:
            self.overflow += 1
        else:
            directory = self._journal_directory()
            completed = directory.parent / (keys[0] + ".json")
            _no_link(completed)
            if completed.is_file():
                return True
            # Persist only the attachment, before acknowledging the webhook.
            _require_space(directory, 8192, self.settings.music_min_free_bytes)
            self._atomic_json(directory / (keys[0] + ".json"), asdict(item))
            self.pending.append(item)
            self.pending_keys.update(keys)
            self._jobs[keys[0]] = item
        self.last_enqueued = time.monotonic()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())
        return True

    async def close(self):
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _quiet(self):
        deadline = time.monotonic() + 5.0
        while (delay := min(deadline - time.monotonic(),
                            self.debounce_seconds - (time.monotonic() - self.last_enqueued))) > 0:
            await asyncio.sleep(delay)

    async def _run(self):
        try:
            while self.pending:
                await self._quiet()
                waiting = sorted(self.pending, key=lambda item: item.message_id)
                batch, self.pending = waiting[:MAX_BATCH_MESSAGES], waiting[MAX_BATCH_MESSAGES:]
                result = ImportResult()
                index = 0
                while index < len(batch):
                    item = batch[index]
                    try:
                        result.merge(await self.ingest(item))
                    except IngestError as exc:
                        result.errors.append(f"{clean_text(item.name, 70)}: {exc}")
                    except Exception as exc:
                        # httpx exceptions can contain the BOT_TOKEN in a URL.
                        logger.warning("Telegram music import failed (%s)", type(exc).__name__)
                        result.errors.append(f"{clean_text(item.name, 70)}: не удалось загрузить. Отправьте файл повторно")
                    # Cancellation deliberately bypasses this: replay unfinished
                    # owner-submitted jobs after restart. Finished receipts gate
                    # duplicate output if a crash occurs before journal removal.
                    job_key = item.receipt_keys()[0]
                    try:
                        path = self._journal_directory() / (job_key + ".json")
                        _no_link(path)
                        path.unlink(missing_ok=True)
                    except (OSError, IngestError):
                        logger.warning("Could not remove a completed Telegram music journal entry")
                    self._jobs.pop(job_key, None)
                    index += 1
                    if index == len(batch) and self.pending and len(batch) < MAX_BATCH_MESSAGES:
                        await self._quiet()
                        waiting = sorted(self.pending, key=lambda value: value.message_id)
                        remaining = MAX_BATCH_MESSAGES - len(batch)
                        batch.extend(waiting[:remaining])
                        self.pending = waiting[remaining:]
                if self.overflow:
                    result.errors.append(f"Очередь заполнена: отправьте ещё раз {self.overflow} файлов после этой пачки")
                    self.overflow = 0
                try:
                    await self.send_summary(batch[0].chat_id, self.summary(result))
                finally:
                    for item in batch:
                        self.pending_keys.difference_update(item.receipt_keys())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telegram music batch failed (%s)", type(exc).__name__)

    @staticmethod
    def summary(result: ImportResult) -> str:
        lines = ["Музыка · импорт завершён", f"Добавлено: {len(result.added)} · Уже в библиотеке: {len(result.existing)}"]
        if result.skipped:
            lines.append(f"Пропущено не-аудиофайлов: {result.skipped}")
        if result.removed:
            lines.append(f"Ранее удалены из библиотеки: {result.removed}. Для восстановления отправьте их новым сообщением")
        if result.restored:
            lines.append(f"Восстановлено копий на сервере: {result.restored}")
        if result.errors:
            lines.append(f"Не удалось импортировать: {len(result.errors)}")
            lines.extend("• " + clean_text(error, 320) for error in result.errors[:6])
            if len(result.errors) > 6:
                lines.append("Остальные файлы тоже не распознаны; проверьте формат и размер.")
        lines.extend(["", "Откройте Музыку в Mini App или приложении — библиотека общая.",
                      "Можно отправлять аудио, файлы, альбомы и ZIP. Лимит Telegram: 20 МБ на файл."])
        return "\n".join(lines)

    async def _download(self, item: Attachment, target: Path):
        limit = min(TELEGRAM_MAX_BYTES, self.settings.music_max_upload_bytes) if Path(item.name).suffix.lower() != ".zip" else TELEGRAM_MAX_BYTES
        if item.size > limit:
            raise IngestError("Файл больше лимита Telegram (20 МБ) или библиотеки. Загрузите его в Музыке через Mini App")
        async with asyncio.timeout(IO_DEADLINE):
            metadata = await self.bot.get_file(item.file_id)
            remote_path = str(metadata.get("file_path") or "")
            # Strictly relative Telegram file paths, not URLs/redirects/decoded traversal.
            if not re.fullmatch(r"[A-Za-z0-9_./-]{1,512}", remote_path) or any(part in {"", ".", ".."} for part in remote_path.split("/")):
                raise IngestError("Telegram не вернул безопасный путь к файлу")
            known_size = int(metadata.get("file_size") or item.size or 0)
            if known_size > limit:
                raise IngestError("Telegram разрешает боту скачивать до 20 МБ. Используйте Музыку в Mini App")
            _require_space(target.parent, known_size or limit, self.settings.music_min_free_bytes)
            url = self.bot.file_url.rstrip("/") + "/" + quote(remote_path, safe="/")
            timeout = httpx.Timeout(30, connect=10)
            async with self.bot.client.stream("GET", url, timeout=timeout, follow_redirects=False,
                                              headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    raise IngestError("Telegram пока не отдал файл. Попробуйте отправить его повторно")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise IngestError("Telegram вернул неподдерживаемое сжатие файла")
                declared = response.headers.get("content-length")
                if declared and (not declared.isdigit() or int(declared) > limit):
                    raise IngestError("Файл превышает допустимый размер скачивания")
                count = 0
                with _new_file(target) as outgoing:
                    async for chunk in response.aiter_bytes(IO_CHUNK):
                        count += len(chunk)
                        if count > limit:
                            raise IngestError("Файл превышает допустимый размер скачивания")
                        _require_space(target.parent, len(chunk), self.settings.music_min_free_bytes)
                        outgoing.write(chunk)
                if not count or (known_size and count != known_size) or (declared and count != int(declared)):
                    raise IngestError("Telegram передал неполный файл; отправьте его повторно")

    def _read_receipt(self, directory: Path, keys: list[str]):
        for key in keys:
            path = directory / (key + ".json")
            _no_link(path)
            if not path.exists():
                continue
            with path.open("rb") as handle:
                content = handle.read(RECEIPT_MAX_BYTES + 1)
            if len(content) > RECEIPT_MAX_BYTES:
                raise IngestError("Повреждена квитанция импорта музыки")
            payload = json.loads(content)
            ids = payload["track_ids"]
            if (payload.get("version") != 1 or not isinstance(ids, list) or len(ids) > ZIP_MAX_TRACKS
                    or any(type(value) is not int or value < 1 for value in ids)):
                raise IngestError("Повреждена квитанция импорта музыки")
            return ImportResult(existing=ids, ordered=ids, skipped=int(payload.get("skipped", 0)), errors=payload.get("errors", [])), key
        return None

    def _write_receipt(self, directory: Path, keys: list[str], result: ImportResult):
        payload = {"version": 1, "track_ids": result.ordered,
                   "skipped": result.skipped, "errors": result.errors}
        for key in keys:
            target = directory / (key + ".json")
            self._atomic_json(target, payload)

    async def ingest(self, item: Attachment) -> ImportResult:
        if item.chat_id != self.settings.owner_user_id:
            raise IngestError("Музыку может загружать только владелец")
        async with self._import_lock:
            root = private_root(self.settings)
            receipts = _private_dir(root / ".telegram-ingest")
            keys = item.receipt_keys()
            previous = await _blocking(self._read_receipt, receipts, keys)
            if previous is not None:
                result, matched_key = previous
                async with self.sessions() as session:
                    tracks = list(await session.scalars(select(MusicTrack).where(
                        MusicTrack.id.in_(result.ordered), MusicTrack.deleted.is_(False))))
                active = {track.id for track in tracks}
                cold = False
                for track in tracks:
                    _no_link(root / track.storage_name)
                    cold = cold or not track_path(root, track.storage_name).is_file()
                missing = sum(track_id not in active for track_id in result.ordered)
                # Redelivery of the same message must not undo a deliberate delete.
                # A new message is an explicit re-upload and may restore that audio.
                if (not missing and not cold) or matched_key == keys[0]:
                    result.existing = [track_id for track_id in result.existing if track_id in active]
                    result.removed = missing
                    if not missing:
                        await _blocking(self._write_receipt, receipts, keys, result)
                    return result
            extension = Path(item.name).suffix.lower()
            if extension not in FORMATS and extension != ".zip":
                raise IngestError("Поддерживаются MP3, WAV, FLAC, OGG, M4A и ZIP с музыкой")
            with tempfile.TemporaryDirectory(prefix="incoming-", dir=receipts) as working:
                directory = Path(working)
                source = directory / ("download" + extension)
                await self._download(item, source)
                result = await ingest_path(self.settings, self.sessions, source, item.name,
                                           title=item.title, artist=item.artist)
                await _blocking(self._write_receipt, receipts, keys, result)
                return result
