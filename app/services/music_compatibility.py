"""Bounded conversion of catalog-owned audio, never URLs or arbitrary paths.

Originals are immutable. A cache entry is named by the catalog digest and the
output codec, with a process-wide one-job limit and a hard input/output budget.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time

from fastapi import HTTPException

from app.services.music_library import track_path

MAX_BYTES = 256 * 1024 * 1024
MAX_CACHE_BYTES = 1024 * 1024 * 1024
MAX_SECONDS = 120
INPUT_FORMATS = "mp3,wav,flac,ogg,mov"
OUTPUTS = {"flac": ("audio/flac", ["-c:a", "flac", "-compression_level", "3"]),
           "mp3": ("audio/mpeg", ["-c:a", "libmp3lame", "-b:a", "256k", "-ac", "2", "-ar", "48000"])}
_job = threading.BoundedSemaphore(1)


def _checked_input(root, track):
    raw = root / str(track.storage_name)
    if raw.is_symlink():
        raise HTTPException(403, "Ссылка вместо аудиофайла не допускается")
    path = track_path(root, track.storage_name)
    if path.is_symlink() or not path.is_file():
        raise HTTPException(503, "Трек ещё возвращается из архива. Повторите позже")
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise HTTPException(413, "Трек превышает предел обработки 256 МБ")
    return path


def probe_codec(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise HTTPException(503, "На сервере не установлен ffprobe для проверки аудиоформата")
    try:
        process = subprocess.Popen([executable, "-v", "error", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", INPUT_FORMATS, "-select_streams", "a:0", "-show_entries",
            "stream=codec_name,duration:format=duration", "-of", "json", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        deadline = threading.Timer(12, process.kill)
        deadline.start()
        try:
            output = process.stdout.read(65537)
            if len(output) > 65536:
                process.kill()
                raise ValueError("probe output limit")
            if process.wait(timeout=2) != 0:
                raise ValueError("probe failed")
        finally:
            deadline.cancel()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            process.stdout.close()
        data = json.loads(output)
        stream = data["streams"][0]
        seconds = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
        if not math.isfinite(seconds) or not 0 < seconds <= 86400:
            raise ValueError("duration")
        return str(stream["codec_name"]), seconds
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError):
        raise HTTPException(415, "Не удалось проверить кодек аудиофайла") from None


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _cache_root(root):
    directory = root / "compatible-audio"
    if directory.is_symlink():
        raise HTTPException(503, "Кэш совместимого аудио недоступен")
    directory.mkdir(exist_ok=True)
    return directory


def _prune(directory, keep):
    for abandoned in directory.iterdir():
        if (not abandoned.is_symlink() and abandoned.is_file()
                and re.fullmatch(r"xass-compatible-[A-Za-z0-9_-]+\.(mp3|flac)", abandoned.name)
                and time.time() - abandoned.stat().st_mtime > MAX_SECONDS * 2):
            abandoned.unlink(missing_ok=True)
    entries = [p for p in directory.iterdir() if p != keep and not p.is_symlink()
               and p.is_file() and re.fullmatch(r"[a-f0-9]{64}\.(mp3|flac)", p.name)]
    total = sum(p.stat().st_size for p in entries) + (keep.stat().st_size if keep.exists() else 0)
    for path in sorted(entries, key=lambda p: p.stat().st_mtime):
        if total <= MAX_CACHE_BYTES - MAX_BYTES:
            break
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size


def prepare(root: Path, track, supported: list[str], *, forced_codec=""):
    original = _checked_input(root, track)
    digest = str(track.sha256 or "")
    if re.fullmatch(r"[a-f0-9]{64}", digest) is None or _digest(original) != digest:
        raise HTTPException(409, "Контрольная сумма исходного трека не совпадает")
    codec, _duration = probe_codec(original)
    if not forced_codec and codec in supported:
        return {"path": original, "sha256": digest, "codec": codec, "variant": "", "mime": track.mime}
    output = forced_codec or next((value for value in ("flac", "mp3") if value in supported), "")
    if output not in OUTPUTS:
        raise HTTPException(415, "Устройства не имеют совместимого аудиоформата")
    if not _job.acquire(blocking=False):
        raise HTTPException(503, "Сервер подготавливает другой трек. Повторите через несколько секунд", headers={"Retry-After": "3"})
    temporary = None
    try:
        directory = _cache_root(root)
        target = directory / f"{digest}.{output}"
        if target.is_symlink():
            raise HTTPException(503, "Кэш совместимого аудио недоступен")
        if not target.is_file():
            executable = shutil.which("ffmpeg")
            if not executable:
                raise HTTPException(503, "На сервере не установлен ffmpeg для подготовки аудио")
            _prune(directory, target)
            with tempfile.NamedTemporaryFile(dir=directory, prefix="xass-compatible-", suffix="." + output, delete=False) as file:
                temporary = Path(file.name)
            try:
                subprocess.run([executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-protocol_whitelist", "file,pipe", "-format_whitelist", INPUT_FORMATS,
                    "-i", str(original), "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
                    "-threads", "1", *OUTPUTS[output][1],
                    "-fs", str(MAX_BYTES), str(temporary)], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=MAX_SECONDS, check=True)
                if not 0 < temporary.stat().st_size < MAX_BYTES:
                    raise ValueError("size")
                actual, converted_duration = probe_codec(temporary)
                if actual != output or abs(converted_duration - _duration) > 0.5:
                    raise ValueError("incomplete")
                temporary.replace(target)
            except (subprocess.SubprocessError, OSError, ValueError):
                raise HTTPException(415, "Не удалось подготовить полный совместимый аудиофайл") from None
        return {"path": target, "sha256": _digest(target), "codec": output, "variant": output, "mime": OUTPUTS[output][0]}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        _job.release()


async def prepare_audio(root, track, supported, *, forced_codec=""):
    return await asyncio.to_thread(prepare, Path(root), track, supported, forced_codec=forced_codec)
