"""Long-lived native music owner. A private inherited pipe, never an HTTP API.

Only native-selected ordinary local audio paths are accepted. Agent URLs/tokens
stay in existing agent modules. stdin EOF closes audio, without stopping agent.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

VERSION = 1
MAX_REQUEST = 192 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
MAX_PATHS = 40
STATES = {"idle", "loading", "stopping", "playing", "paused", "stopped", "ended", "error", "offline"}
CAST_STATES = {"loading", "stopping", "playing", "paused", "error"}
FIELDS = {"snapshot": set(), "open": {"paths"}, "local_play": {"index"}, "previous": set(),
          "next": set(), "pause": set(), "resume": set(), "stop": set(), "reset": set(),
          "seek": {"position"}, "volume": {"volume"}, "catalog": {"offset", "query"},
          "play": {"track_id", "volume"}, "shutdown": set()}


def clean_text(value, limit=240):
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32)[:limit] if isinstance(value, str) else ""


def number(value, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= high:
        raise ValueError("Некорректное число")
    return float(value)


def read_json(path, limit=65536):
    from native_music_ownership import safe_path
    safe_path(path)
    with path.open("r", encoding="utf-8-sig") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Слишком большой файл")
    return json.loads(raw)


def lyrics_rows(text):
    """Bounded LRC; plain lyrics and multiple timestamps remain readable."""
    rows = []
    offset = 0.0
    found = re.search(r"\[offset:([+-]?\d{1,7})\]", text, re.I)
    if found:
        offset = max(-3600, min(3600, int(found[1]) / 1000))
    for line in text.splitlines()[:2000]:
        times = re.findall(r"\[(\d{1,3}):(\d{2}(?:\.\d{1,3})?)\]", line)
        label = re.sub(r"\[[^\]]*\]", "", line).strip()
        if times:
            for minute, second in times[:20]:
                if float(second) < 60:
                    rows.append({"time": max(0, int(minute) * 60 + float(second) + offset), "text": label[:1000]})
        elif label:
            rows.append({"time": None, "text": label[:1000]})
        if len(rows) >= 2000:
            break
    timed = any(row["time"] is not None for row in rows)
    return sorted((row for row in rows if row["time"] is not None), key=lambda row: row["time"])[:2000] if timed else rows[:2000]


def safe_error(value):
    # Decoder/network text can contain paths, signed URLs or credentials. Never
    # return it verbatim, even when a malicious status file supplies the value.
    text = str(value or "").lower()
    if not text:
        return ""
    if "трансляци" in text or "другой плеер" in text:
        return "Сначала остановите трансляцию или другой плеер, затем повторите."
    if any(word in text for word in ("файл", "формат", "трек", "декод")):
        return "Не удалось открыть аудиофайл. Проверьте формат, наличие и доступ к файлу."
    if any(word in text for word in ("выход", "устройств", "звук")):
        return "Аудиовыход недоступен. Проверьте устройство Windows и повторите."
    return "Воспроизведение не удалось. Сбросьте плеер и повторите запуск."


class MusicService:
    def __init__(self, source, data, *, player=None, adapter=None):
        from native_music_ownership import AudioOwnership, FileLock, safe_path
        from music_player import MusicPlayer
        self.source, self.data = Path(source), safe_path(Path(data))
        self.data.mkdir(parents=True, exist_ok=True)
        self.ownership = AudioOwnership(self.data, local=True)
        self.host_lock = FileLock(self.ownership.folder / "host.lock")
        if not self.host_lock.acquire():
            raise ValueError("Локальный плеер уже запущен")
        self.player = player or MusicPlayer()
        self.player.set_ownership(self.ownership)
        self.adapter = adapter
        self.queue = []
        self.current = -1
        self.error = ""
        self.loading = False
        self.generation = 0
        self.guard = threading.RLock()
        self.open_condition = threading.Condition(self.guard)
        self.pending_open = None
        self.stop_event = threading.Event()
        self.closed = False
        self.identity_checked = 0.0
        self.identity = {"active": False, "compatible": True, "reason": ""}
        self.agent_override = None
        self.cover_digest = ""
        self.cover_bytes = b""
        try:
            rows = read_json(self.data / "local-music.json", 192 * 1024)
            if isinstance(rows, list):
                self.queue = list(dict.fromkeys(row for row in rows if isinstance(row, str) and len(row) <= 4096 and Path(row).is_absolute()))[:MAX_PATHS]
        except (OSError, ValueError, RuntimeError):
            pass
        self.opener = threading.Thread(target=self._open_loop, name="xass-native-local-open", daemon=True)
        self.opener.start()
        self.watcher = threading.Thread(target=self._watch, name="xass-native-music-owner", daemon=True)
        self.watcher.start()

    def _identity(self, *, force=False):
        if force or time.monotonic() - self.identity_checked >= 1:
            from native_agent_identity import probe_agent
            self.identity = probe_agent(self.data, self.source)
            self.identity_checked = time.monotonic()
        return self.identity

    def _legacy_blocked(self, *, force=False):
        identity = self._identity(force=force)
        return identity["active"] and not identity["compatible"]

    def _agent(self):
        path = self.data / "music-playback.json"
        try:
            if not -5 <= time.time() - path.stat().st_mtime <= 3:
                return {}
            result = read_json(path)
            if not isinstance(result, dict):
                return {}
            if self.agent_override and time.monotonic() < self.agent_override[0]:
                result = {**result, **self.agent_override[1]}
            return result
        except (OSError, ValueError, RuntimeError):
            return {}

    def _watch(self):
        while not self.stop_event.wait(0.04):
            try:
                agent = self._agent()
                cast = agent.get("state") in CAST_STATES and type(agent.get("track_id")) is int
                if self.ownership.cast_pending() or cast or self._legacy_blocked():
                    with self.guard:
                        active = self.loading or self.player.snapshot().get("state") in {"playing", "paused", "loading", "ended", "error"}
                        if active:
                            self.generation += 1
                            self.loading = False
                            self.pending_open = None
                            self.player.command("music_stop", {}, {})
            except Exception:
                # A corrupt ownership path is fail-closed for local audio.
                with self.guard:
                    self.generation += 1
                    self.loading = False
                    self.pending_open = None
                    self.player.command("music_stop", {}, {})

    def _save(self):
        from native_music_ownership import safe_path
        path = safe_path(self.data / "local-music.json")
        descriptor, temporary = tempfile.mkstemp(prefix=".local-music-", suffix=".tmp", dir=self.data)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.queue, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _validate_file(self, value):
        from native_music_ownership import safe_path
        from music_player import _local_audio_file
        if not isinstance(value, str) or not 1 <= len(value) <= 4096 or not Path(value).is_absolute() or "\0" in value:
            raise ValueError("Нужен локальный абсолютный путь")
        return _local_audio_file(safe_path(Path(value)))

    def _start(self, index):
        if self._legacy_blocked(force=True):
            raise ValueError(self.identity["reason"])
        if self.ownership.cast_pending() or self._cast_live():
            raise ValueError("Сначала остановите трансляцию с телефона")
        path = self._validate_file(self.queue[index])
        with self.guard:
            self.generation += 1
            generation = self.generation
            self.player.command("music_stop", {}, {})
            self.current, self.loading, self.error = index, True, ""
            self.pending_open = (generation, path)
            self.open_condition.notify()

    def _open_loop(self):
        # One decoder worker and one replaceable pending track, never one
        # thread per rapid Next/Previous click.
        while True:
            with self.open_condition:
                self.open_condition.wait_for(lambda: self.pending_open is not None or self.stop_event.is_set())
                if self.stop_event.is_set():
                    return
                generation, path = self.pending_open
                self.pending_open = None
            try:
                self.player.play_local(path, cancelled=lambda: generation != self.generation or self.stop_event.is_set())
                with self.guard:
                    if generation == self.generation and (self.stop_event.is_set() or self.ownership.cast_pending() or self._cast_live() or self._legacy_blocked(force=True)):
                        self.player.command("music_stop", {}, {})
            except Exception as exc:
                with self.guard:
                    if generation == self.generation:
                        self.error = safe_error(exc)
            finally:
                with self.guard:
                    if generation == self.generation:
                        self.loading = False

    def _cast_live(self):
        agent = self._agent()
        return type(agent.get("track_id")) is int and agent.get("state") in CAST_STATES

    def _adapter(self):
        if self.adapter is None:
            from bridge import AgentAdapter
            self.adapter = AgentAdapter(self.source, self.data)
        return self.adapter

    def _presentation(self, snap, cast):
        from music_player import _clean_lyrics, _jpeg_artwork
        if cast:
            from music_bridge import read_cover
            lyrics = _clean_lyrics(clean_text(snap.get("lyrics"), 8000))
            expected = snap.get("cover_sha256")
            raw = read_cover(self.data, expected_sha256=expected) if isinstance(expected, str) and re.fullmatch("[0-9a-f]{64}", expected) else b""
        else:
            notes = self.player.presentation()
            lyrics = _clean_lyrics(clean_text(notes.get("lyrics"), 8000))
            raw = notes.get("artwork", b"")
        raw = raw if isinstance(raw, bytes) and len(raw) <= 512 * 1024 else b""
        digest = hashlib.sha256(raw).hexdigest() if raw else ""
        if digest != self.cover_digest:
            self.cover_digest, self.cover_bytes = digest, _jpeg_artwork(raw) if raw else b""
        return {"lyrics": lyrics, "lyric_rows": lyrics_rows(lyrics),
                "cover": base64.b64encode(self.cover_bytes).decode("ascii"),
                "cover_sha256": hashlib.sha256(self.cover_bytes).hexdigest() if self.cover_bytes else ""}

    def snapshot(self):
        with self.guard:
            agent = self._agent()
            cast = type(agent.get("track_id")) is int and agent.get("state") in CAST_STATES | {"ended"}
            local = self.player.snapshot()
            # Ended cast remains replayable unless a local track was selected.
            if cast and agent.get("state") == "ended" and (self.loading or local.get("state") not in {"idle", "stopped"}):
                cast = False
            snap = agent if cast else local
            def bounded(key, maximum):
                try:
                    return number(snap.get(key) or 0, maximum)
                except ValueError:
                    return 0.0
            state = snap.get("state") if snap.get("state") in STATES else "offline"
            if not cast and self.loading:
                state = "loading"
            if not cast and self.error:
                state = "error"
            result = {"owner": "agent" if cast else "local", "state": state,
                      "track_id": snap.get("track_id") if type(snap.get("track_id")) is int else None,
                      "title": clean_text(snap.get("title")) or "Ничего не играет", "artist": clean_text(snap.get("artist")),
                      "position": bounded("position_sec", 86400), "duration": bounded("duration_sec", 86400),
                      "volume": bounded("volume", 100), "error": safe_error(snap.get("error")) if cast else self.error or safe_error(snap.get("error")),
                      "reveal": agent.get("reveal") if type(agent.get("reveal")) is int and 0 <= agent["reveal"] <= 2**31-1 else 0,
                      "local_available": not self._cast_live() and not self.ownership.cast_pending() and not self._legacy_blocked(),
                      "compatibility_notice": self.identity.get("reason", ""),
                      "current_index": self.current, "queue": [{"index": i, "path": p, "title": Path(p).stem,
                          "exists": Path(p).is_file()} for i, p in enumerate(self.queue)]}
            result.update(self._presentation(snap, cast))
            return result

    def request(self, request):
        if not isinstance(request, dict) or type(request.get("version")) is not int or request.get("version") != VERSION or type(request.get("id")) is not int or not 0 <= request["id"] <= 2**31-1:
            raise ValueError("Неверная версия запроса")
        action = request.get("action")
        if not isinstance(action, str) or action not in FIELDS or set(request) != FIELDS[action] | {"version", "id", "action"}:
            raise ValueError("Неверные поля запроса")
        if action == "snapshot":
            return self.snapshot()
        if action == "shutdown":
            self.close()
            return {"closed": True}
        if action == "catalog":
            if type(request["offset"]) is not int or not 0 <= request["offset"] <= 100000 or not isinstance(request["query"], str) or len(request["query"]) > 120:
                raise ValueError("Неверный поиск")
            return self._adapter().request("catalog", request)
        if action == "play":
            if type(request["track_id"]) is not int or not 0 < request["track_id"] <= 2**31-1:
                raise ValueError("Неверный трек")
            number(request["volume"], 100)
            self._cancel_local()
            self._adapter().request("play", request)
            return {"accepted": True, "queued": True}
        if action == "open":
            paths = request["paths"]
            if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_PATHS:
                raise ValueError("Выберите от 1 до 40 аудиофайлов")
            # Validate every selected file before modifying remembered paths.
            validated = list(dict.fromkeys(str(self._validate_file(path)) for path in paths))
            self.queue = (validated + [p for p in self.queue if p not in validated])[:MAX_PATHS]
            self._save()
            self._start(0)
        elif action == "local_play":
            index = request["index"]
            if type(index) is not int or not 0 <= index < len(self.queue):
                raise ValueError("Выберите сохранённый трек")
            self._start(index)
        elif action in {"previous", "next"}:
            direction = -1 if action == "previous" else 1
            initial = self.current if self.current >= 0 else (-1 if direction > 0 else 0)
            for step in range(1, len(self.queue) + 1):
                index = (initial + step * direction) % len(self.queue)
                try:
                    self._validate_file(self.queue[index])
                except (OSError, ValueError):
                    continue
                self._start(index)
                break
            else:
                raise ValueError("Сохранённые файлы не найдены. Выберите файлы заново")
        else:
            action = "stop" if action == "reset" else action
            payload = {"position_sec": number(request["position"], 86400)} if action == "seek" else {"volume": number(request["volume"], 100)} if action == "volume" else {}
            cast = self.snapshot()["owner"] == "agent"
            if cast:
                from music_bridge import send_command
                response = send_command("music_" + action, payload, root=self.data)
                if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("player"), dict):
                    raise ValueError("Плеер не подтвердил команду")
                self.agent_override = (time.monotonic() + 0.6, response["player"])
            elif action == "stop":
                self._cancel_local()
            else:
                self.player.command("music_" + action, payload, {})
        return self.snapshot()

    def _cancel_local(self):
        with self.guard:
            self.generation += 1
            self.loading = False
            self.error = ""
            self.pending_open = None
            self.open_condition.notify_all()
            self.player.command("music_stop", {}, {})

    def close(self):
        with self.guard:
            if self.closed:
                return
            self.closed = True
            self.stop_event.set()
            self._cancel_local()
        if self.watcher is not threading.current_thread():
            self.watcher.join(timeout=1)
        self.player.close()
        self.opener.join(timeout=0.2)
        self.ownership.release()
        self.host_lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args(argv)
    source = args.source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Missing bundled source")
    os.environ["XASS_DATA_ROOT"] = str(args.data.absolute())
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    service = None
    output = sys.stdout
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            service = MusicService(source, args.data)
        while True:
            raw = sys.stdin.readline(MAX_REQUEST + 1)
            if not raw:
                break
            identifier = 0
            try:
                if len(raw) > MAX_REQUEST or not raw.endswith("\n"):
                    raise ValueError("Request too large")
                request = json.loads(raw)
                identifier = request.get("id", 0) if isinstance(request, dict) else 0
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    result = service.request(request)
                response = {"version": VERSION, "id": identifier, "ok": True, "result": result}
            except Exception as exc:
                response = {"version": VERSION, "id": identifier if type(identifier) is int else 0,
                            "ok": False, "error": safe_error(exc)}
            encoded = json.dumps(response, ensure_ascii=True, allow_nan=False)
            if len(encoded) > MAX_RESPONSE:
                encoded = json.dumps({"version": VERSION, "id": identifier, "ok": False, "error": "Ответ плеера слишком большой"})
            output.write(encoded + "\n")
            output.flush()
            if service.stop_event.is_set():
                break
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    main()
