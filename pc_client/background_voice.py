"""Opt-in, process-scoped local listening. No recordings, transcripts, or network I/O.

The control thread owns permission to capture. The daemon worker may be inside a
non-cancellable local model call, but can never publish a superseded generation.
Only a complete, explicitly addressed utterance crosses the process boundary.
"""
from __future__ import annotations

from array import array
from collections import deque
import ctypes
import math
import os
import re
import sys
import threading
import time
from typing import Callable

try:
    from voice_capture import SAMPLE_RATE, WhisperTranscriber
except ModuleNotFoundError:
    from pc_client.voice_capture import SAMPLE_RATE, WhisperTranscriber


FRAME_MS = 100
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2
MAX_PCM_BYTES = SAMPLE_RATE * 10 * 2
MAX_TEXT = 512
MAX_INTEGER = (1 << 63) - 1
ERRORS = {
    "invalid_request": "Неверный запрос фонового прослушивания.",
    "model_unavailable": "Локальная модель речи недоступна. Проверьте папку модели и зависимости Python.",
    "microphone_unavailable": "Микрофон недоступен. Проверьте устройство и разрешения Windows.",
    "microphone_close_failed": "Не удалось подтвердить закрытие микрофона. Отключите фоновое прослушивание.",
    "worker_failed": "Фоновое прослушивание остановлено. Проверьте локальные настройки.",
}


class ListenerError(ValueError):
    """Only an internal error code, never a model/driver exception or transcript."""


def addressed_command(text: object) -> str | None:
    """No wake-word latch: the prefix and command must share one utterance."""
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    value = text.strip()
    match = re.fullmatch(r"джарвис(?:\s+|,\s*)(.+)", value, re.IGNORECASE)
    if not match or not any(char.isalnum() for char in match.group(1)):
        return None
    return value


def rms(pcm: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return math.sqrt(sum(value * value for value in samples) / len(samples)) if samples else 0.0


class SilenceSegmenter:
    """Bounded PCM, 200 ms pre-roll, 700 ms silence, no cross-utterance context.

    Overlong utterances are discarded until silence, never split into commands.
    This is deliberately a simple energy detector, not a claim of speaker or
    wake-word authentication. Whisper's local VAD still filters each segment.
    """
    def __init__(self):
        self._pre_roll: deque[bytes] = deque(maxlen=2)
        self._pcm = bytearray()
        self._speaking = False
        self._overflow = False
        self._quiet_frames = 0
        self._speech_frames = 0

    def clear(self) -> None:
        self._pre_roll.clear()
        self._pcm.clear()
        self._speaking = self._overflow = False
        self._quiet_frames = self._speech_frames = 0

    def feed(self, frame: bytes) -> bytes | None:
        if len(frame) != FRAME_BYTES:
            raise ListenerError("microphone_unavailable")
        voiced = rms(frame) >= 220.0
        if not self._speaking:
            if not voiced:
                self._pre_roll.append(frame)
                return None
            self._speaking = True
            for previous in self._pre_roll:
                self._pcm.extend(previous)
            self._pre_roll.clear()
        self._speech_frames = min(3, self._speech_frames + int(voiced))
        self._quiet_frames = 0 if voiced else self._quiet_frames + 1
        if not self._overflow:
            if len(self._pcm) + len(frame) > MAX_PCM_BYTES:
                self._pcm.clear()
                self._overflow = True
            else:
                self._pcm.extend(frame)
        if self._quiet_frames < 7:
            return None
        result = bytes(self._pcm) if not self._overflow and self._speech_frames >= 3 else None
        self.clear()
        return result


# Fixed-width fields also permit ABI/layout tests without Windows or hardware.
class WaveFormat(ctypes.Structure):
    _pack_ = 2
    _fields_ = [("tag", ctypes.c_uint16), ("channels", ctypes.c_uint16),
                ("samples", ctypes.c_uint32), ("bytes_per_second", ctypes.c_uint32),
                ("align", ctypes.c_uint16), ("bits", ctypes.c_uint16), ("extra", ctypes.c_uint16)]


class WaveHeader(ctypes.Structure):
    pass


WaveHeader._fields_ = [("data", ctypes.c_void_p), ("length", ctypes.c_uint32),
                      ("recorded", ctypes.c_uint32), ("user", ctypes.c_size_t),
                      ("flags", ctypes.c_uint32), ("loops", ctypes.c_uint32),
                      ("next", ctypes.POINTER(WaveHeader)), ("reserved", ctypes.c_size_t)]

# Retain native allocations if a broken driver refuses to relinquish them. The
# parent terminates this process on a failed pause/stop handshake.
_UNCLOSED_CAPTURES: list[object] = []


class WinMMCapture:
    """Four small reusable native buffers; reset/close may run from control stdin."""
    def __init__(self, *, _api=None):
        if _api is None and os.name != "nt":
            raise ListenerError("microphone_unavailable")
        self._api = _api if _api is not None else ctypes.WinDLL("winmm", use_last_error=True)
        self._lock = threading.Lock()
        self._handle = ctypes.c_void_p()
        self._buffers: list[tuple[ctypes.Array, WaveHeader]] = []
        self._prepared: list[WaveHeader] = []
        self._open = False
        self._closing = False
        self._next = 0
        self._segmenter = SilenceSegmenter()
        api = self._api
        api.waveInOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
                                  ctypes.POINTER(WaveFormat), ctypes.c_size_t,
                                  ctypes.c_size_t, ctypes.c_uint32]
        for name in ("waveInPrepareHeader", "waveInAddBuffer", "waveInUnprepareHeader"):
            getattr(api, name).argtypes = [ctypes.c_void_p, ctypes.POINTER(WaveHeader), ctypes.c_uint]
        for name in ("waveInStart", "waveInReset", "waveInClose"):
            getattr(api, name).argtypes = [ctypes.c_void_p]
        for name in ("waveInOpen", "waveInPrepareHeader", "waveInAddBuffer", "waveInUnprepareHeader",
                     "waveInStart", "waveInReset", "waveInClose"):
            getattr(api, name).restype = ctypes.c_uint
        fmt = WaveFormat(1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16, 0)
        if api.waveInOpen(ctypes.byref(self._handle), 0xFFFFFFFF, ctypes.byref(fmt), 0, 0, 0):
            raise ListenerError("microphone_unavailable")
        self._open = True
        try:
            for _ in range(4):
                buffer = ctypes.create_string_buffer(FRAME_BYTES)
                header = WaveHeader(ctypes.addressof(buffer), FRAME_BYTES, 0, 0, 0, 0, None, 0)
                self._buffers.append((buffer, header))
                if api.waveInPrepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header)):
                    raise ListenerError("microphone_unavailable")
                self._prepared.append(header)
                if api.waveInAddBuffer(self._handle, ctypes.byref(header), ctypes.sizeof(header)):
                    raise ListenerError("microphone_unavailable")
            if api.waveInStart(self._handle):
                raise ListenerError("microphone_unavailable")
        except BaseException:
            self.close()
            raise

    def _read_frame(self, cancelled: threading.Event) -> bytes | None:
        deadline = time.monotonic() + 2.0
        while not cancelled.is_set():
            with self._lock:
                if not self._open or self._closing:
                    return None
                buffer, header = self._buffers[self._next]
                if header.flags & 1:  # WHDR_DONE
                    if header.recorded != FRAME_BYTES:
                        raise ListenerError("microphone_unavailable")
                    frame = buffer.raw[:header.recorded]
                    ctypes.memset(header.data, 0, FRAME_BYTES)
                    header.recorded = 0
                    header.flags &= ~1
                    if self._api.waveInAddBuffer(self._handle, ctypes.byref(header), ctypes.sizeof(header)):
                        raise ListenerError("microphone_unavailable")
                    self._next = (self._next + 1) % len(self._buffers)
                    return frame
            if time.monotonic() >= deadline:
                raise ListenerError("microphone_unavailable")
            cancelled.wait(0.02)
        return None

    def read_segment(self, cancelled: threading.Event) -> bytes | None:
        try:
            while not cancelled.is_set():
                frame = self._read_frame(cancelled)
                if frame is None:
                    return None
                with self._lock:
                    if self._closing or cancelled.is_set():
                        return None
                    segment = self._segmenter.feed(frame)
                del frame
                if segment is not None:
                    return segment if not cancelled.is_set() else None
            return None
        finally:
            with self._lock:
                self._segmenter.clear()

    def close(self) -> None:
        with self._lock:
            if not self._open:
                return
            self._closing = True
            self._segmenter.clear()
            # Do not release native memory until the driver returned all buffers.
            if self._api.waveInReset(self._handle):
                if self not in _UNCLOSED_CAPTURES:
                    _UNCLOSED_CAPTURES.append(self)
                raise ListenerError("microphone_close_failed")
            failed = False
            for header in tuple(self._prepared):
                if self._api.waveInUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header)):
                    failed = True
                else:
                    self._prepared.remove(header)
            if failed or self._api.waveInClose(self._handle):
                if self not in _UNCLOSED_CAPTURES:
                    _UNCLOSED_CAPTURES.append(self)
                raise ListenerError("microphone_close_failed")
            self._open = False
            for buffer, _ in self._buffers:
                ctypes.memset(ctypes.addressof(buffer), 0, len(buffer))
            self._buffers.clear()
            if self in _UNCLOSED_CAPTURES:
                _UNCLOSED_CAPTURES.remove(self)


def _integer(value: object, *, zero: bool = False) -> bool:
    return type(value) is int and (0 if zero else 1) <= value <= MAX_INTEGER


def validate_request(request: object) -> dict:
    if not isinstance(request, dict):
        raise ListenerError("invalid_request")
    operation = request.get("operation")
    fields = {"start": {"operation", "model_path", "epoch", "paused"}, "pause": {"operation", "id"},
              "resume": {"operation", "epoch"}, "stop": {"operation"}}
    if not isinstance(operation, str) or operation not in fields or set(request) - fields[operation]:
        raise ListenerError("invalid_request")
    if operation == "start":
        path = request.get("model_path")
        if not isinstance(path, str) or not path.strip() or len(path) > 2048 or "\x00" in path:
            raise ListenerError("invalid_request")
        if type(request.get("paused", False)) is not bool:
            raise ListenerError("invalid_request")
        if not _integer(request.get("epoch", 1)):
            raise ListenerError("invalid_request")
    if operation == "pause" and not _integer(request.get("id"), zero=True):
        raise ListenerError("invalid_request")
    if operation == "resume" and not _integer(request.get("epoch")):
        raise ListenerError("invalid_request")
    return request


class BackgroundVoiceWorker:
    """Single model, no implicit resume, cancellable capture and stale inference."""
    def __init__(self, emit: Callable[[dict], None], *, transcriber_factory=WhisperTranscriber,
                 capture_factory=WinMMCapture):
        self._emit = emit
        self._transcriber_factory = transcriber_factory
        self._capture_factory = capture_factory
        self._condition = threading.Condition(threading.RLock())
        self._cancelled = threading.Event()
        self._capture = None
        self._started = False
        self._stopped = False
        self._paused = True
        self._loaded = False
        self._generation = 0
        self._epoch = 1
        self._thread: threading.Thread | None = None

    def _state(self, state: str, **extra) -> None:
        self._emit({"type": "state", "state": state, "epoch": self._epoch, **extra})

    def _invalidate(self) -> None:
        self._generation += 1
        self._cancelled.set()
        self._paused = True
        self._condition.notify_all()

    def _close_capture(self) -> None:
        if self._capture is not None:
            self._capture.close()
            self._capture = None

    def _current(self, generation: int) -> bool:
        return not self._stopped and not self._paused and generation == self._generation

    def handle(self, request: object) -> None:
        request = validate_request(request)
        operation = request["operation"]
        with self._condition:
            if self._stopped:
                if operation == "stop":
                    return
                raise ListenerError("invalid_request")
            if operation == "start":
                if self._started:
                    raise ListenerError("invalid_request")
                self._started = True
                self._paused = request.get("paused", False)
                self._epoch = request.get("epoch", 1)
                self._state("loading_model")
                self._thread = threading.Thread(target=self._run_safely, args=(request["model_path"],),
                                                name="xass-local-voice", daemon=True)
                self._thread.start()
            elif operation == "stop":
                self.stop()
            elif not self._started:
                raise ListenerError("invalid_request")
            elif operation == "pause":
                self._invalidate()
                self._close_capture()
                self._state("paused", id=request["id"])
            elif operation == "resume":
                # A changed epoch replaces even an active capture. Callers can
                # safely ignore any command emitted before their resume request.
                self._invalidate()
                self._close_capture()
                self._epoch = request["epoch"]
                self._paused = False
                if not self._loaded:
                    self._state("loading_model")
                self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            if self._stopped:
                return
            self._invalidate()
            self._close_capture()
            self._stopped = True
            self._state("stopped")

    def fail(self, code: str = "worker_failed") -> None:
        with self._condition:
            self._invalidate()
            closed = True
            try:
                self._close_capture()
            except Exception:
                code = "microphone_close_failed"
                closed = False
            if not self._stopped:
                code = code if code in ERRORS else "worker_failed"
                self._emit({"type": "error", "code": code, "message": ERRORS[code]})
                self._stopped = True
                if closed:
                    self._state("stopped")

    def _run_safely(self, model_path: str) -> None:
        try:
            self._run(model_path)
        except Exception:
            # A vanished/blocked parent or broken output stream must not produce
            # a raw daemon traceback containing model paths or recognized text.
            try:
                self.fail("worker_failed")
            except Exception:
                with self._condition:
                    self._invalidate()
                    self._stopped = True
                    try:
                        self._close_capture()
                    except Exception:
                        pass

    def _run(self, model_path: str) -> None:
        try:
            transcriber = self._transcriber_factory(model_path)
        except Exception:
            with self._condition:
                if not self._stopped:
                    self.fail("model_unavailable")
            return
        with self._condition:
            if self._stopped:
                return
            self._loaded = True
            self._state("ready")
            if self._paused:
                self._state("paused")
        while True:
            pcm = None
            text = None
            try:
                with self._condition:
                    self._condition.wait_for(lambda: self._stopped or not self._paused)
                    if self._stopped:
                        return
                    generation = self._generation
                    epoch = self._epoch
                    cancelled = self._cancelled = threading.Event()
                    # Opening is serialized with pause. Once pause returns, no
                    # opening can slip through its microphone-closed barrier.
                    capture = self._capture = self._capture_factory()
                    self._state("listening")
                pcm = capture.read_segment(cancelled)
                with self._condition:
                    if self._capture is capture:
                        self._close_capture()
                    if not self._current(generation):
                        continue
                    if not pcm:
                        raise ListenerError("microphone_unavailable")
                    self._state("transcribing")
                try:
                    text = transcriber.transcribe(pcm)
                except ValueError:
                    # Whisper's authored no-speech/rejected-text errors contain
                    # no useful command. Discard them just like ambient speech.
                    text = None
                finally:
                    pcm = None
                with self._condition:
                    if not self._current(generation):
                        continue
                    command = addressed_command(text)
                    text = None
                    if command is not None:
                        self._invalidate()
                        self._state("paused")
                        self._emit({"type": "command", "text": command, "epoch": epoch})
                        command = None
            except Exception as error:
                with self._condition:
                    if self._stopped:
                        return
                    if not self._current(generation) and not (isinstance(error, ListenerError)
                                                            and str(error) == "microphone_close_failed"):
                        continue
                    self.fail(str(error) if isinstance(error, ListenerError) else "worker_failed")
                return
            finally:
                pcm = text = None
