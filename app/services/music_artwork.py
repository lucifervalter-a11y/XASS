"""Bounded extraction of real embedded artwork into a private JPEG cache.

No network, external artwork URL, generated placeholder or public cache path.
The authenticated route must still check track ownership/deletion on every call.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
import io
import itertools
import os
from pathlib import Path
import re
import secrets
import threading
import time
import warnings

import mutagen
from mutagen.flac import Picture
from PIL import Image, ImageOps

from app.services.music_library import track_path

MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_EMBEDDED_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_OUTPUT_BYTES = 512 * 1024
MAX_CANDIDATES = 16
THUMBNAIL_SIZE = 512
CACHE_VERSION = "v1"
_decode_slots = threading.BoundedSemaphore(2)


class _MetadataBudgetExceeded(ValueError):
    pass


class _BoundedReader:
    """Mutagen may seek over audio frames; only metadata reads spend the budget."""
    def __init__(self, stream):
        self.stream, self.name = stream, stream.name
        self.remaining = MAX_METADATA_BYTES
        self.operations = 0
        self.deadline = time.monotonic() + 3.0

    def _check(self):
        self.operations += 1
        if self.operations > 20_000 or time.monotonic() > self.deadline:
            raise _MetadataBudgetExceeded("Audio metadata work budget exceeded")

    def read(self, size=-1):
        self._check()
        if size < 0:
            size = os.fstat(self.stream.fileno()).st_size - self.stream.tell()
        if size > self.remaining:
            raise _MetadataBudgetExceeded("Audio metadata byte budget exceeded")
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def seek(self, offset, whence=0):
        self._check()
        return self.stream.seek(offset, whence)

    def tell(self):
        return self.stream.tell()


def _picture_data(value) -> bytes | None:
    data = getattr(value, "data", value)
    if isinstance(data, (bytes, bytearray)) and 0 < len(data) <= MAX_EMBEDDED_BYTES:
        return bytes(data)
    return None


def _embedded_candidates(audio) -> Iterator[bytes]:
    """Front cover first, with a finite number of actual embedded candidates."""
    pictures = []
    for picture in itertools.islice(getattr(audio, "pictures", ()) or (), MAX_CANDIDATES):
        pictures.append((getattr(picture, "type", 0) != 3, picture))
    tags = getattr(audio, "tags", None)
    if tags:
        if hasattr(tags, "getall"):
            pictures.extend((getattr(frame, "type", 0) != 3, frame)
                            for frame in itertools.islice(tags.getall("APIC"), MAX_CANDIDATES - len(pictures)))
        for cover in itertools.islice(tags.get("covr", ()) or (), MAX_CANDIDATES - len(pictures)):
            pictures.append((False, cover))
        for key in ("metadata_block_picture", "coverart"):
            for value in itertools.islice(tags.get(key, ()) or (), MAX_CANDIDATES - len(pictures)):
                if not isinstance(value, str) or len(value) > 4 * ((MAX_EMBEDDED_BYTES + 2) // 3):
                    continue
                try:
                    decoded = base64.b64decode(value, validate=True)
                    if len(decoded) > MAX_EMBEDDED_BYTES:
                        continue
                    picture = Picture(decoded) if key == "metadata_block_picture" else decoded
                except (ValueError, TypeError, binascii.Error, mutagen.MutagenError):
                    continue
                pictures.append((getattr(picture, "type", 0) != 3, picture))
    yielded = 0
    for _, picture in sorted(pictures, key=lambda pair: pair[0]):
        # FLAC/APIC MIME '-->' means an external URL; never follow or emit it.
        if getattr(picture, "mime", "") == "-->":
            continue
        data = _picture_data(picture)
        if data is not None:
            yield data
            yielded += 1
            if yielded >= MAX_CANDIDATES:
                return


def _jpeg_thumbnail(data: bytes) -> bytes | None:
    if not 0 < len(data) <= MAX_EMBEDDED_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                width, height = source.size
                if (source.format not in {"JPEG", "PNG", "WEBP", "GIF"}
                        or width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS):
                    return None
                # JPEG draft decoding avoids allocating its original full bitmap.
                source.draft("RGB", (THUMBNAIL_SIZE, THUMBNAIL_SIZE))
                source.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
                oriented = ImageOps.exif_transpose(source)
                if oriented.mode in {"RGBA", "LA"} or (oriented.mode == "P" and "transparency" in oriented.info):
                    rgba = oriented.convert("RGBA")
                    output = Image.new("RGB", rgba.size, "#202022")
                    output.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    output = oriented.convert("RGB")
                encoded = io.BytesIO()
                # Deliberately omit EXIF/ICC/comments from the sanitized thumbnail.
                output.save(encoded, format="JPEG", quality=85, optimize=True)
                result = encoded.getvalue()
                return result if len(result) <= MAX_OUTPUT_BYTES else None
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def _no_link(path: Path):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError("Unsafe artwork path")


def _write_cache(path: Path, data: bytes):
    _no_link(path)
    temporary = path.parent / (secrets.token_hex(16) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _cached_artwork(path: Path) -> bool:
    _no_link(path)
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_OUTPUT_BYTES:
        return False
    with path.open("rb") as stream:
        return stream.read(3) == b"\xff\xd8\xff"


def artwork_thumbnail(root: Path, track) -> Path | None:
    """Blocking helper: call in a worker thread, return JPEG path or no artwork.

    Track bytes are immutable and keyed by their ingestion-verified SHA256.
    Cold tracks retain a previously cached cover; deleted or unsafe tracks do not.
    """
    try:
        digest = str(getattr(track, "sha256", ""))
        if getattr(track, "deleted", False) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            return None
        root = Path(root).absolute()
        for part in [root, *root.parents]:
            _no_link(part)
        _no_link(root / track.storage_name)
        source = track_path(root, track.storage_name)
        present = source.is_file()
        if source.exists() and not present:
            return None
        if present and (not 0 < source.stat().st_size <= MAX_SOURCE_BYTES or source.stat().st_size != track.size):
            return None
        cache = root / ".artwork"
        _no_link(cache)
        image = cache / f"{digest}-{CACHE_VERSION}.jpg"
        absent = cache / f"{digest}-{CACHE_VERSION}.none"
        _no_link(absent)
        if _cached_artwork(image):
            return image
        # Offloaded audio is not "no embedded artwork"; do not negative-cache it.
        if not present:
            return None
        cache.mkdir(mode=0o700, exist_ok=True)
        if os.name != "nt":
            cache.chmod(0o700)
        if absent.is_file():
            return None
        with _decode_slots:
            # Concurrent requests for the same cover reuse the first result.
            if _cached_artwork(image):
                return image
            if absent.is_file():
                return None
            with source.open("rb") as stream:
                audio = mutagen.File(_BoundedReader(stream), easy=False)
            if audio is not None:
                for candidate in _embedded_candidates(audio):
                    thumbnail = _jpeg_thumbnail(candidate)
                    if thumbnail:
                        _write_cache(image, thumbnail)
                        return image
            _write_cache(absent, b"")
            return None
    except (OSError, ValueError, TypeError, AttributeError, mutagen.MutagenError):
        return None
