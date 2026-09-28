"""XASS-owned music playback. No shell, system mixer changes or arbitrary URLs.

Only the paired server's short-lived library stream URLs may be downloaded.
One worker handles downloads; newer play/stop requests invalidate older work.
The audio callback never takes the control lock (device.stop waits for it).
"""
from __future__ import annotations

import atexit
import hashlib
import ipaddress
import math
import os
import re
import tempfile
import threading
import time
from array import array
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

try:
    from network_client import create_http_client
except ModuleNotFoundError:
    from pc_client.network_client import create_http_client

MUSIC_COMMANDS = frozenset({
    "music_outputs", "music_play", "music_pause", "music_resume", "music_stop",
    "music_seek", "music_volume", "music_status",
})
SUPPORTED_FORMATS = ("mp3", "wav", "flac", "ogg")
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 120
SAMPLE_RATE = 44100


class MusicError(ValueError):
    """A safe, user-facing error which never includes a media ticket."""


def _number(value: Any, name: str, lower: float, upper: float) -> float:
    if isinstance(value, bool):
        raise MusicError(f"Некорректное значение: {name}")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise MusicError(f"Некорректное значение: {name}") from None
    if not math.isfinite(result) or not lower <= result <= upper:
        raise MusicError(f"{name}: допустимо от {lower:g} до {upper:g}")
    return result


def approved_media_url(server_url: str, value: Any, track_id: Any) -> tuple[str, int]:
    """Require the exact paired origin, exact track route and one opaque ticket."""
    identifier = str(track_id)
    if isinstance(track_id, bool) or not identifier.isascii() or not identifier.isdigit() or len(identifier) > 19 or not 0 < int(identifier) < 2**63:
        raise MusicError("Некорректный номер трека")
    raw = str(value or "")
    if len(raw) > 8192 or any(ord(char) <= 32 or ord(char) == 127 for char in raw) or "\\" in raw:
        raise MusicError("Недопустимая ссылка на трек")
    try:
        base, target = urlsplit(server_url), urlsplit(raw)
        def origin(parts):
            if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None or parts.password is not None:
                raise ValueError("origin")
            return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)
        if origin(base) != origin(target) or target.fragment:
            raise ValueError("origin")
        if target.path != f"{base.path.rstrip('/')}/agent/music/tracks/{int(track_id)}/stream":
            raise ValueError("path")
        query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True)
        if set(query) != {"ticket"} or len(query["ticket"]) != 1 or not 8 <= len(query["ticket"][0]) <= 4096:
            raise ValueError("ticket")
    except (TypeError, ValueError, OverflowError):
        raise MusicError("Ссылка должна вести на музыку привязанного сервера XASS") from None
    return raw, int(track_id)


_LAN_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,64}\Z")
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _private_ipv4(host: str) -> ipaddress.IPv4Address | None:
    try:
        value = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not isinstance(value, ipaddress.IPv4Address):
        return None
    if not any(value in network for network in _RFC1918):
        return None
    return value


def approved_lan_url(value: Any, track_id: Any) -> str:
    """One private HTTP offer for this track. Anything else is refused before a request."""
    identifier = str(track_id)
    if isinstance(track_id, bool) or not identifier.isascii() or not identifier.isdigit() or len(identifier) > 19 or not 0 < int(identifier) < 2**63:
        raise MusicError("Некорректный номер трека")
    if not isinstance(value, str) or len(value) > 300 or any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise MusicError("Локальная ссылка на трек отклонена")
    try:
        parts = urlsplit(value)
        if parts.scheme != "http" or parts.username is not None or parts.password is not None or parts.fragment or parts.port is None:
            raise ValueError("url")
        if not 1024 <= parts.port <= 65535:
            raise ValueError("port")
        address = _private_ipv4(parts.hostname or "")
        if address is None or parts.hostname != str(address):
            raise ValueError("host")
        if parts.path != f"/xass-lan/{int(identifier)}":
            raise ValueError("path")
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        token = query.get("token", [])
        if set(query) != {"token"} or len(token) != 1 or _LAN_TOKEN.fullmatch(token[0]) is None:
            raise ValueError("token")
    except (TypeError, ValueError, OverflowError):
        raise MusicError("Локальная ссылка на трек отклонена") from None
    return f"http://{address}:{parts.port}/xass-lan/{int(identifier)}?token={token[0]}"


LOCAL_SUFFIXES = {".mp3": ".mp3", ".wav": ".wav", ".flac": ".flac", ".ogg": ".ogg"}


def _local_audio_file(path: Path) -> Path:
    try:
        candidate = Path(path).expanduser()
        if candidate.is_symlink():
            raise MusicError("Нужен обычный аудиофайл на этом ПК")
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise MusicError("Файл музыки не найден") from None
    if not resolved.is_file() or resolved.is_symlink():
        raise MusicError("Нужен обычный аудиофайл на этом ПК")
    expected = LOCAL_SUFFIXES.get(resolved.suffix.lower())
    if expected is None:
        raise MusicError("На ПК играют MP3, WAV, FLAC и OGG Vorbis")
    try:
        size = resolved.stat().st_size
        if not 0 < size <= MAX_DOWNLOAD_BYTES:
            raise MusicError("Аудиофайл пустой или больше 256 МБ")
        with resolved.open("rb") as source:
            header = source.read(16)
    except OSError:
        raise MusicError("Не удалось прочитать файл музыки") from None
    if _audio_suffix(header) != expected:
        raise MusicError("Содержимое файла не совпадает с его расширением")
    return resolved


def _audio_suffix(header: bytes) -> str:
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return ".wav"
    if header.startswith(b"fLaC"):
        return ".flac"
    if header.startswith(b"OggS"):
        return ".ogg"
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 255 and header[1] & 0xE0 == 0xE0):
        return ".mp3"
    raise MusicError("Формат не поддерживается на ПК. Используйте MP3, WAV, FLAC или OGG Vorbis")


class NativeAudio:
    """Small miniaudio/WASAPI adapter, imported lazily; no output opens on import."""
    def __init__(self):
        if os.name != "nt":
            raise MusicError("Выбор аудиовыхода доступен в Windows-агенте")
        try:
            import miniaudio
        except ImportError:
            raise MusicError("Обновите агент: аудиодвижок ещё не установлен") from None
        self.ma = miniaudio

    def outputs(self) -> tuple[list[dict], dict[str, Any]]:
        ma = self.ma
        devices = ma.Devices(backends=[ma.Backend.WASAPI])
        rows = [{"id": "default", "name": "По умолчанию Windows", "is_default": True}]
        ids = {"default": None}
        for item in devices.get_playbacks():
            # The WASAPI ma_device_id is a NUL-terminated UTF-16 endpoint ID.
            # Hash only the string, not padding or device order/friendly names.
            raw = bytes(ma.ffi.buffer(item["id"]))
            endpoint = raw.decode("utf-16-le", errors="ignore").split("\0", 1)[0]
            if not endpoint:
                continue
            key = "wasapi-" + hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:32]
            rows.append({"id": key, "name": str(item["name"])[:256], "is_default": False})
            ids[key] = item["id"]
        return rows, ids

    def duration(self, path: Path) -> float:
        value = float(self.ma.get_file_info(str(path)).duration)
        if not math.isfinite(value) or not 0 < value <= 24 * 3600:
            raise MusicError("Не удалось определить длительность трека")
        return value

    def stream(self, path: Path, position: float):
        return self.ma.stream_file(str(path), sample_rate=SAMPLE_RATE, nchannels=2,
                                   output_format=self.ma.SampleFormat.SIGNED16,
                                   seek_frame=int(position * SAMPLE_RATE))

    def device(self, output_id):
        return self.ma.PlaybackDevice(sample_rate=SAMPLE_RATE, nchannels=2,
                                     output_format=self.ma.SampleFormat.SIGNED16,
                                     device_id=output_id, backends=[self.ma.Backend.WASAPI],
                                     buffersize_msec=100, app_name="XASS Music")


class MusicPlayer:
    def __init__(self, *, audio_factory=NativeAudio, client_factory=create_http_client, cache_parent=None):
        self._audio_factory = audio_factory
        self._client_factory = client_factory
        self._cache_parent = cache_parent
        self._audio = None
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._worker = None
        self._closed = False
        self._generation = 0
        self._pending = None
        self._device = None
        self._stream = None
        self._progress = {"position": 0.0, "ended": False, "error": ""}
        self._path: Path | None = None
        self._ephemeral = False
        self._tempdir = None
        self._state = "idle"
        self._track_id = None
        self._output_id = "default"
        self._duration = 0.0
        self._volume = 70.0
        self._error = ""
        self._title = ""
        self._artist = ""
        self._downloaded_bytes = 0
        self._total_bytes = 0
        self._reveal = 0
        self._reveal_listener = None

    def set_reveal_listener(self, listener) -> None:
        """GUI wake-up only. The listener must not do work while the player lock is held."""
        with self._lock:
            self._reveal_listener = listener

    def reveal_count(self) -> int:
        with self._lock:
            return self._reveal

    def _notify_reveal(self) -> None:
        with self._lock:
            listener = self._reveal_listener
        if listener is None:
            return
        try:
            listener()
        except Exception:
            return

    def _engine(self):
        if self._audio is None:
            self._audio = self._audio_factory()
        return self._audio

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._state == "playing":
                if self._progress["error"]:
                    self._error = self._progress["error"]
                    self._state = "error"
                    self._release_device()
                elif self._progress["ended"]:
                    self._state = "ended"
                    self._release_device()
                elif self._device is not None and not self._device.running:
                    self._state, self._error = "error", "Аудиовыход отключён. Выберите доступное устройство"
                    self._release_device()
            return {"state": self._state, "track_id": self._track_id, "output_id": self._output_id,
                    "position_sec": round(min(self._duration, self._progress["position"])
                        if self._duration else self._progress["position"], 2),
                    "duration_sec": round(self._duration, 2), "volume": round(self._volume),
                    "title": self._title, "artist": self._artist, "error": self._error,
                    "downloaded_bytes": self._downloaded_bytes, "total_bytes": self._total_bytes,
                    "supported_formats": list(SUPPORTED_FORMATS)}

    def _release_device(self):
        if self._device is not None:
            device, self._device = self._device, None
            device.close()
        if self._stream is not None:
            stream, self._stream = self._stream, None
            stream.close()

    def _discard_track(self):
        self._release_device()
        path, ephemeral = self._path, self._ephemeral
        self._path = None
        self._ephemeral = False
        # Server downloads are temporary. A file the user opened must stay on disk.
        if path is not None and ephemeral:
            path.unlink(missing_ok=True)

    def _callback(self, decoder, progress):
        try:
            requested = yield b""
            while True:
                try:
                    samples = decoder.send(requested)
                except StopIteration:
                    progress["ended"] = True
                    return
                gain = self._volume / 100.0
                if gain != 1.0:
                    samples = array("h", (int(sample * gain) for sample in samples))
                progress["position"] += len(samples) / (2 * SAMPLE_RATE)
                requested = yield samples
        except GeneratorExit:
            pass
        except Exception:
            # Never let a decoder exception escape a C callback, or expose paths.
            progress["error"] = "Не удалось декодировать аудиотрек"
        finally:
            decoder.close()

    def _start_at(self, position: float):
        self._release_device()
        try:
            engine = self._engine()
            _, ids = engine.outputs()  # Refresh before opening: no stale index fallback.
            if self._output_id not in ids:
                raise MusicError("Выбранный аудиовыход отключён. Выберите устройство ещё раз")
            self._progress = {"position": position, "ended": False, "error": ""}
            if position >= self._duration:
                self._state = "ended"
                return
            decoder = engine.stream(self._path, position)
            stream = self._callback(decoder, self._progress)
            next(stream)
            self._stream = stream
            self._device = engine.device(ids[self._output_id])
            self._device.start(stream)
        except Exception as exc:
            self._release_device()
            self._state = "error"
            self._error = str(exc) if isinstance(exc, MusicError) else "Не удалось открыть аудиовыход Windows"
            raise MusicError(self._error) from None
        self._state, self._error = "playing", ""

    def play_local(self, path: Path, *, title: str = "", artist: str = "") -> dict[str, Any]:
        """Play a file that already exists on this PC. No server and no copy."""
        resolved = _local_audio_file(path)
        with self._condition:
            if self._closed:
                raise MusicError("Музыкальный плеер завершает работу")
            self._generation += 1
            generation = self._generation
            self._pending = None
        try:
            duration = self._engine().duration(resolved)
        except MusicError:
            raise
        except Exception:
            raise MusicError("Не удалось прочитать трек. Поддерживаются MP3, WAV, FLAC и OGG Vorbis") from None
        with self._condition:
            if self._closed:
                raise MusicError("Музыкальный плеер завершает работу")
            if generation != self._generation:
                return self.snapshot()
            self._discard_track()
            self._track_id = None
            self._title = (title.strip() or resolved.stem)[:256]
            self._artist = artist.strip()[:256]
            self._downloaded_bytes = self._total_bytes = resolved.stat().st_size
            self._path, self._duration, self._ephemeral = resolved, duration, False
            self._start_at(0)
            return self.snapshot()

    def command(self, command: str, payload: dict, config: dict) -> dict[str, Any]:
        reveal = False
        try:
            if "expires_at" in payload:
                try:
                    expires = float(payload["expires_at"])
                    if not math.isfinite(expires) or expires <= time.time():
                        raise ValueError("expired")
                except (ValueError, TypeError):
                    raise MusicError("Команда устарела. Повторите действие") from None
            with self._condition:
                if self._closed:
                    raise MusicError("Музыкальный плеер завершает работу")
                self.snapshot()
                if command == "music_outputs":
                    rows, _ = self._engine().outputs()
                    return {**self.snapshot(), "outputs": rows, "default_output_id": "default"}
                if command == "music_status":
                    return self.snapshot()
                if command == "music_play":
                    server_url = str(config.get("server_url") or "")
                    media_path = payload.get("media_path")
                    # The server may have a public HTTPS origin while an older
                    # paired agent uses its private/IP endpoint. Relative library
                    # routes resolve ONLY against that agent's own configured base.
                    value = server_url.rstrip("/") + str(media_path) if media_path is not None else payload.get("url")
                    url, track_id = approved_media_url(server_url, value, payload.get("track_id"))
                    raw_lan = payload.get("lan_url")
                    lan_url = approved_lan_url(raw_lan, track_id) if raw_lan else None
                    position = _number(payload.get("position_sec", 0), "Позиция", 0, 24 * 3600)
                    volume = _number(payload.get("volume", self._volume), "Громкость", 0, 100)
                    output_id = str(payload.get("output_id") or "default")
                    _, ids = self._engine().outputs()
                    if output_id not in ids:
                        raise MusicError("Аудиовыход не найден. Обновите список устройств")
                    self._discard_track()
                    self._generation += 1
                    self._track_id, self._output_id, self._volume = track_id, output_id, volume
                    self._title, self._artist = str(payload.get("title") or "")[:256], str(payload.get("artist") or "")[:256]
                    self._state, self._error, self._duration = "loading", "", 0.0
                    self._downloaded_bytes, self._total_bytes = 0, 0
                    self._progress = {"position": position, "ended": False, "error": ""}
                    self._pending = (self._generation, url, dict(config), position, lan_url)
                    self._reveal += 1
                    reveal = True
                    if self._worker is None:
                        self._worker = threading.Thread(target=self._download_loop, name="xass-music", daemon=True)
                        self._worker.start()
                    self._condition.notify()
                elif command == "music_stop":
                    self._generation += 1
                    self._pending = None
                    self._discard_track()
                    self._state, self._error = "stopped", ""
                    self._progress = {"position": 0.0, "ended": False, "error": ""}
                elif command == "music_volume":
                    self._volume = _number(payload.get("volume"), "Громкость", 0, 100)
                elif command in {"music_pause", "music_resume", "music_seek"}:
                    if command == "music_pause" and (self._path is None or self._state in {"idle", "loading", "stopped", "error"}):
                        # A handoff only needs proof of silence. An idle player
                        # must not raise: that leaves the phone session stuck.
                        # Cancelling a download also stops the worker from
                        # starting audio after the pause was acknowledged.
                        if self._state == "loading":
                            self._generation += 1
                            self._pending = None
                            self._discard_track()
                        self._state, self._error = "stopped", ""
                        return self.snapshot()
                    if self._path is None or self._state in {"idle", "loading", "stopped", "error"}:
                        raise MusicError("Сначала запустите трек и дождитесь загрузки")
                    if command == "music_pause":
                        if self._state == "playing":
                            self._release_device()
                            self._state = "paused"
                    elif command == "music_resume":
                        if self._state != "playing":
                            self._start_at(0 if self._state == "ended" else self._progress["position"])
                    else:
                        position = _number(payload.get("position_sec"), "Позиция", 0, self._duration)
                        if self._state == "paused":
                            self._progress = {"position": position, "ended": False, "error": ""}
                        else:
                            self._start_at(position)
                else:
                    raise MusicError("Неизвестная музыкальная команда")
                return self.snapshot()
        finally:
            if reveal:
                self._notify_reveal()

    def _cancelled(self, generation):
        with self._lock:
            return self._closed or generation != self._generation

    def _download(self, generation, url, config, lan_url=None) -> Path | None:
        if lan_url:
            if self._cancelled(generation):
                return None
            try:
                fetched = self._fetch(generation, lan_url, config, direct=True)
            except Exception:
                fetched = None
                if self._cancelled(generation):
                    return None
            else:
                return fetched
            if self._cancelled(generation):
                return None
        return self._fetch(generation, url, config, direct=False)

    def _fetch(self, generation, url, config, *, direct: bool) -> Path | None:
        if self._tempdir is None:
            self._tempdir = tempfile.TemporaryDirectory(prefix="xass-music-", dir=self._cache_parent)
        target = Path(self._tempdir.name) / f"track-{generation}.part"
        started, total = time.monotonic(), 0
        with self._lock:
            if self._cancelled(generation):
                return None
            self._downloaded_bytes = 0
            self._total_bytes = 0
        headers = {"Accept-Encoding": "identity"}
        if not direct:
            headers["X-Api-Key"] = str(config.get("api_key") or "")
        timeout = httpx.Timeout(10, connect=2 if direct else 5)
        trust_env = False if direct else bool(config.get("trust_env_proxy", False))
        try:
            with self._client_factory(url if direct else str(config["server_url"]), timeout=timeout,
                                      trust_env=trust_env, follow_redirects=False) as client:
                with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
                    if response.status_code in {401, 403, 404, 410}:
                        raise MusicError("Ссылка на музыку истекла или доступ отозван. Запустите трек снова")
                    if response.status_code != 200:
                        raise MusicError("Сервер не смог передать аудиотрек")
                    if response.headers.get("content-encoding", "identity").lower() not in {"identity", ""}:
                        raise MusicError("Сервер вернул неподдерживаемое сжатие аудиотрека")
                    length = response.headers.get("content-length", "")
                    if length and (not length.isdigit() or int(length) > MAX_DOWNLOAD_BYTES):
                        raise MusicError("Аудиофайл больше допустимых 256 МБ")
                    with self._lock:
                        if self._cancelled(generation):
                            return None
                        self._total_bytes = int(length or 0)
                    with target.open("wb") as output:
                        # Do not buffer a fixed large chunk: even a drip-fed
                        # response must re-check the total deadline/cancellation.
                        for chunk in response.iter_bytes():
                            if self._cancelled(generation):
                                return None
                            total += len(chunk)
                            if total > MAX_DOWNLOAD_BYTES:
                                raise MusicError("Аудиофайл больше допустимых 256 МБ")
                            if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                                raise MusicError("Загрузка музыки заняла слишком много времени")
                            output.write(chunk)
                            with self._lock:
                                if not self._cancelled(generation):
                                    self._downloaded_bytes = total
                    if not total or (length and int(length) != total):
                        raise MusicError("Аудиофайл загружен не полностью")
            with target.open("rb") as source:
                suffix = _audio_suffix(source.read(16))
            final = target.with_suffix(suffix)
            target.replace(final)
            return final
        finally:
            target.unlink(missing_ok=True)

    def _download_loop(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending is not None or self._closed)
                if self._closed:
                    break
                pending = self._pending
                self._pending = None
                if not pending:
                    continue
                generation, url, config, position = pending[:4]
                lan_url = pending[4] if len(pending) > 4 else None
            path = None
            try:
                path = self._download(generation, url, config, lan_url)
                if path is None:
                    continue
                # MP3 duration inspection can scan a large file. Never hold the
                # command/heartbeat lock during that disk/decoder work.
                try:
                    duration = self._engine().duration(path)
                except Exception:
                    raise MusicError("Не удалось прочитать трек. Поддерживаются MP3, WAV, FLAC и OGG Vorbis") from None
                with self._lock:
                    if self._cancelled(generation):
                        continue
                    if position > duration:
                        raise MusicError("Начальная позиция больше длительности трека")
                    self._path, self._duration, self._ephemeral = path, duration, True
                    path = None
                    self._start_at(position)
            except Exception as exc:
                with self._lock:
                    if not self._cancelled(generation):
                        self._error = str(exc) if isinstance(exc, MusicError) else "Не удалось загрузить музыку. Проверьте подключение и повторите запуск"
                        self._state = "error"
                        self._discard_track()
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)
        if self._tempdir is not None:
            self._tempdir.cleanup()

    def close(self):
        with self._condition:
            self._closed = True
            self._pending = None
            self._generation += 1
            self._discard_track()
            self._condition.notify_all()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=0.1)


_player: MusicPlayer | None = None


def music_player() -> MusicPlayer:
    global _player
    if _player is None:
        _player = MusicPlayer()
        atexit.register(_player.close)
    return _player


def music_snapshot() -> dict[str, Any]:
    # No enumeration, cache directory or download thread until the first command.
    return music_player().snapshot()


def handle_music_command(command: str, payload: dict, config: dict) -> dict[str, Any]:
    return music_player().command(command, payload, config)
