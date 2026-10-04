"""Bounded push-to-record PCM capture; local Whisper only, no saved audio."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time

try:
    from voice_assistant import AssistantError
except ModuleNotFoundError:
    from pc_client.voice_assistant import AssistantError


SAMPLE_RATE = 16000
RECORD_SECONDS = 6
# At most one failed capture is retained until this one-shot process exits.
_UNRELEASED_CAPTURE_REFERENCES: list[tuple] = []


def _release_capture(api, handle, header, prepared: bool) -> None:
    # Always attempt every release operation, even after a failed driver call.
    # A failed release must not return PCM or emit a transcribing/mic-off phase.
    # The one-shot parent verifies process exit before reporting the mic stopped.
    calls = [(api.waveInReset, (handle,))]
    if prepared:
        calls.append((api.waveInUnprepareHeader, (handle, ctypes.byref(header), ctypes.sizeof(header))))
    calls.append((api.waveInClose, (handle,)))
    failed = False
    for function, arguments in calls:
        try:
            if function(*arguments) != 0:
                failed = True
        except Exception:
            failed = True
    if failed:
        raise AssistantError("Не удалось подтвердить штатное закрытие устройства записи. Обработка записи отменена.")


def record_pcm(seconds: int = RECORD_SECONDS) -> bytes:
    if _UNRELEASED_CAPTURE_REFERENCES:
        raise AssistantError("Запись заблокирована после ошибки закрытия устройства. Перезапустите помощник.")
    if os.name != "nt" or type(seconds) is not int or not 1 <= seconds <= 10:
        raise AssistantError("Короткая запись доступна только на Windows (1–10 секунд).")

    class WaveFormat(ctypes.Structure):
        _pack_ = 2
        _fields_ = [("tag", wintypes.WORD), ("channels", wintypes.WORD),
                    ("samples", wintypes.DWORD), ("bytes_per_second", wintypes.DWORD),
                    ("align", wintypes.WORD), ("bits", wintypes.WORD), ("extra", wintypes.WORD)]

    class WaveHeader(ctypes.Structure):
        pass

    WaveHeader._fields_ = [("data", ctypes.c_void_p), ("length", wintypes.DWORD),
                          ("recorded", wintypes.DWORD), ("user", ctypes.c_size_t),
                          ("flags", wintypes.DWORD), ("loops", wintypes.DWORD),
                          ("next", ctypes.POINTER(WaveHeader)), ("reserved", ctypes.c_size_t)]
    api = ctypes.WinDLL("winmm", use_last_error=True)
    handle = wintypes.HANDLE()
    api.waveInOpen.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.UINT,
                              ctypes.POINTER(WaveFormat), ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    for name in ("waveInPrepareHeader", "waveInAddBuffer", "waveInUnprepareHeader"):
        getattr(api, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(WaveHeader), wintypes.UINT]
    for name in ("waveInStart", "waveInReset", "waveInClose"):
        getattr(api, name).argtypes = [wintypes.HANDLE]
    fmt = WaveFormat(1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16, 0)
    buffer = ctypes.create_string_buffer(SAMPLE_RATE * seconds * 2)
    header = WaveHeader(ctypes.addressof(buffer), len(buffer), 0, 0, 0, 0, None, 0)
    if api.waveInOpen(ctypes.byref(handle), 0xFFFFFFFF, ctypes.byref(fmt), 0, 0, 0):
        raise AssistantError("Микрофон недоступен. Проверьте устройство и разрешение микрофона для настольных приложений Windows.")
    prepared = False
    try:
        if api.waveInPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header)):
            raise AssistantError("Не удалось подготовить запись микрофона.")
        prepared = True
        if api.waveInAddBuffer(handle, ctypes.byref(header), ctypes.sizeof(header)) or api.waveInStart(handle):
            raise AssistantError("Не удалось начать запись микрофона.")
        deadline = time.monotonic() + seconds + 2
        while not header.flags & 1:  # WHDR_DONE
            if time.monotonic() > deadline:
                raise AssistantError("Микрофон не закончил запись вовремя.")
            time.sleep(0.03)
        return buffer.raw[:header.recorded]
    finally:
        try:
            _release_capture(api, handle, header, prepared)
        except AssistantError:
            # Do not free driver-referenced memory after a failed close. The
            # parent ends/verifies this process; no further capture is allowed.
            _UNRELEASED_CAPTURE_REFERENCES.append((handle, header, buffer))
            raise


class WhisperTranscriber:
    def __init__(self, model_path: str):
        path = Path(model_path)
        # local_files_only guards model download, but faster-whisper can fetch a fallback
        # tokenizer when tokenizer.json is absent. Reject incomplete explicit folders first.
        required = ("model.bin", "config.json", "tokenizer.json")
        try:
            complete = path.is_absolute() and all(
                (path / name).is_file() and (path / name).stat().st_size > 0 for name in required)
        except OSError:
            complete = False
        if not complete:
            raise AssistantError("Укажите полную существующую локальную папку faster-whisper с model.bin, "
                                 "config.json и tokenizer.json. Автозагрузка отключена.")
        # Set before importing HF-backed libraries, including external-Python fallback.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        try:
            import numpy as np
            from faster_whisper import WhisperModel
        except ImportError:
            raise AssistantError("В выбранном Python нет faster-whisper. Используйте среду из инструкции голосового MVP.") from None
        self.np = np
        self.model = WhisperModel(str(path), device="cpu", compute_type="int8", cpu_threads=4,
                                  num_workers=1, local_files_only=True)

    def transcribe(self, pcm: bytes) -> str:
        if not pcm or len(pcm) > SAMPLE_RATE * 10 * 2 or len(pcm) % 2:
            raise AssistantError("Получена пустая или некорректная запись.")
        audio = self.np.frombuffer(pcm, dtype=self.np.int16).astype(self.np.float32) / 32768.0
        if float(self.np.sqrt(self.np.mean(audio * audio))) < 0.003:
            raise AssistantError("Речь не слышна. Повторите запись ближе к микрофону.")
        segments, _ = self.model.transcribe(audio, language="ru", beam_size=1,
                                           vad_filter=True, condition_on_previous_text=False)
        text = " ".join(segment.text.strip() for segment in segments if segment.no_speech_prob < 0.6).strip()
        if not text or len(text) > 512:
            raise AssistantError("Не удалось уверенно распознать короткую команду. Повторите запись или введите текст.")
        return text
