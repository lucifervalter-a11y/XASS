"""Music page. Local files play here. A track sent from the phone is shown and controlled through the agent."""
from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageTk

from client_update import DATA_ROOT
from desktop_widgets import CARD, MUTED, TEXT, ModernButton, RoundedPanel, rounded_image
from music_player import MusicError
from network_client import create_http_client, require_secure_transport
from runtime_state import atomic_write_json

PLAYLIST = "local-music.json"
BLUE, LILAC = "#829cff", "#c495f4"
AUDIO_TYPES = [
    ("Аудио", "*.mp3 *.wav *.flac *.ogg"),
    ("MP3", "*.mp3"),
    ("WAV", "*.wav"),
    ("FLAC", "*.flac"),
    ("OGG", "*.ogg"),
]
STATE_LABELS = {
    "idle": "Готов играть с этого компьютера",
    "loading": "Открытие",
    "playing": "Играет здесь",
    "paused": "Пауза",
    "stopped": "Остановлено",
    "ended": "Трек закончился",
}
CAST_LABELS = {
    "loading": "Загрузка с телефона",
    "stopping": "Остановка",
    "playing": "Играет на этом компьютере",
    "paused": "Пауза",
    "error": "Не удалось включить",
}


def _cast_status() -> dict:
    try:
        from music_bridge import read_playback
        payload = read_playback()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _cast_live(status: dict) -> bool:
    state = str(status.get("state") or "")
    track = status.get("track_id")
    return state in {"loading", "stopping", "playing", "paused", "error"} and isinstance(track, int) and not isinstance(track, bool) and track > 0


def _cast_display(status: dict) -> dict:
    shown = dict(status)
    if not str(shown.get("title") or "").strip():
        shown["title"] = "Трек XASS"
    if not str(shown.get("artist") or "").strip():
        shown["artist"] = "Библиотека XASS"
    return shown


def _send_cast(command: str, payload: dict | None = None) -> dict:
    from music_bridge import send_command
    return send_command(command, payload or {})


class CastCommandPump:
    """Serialize remote controls away from Tk and collapse volume drags."""

    def __init__(self, sender=None) -> None:
        self._sender = sender or _send_cast
        self._condition = threading.Condition()
        self._queue = deque()
        self._results = deque(maxlen=64)
        self._sequence = 0
        self._active = False
        self._closed = False
        self._thread = None
        self._last_volume_sent = 0.0

    def submit(self, command: str, payload: dict | None = None) -> int | None:
        request = (command, dict(payload or {}))
        with self._condition:
            if self._closed:
                return None
            self._sequence += 1
            sequence = self._sequence
            item = (sequence, *request)
            # A pointer drag can produce hundreds of callbacks per second. Only
            # the most recent consecutive volume request has observable value.
            if command == "music_volume" and self._queue and self._queue[-1][1] == command:
                self._queue[-1] = item
            else:
                self._queue.append(item)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="xass-cast-controls", daemon=True)
                self._thread.start()
            self._condition.notify()
            return sequence

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closed)
                if self._closed and not self._queue:
                    return
                if self._queue[0][1] == "music_volume":
                    # Keep loopback traffic below ~14 requests/second even if
                    # Windows emits pointer-motion callbacks much faster.
                    remaining = 0.075 - (time.monotonic() - self._last_volume_sent)
                    if remaining > 0:
                        self._condition.wait(remaining)
                        continue
                sequence, command, payload = self._queue.popleft()
                self._active = True
            response, error = {}, ""
            try:
                value = self._sender(command, payload)
                response = value if isinstance(value, dict) else {}
            except MusicError as exc:
                error = str(exc)
            except Exception:
                error = "Не удалось связаться с плеером компьютера"
            with self._condition:
                self._active = False
                if command == "music_volume":
                    self._last_volume_sent = time.monotonic()
                if not self._closed:
                    self._results.append((sequence, command, payload, response, error))
                self._condition.notify_all()

    def drain(self) -> list[tuple[int, str, dict, dict, str]]:
        with self._condition:
            rows = list(self._results)
            self._results.clear()
            return rows

    def busy(self) -> bool:
        with self._condition:
            return self._active or bool(self._queue)

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """Test/teardown helper; the Tk path never waits for the worker."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._active or self._queue:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._condition.wait(remaining):
                    return False
            return True

    def close(self, *, wait: bool = False, timeout: float = 0.5) -> None:
        with self._condition:
            self._closed = True
            self._queue.clear()
            thread = self._thread
            self._condition.notify_all()
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, timeout))


def _optimistic_cast(status: dict, command: str, payload: dict | None = None) -> dict:
    """Return immediate UI state while the loopback command is in flight."""
    shown = dict(status)
    values = payload or {}
    if command == "music_volume":
        try:
            shown["volume"] = min(100.0, max(0.0, float(values.get("volume"))))
        except (TypeError, ValueError):
            pass
    elif command == "music_seek":
        try:
            shown["position_sec"] = max(0.0, float(values.get("position_sec")))
        except (TypeError, ValueError):
            pass
    elif command == "music_pause" and shown.get("state") == "playing":
        shown["state"] = "paused"
    elif command == "music_resume" and shown.get("state") in {"paused", "ended"}:
        shown["state"] = "playing"
        shown["error"] = ""
    elif command == "music_stop":
        # Keep the remote session authoritative until stop is acknowledged, so
        # local playback cannot start on top of an in-flight remote stop.
        shown["state"] = "stopping"
        shown["error"] = ""
    return shown


def playlist_file() -> Path:
    return DATA_ROOT / PLAYLIST


def load_playlist() -> list[str]:
    try:
        with playlist_file().open("r", encoding="utf-8") as source:
            payload = json.loads(source.read(128 * 1024))
    except (OSError, ValueError, TypeError, UnicodeError):
        return []
    if not isinstance(payload, list):
        return []
    return list(dict.fromkeys(item for item in payload if isinstance(item, str) and item and len(item) <= 4096))[:40]


def remember_track(path: Path) -> list[str]:
    chosen = str(path.resolve())
    items = load_playlist()
    # Playing an existing row must not reorder the queue (A -> B -> A forever).
    if chosen in items:
        return items
    items = [chosen, *items][:40]
    atomic_write_json(playlist_file(), items)
    return items


def next_path(paths: list[str], current: str, direction: int) -> Path | None:
    if not paths:
        return None
    try:
        index = paths.index(current)
    except ValueError:
        index = -1 if direction > 0 else 0
    return Path(paths[(index + direction) % len(paths)])


class LocalOpenJob:
    """File inspection and WASAPI startup run off Tk; results are polled by Tk."""

    def __init__(self, player, path: Path) -> None:
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.error = ""
        self.snapshot = None
        self.thread = threading.Thread(target=self._run, args=(player, path), name="xass-local-music", daemon=True)
        self.thread.start()

    def _run(self, player, path):
        try:
            if self.cancelled.is_set():
                self.snapshot = {"state": "stopped"}
                return
            self.snapshot = player.play_local(path)
            if self.cancelled.is_set():
                # A phone/agent cast may become live while the file decoder or
                # WASAPI device is opening. Stop from this worker, never Tk.
                self.snapshot = player.command("music_stop", {}, {})
                return
            if self.snapshot.get("state") == "playing":
                try:
                    remember_track(path)
                except OSError:
                    self.error = "Трек играет, но список файлов не удалось сохранить."
        except MusicError as exc:
            self.error = str(exc)
        except Exception:
            self.error = "Не удалось открыть трек. Попробуйте другой файл."
        finally:
            self.done.set()

    def cancel(self) -> None:
        self.cancelled.set()


class LocalSilenceJob:
    """Make a live agent cast authoritative without ever waiting in Tk."""

    def __init__(self, player, opening: LocalOpenJob | None = None) -> None:
        self.done = threading.Event()
        if opening is not None:
            opening.cancel()
        self.thread = threading.Thread(
            target=self._run, args=(player,), name="xass-local-music-stop", daemon=True,
        )
        self.thread.start()

    def _run(self, player) -> None:
        try:
            player.command("music_stop", {}, {})
        except Exception:
            pass
        finally:
            self.done.set()


class ServerCatalogJob:
    """Fetch a small safe catalog page without ever blocking Tk."""

    def __init__(self, config: dict, *, offset: int = 0, query: str = "") -> None:
        self.done = threading.Event()
        self.tracks: list[dict] = []
        self.total = 0
        self.offset = max(0, min(100000, int(offset)))
        self.query = str(query).strip()[:120]
        self.next_offset: int | None = None
        self.error = ""
        self.thread = threading.Thread(
            target=self._run, args=(dict(config),), name="xass-music-catalog", daemon=True,
        )
        self.thread.start()

    def _run(self, config: dict) -> None:
        base = str(config.get("server_url") or "").rstrip("/")
        key = str(config.get("api_key") or "")
        if not base or not key.startswith("ag_"):
            self.error = "Перепривяжите ПК, чтобы увидеть библиотеку сервера."
            self.done.set()
            return
        try:
            base = require_secure_transport(base, allow_insecure_http=config.get("allow_insecure_http") is True)
            with create_http_client(
                base, timeout=10, trust_env=bool(config.get("trust_env_proxy", False)),
                follow_redirects=False,
            ) as client:
                response = client.get(
                    base + "/agent/music/library",
                    params={"limit": 100, "offset": self.offset, "q": self.query},
                    headers={"X-Api-Key": key, "Accept": "application/json"},
                )
            if response.status_code in {401, 403}:
                self.error = "Доступ к библиотеке отозван. Перепривяжите ПК."
                return
            if response.status_code == 404:
                self.error = "Обновите сервер XASS, чтобы видеть его музыку здесь."
                return
            response.raise_for_status()
            if len(response.content) > 512 * 1024:
                raise ValueError("catalog too large")
            body = response.json()
            rows = body.get("tracks") if isinstance(body, dict) else None
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError("invalid catalog")
            parsed: list[dict] = []
            for row in rows:
                if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
                    continue
                parsed.append({
                    "id": row["id"],
                    "title": str(row.get("title") or "Без названия")[:240],
                    "artist": str(row.get("artist") or "Неизвестный исполнитель")[:240],
                    "album": str(row.get("album") or "")[:240],
                    "duration": max(0.0, min(86400.0, float(row.get("duration") or 0))),
                    "favorite": bool(row.get("favorite")),
                    "has_artwork": bool(row.get("has_artwork")),
                })
            self.tracks = parsed
            self.total = max(len(parsed), min(100000, int(body.get("total") or len(parsed))))
            if "offset" in body and (type(body["offset"]) is not int or body["offset"] != self.offset):
                raise ValueError("invalid catalog offset")
            more = body.get("has_more", self.offset + len(rows) < self.total)
            if more:
                next_offset = body.get("next_offset", self.offset + len(rows))
                if not rows or type(next_offset) is not int or not self.offset < next_offset <= 100000:
                    raise ValueError("catalog did not advance")
                self.next_offset = next_offset
        except Exception:
            self.tracks = []
            self.next_offset = None
            self.error = "Не удалось обновить библиотеку. XASS повторит после восстановления сети."
        finally:
            self.done.set()


class ServerPlayJob:
    """Queue a selected server track on this paired agent off the UI thread."""

    def __init__(self, config: dict, track_id: int, *, volume: float) -> None:
        self.done = threading.Event()
        self.error = ""
        self.thread = threading.Thread(
            target=self._run, args=(dict(config), int(track_id), float(volume)),
            name="xass-music-server-play", daemon=True,
        )
        self.thread.start()

    def _run(self, config: dict, track_id: int, volume: float) -> None:
        base = str(config.get("server_url") or "").rstrip("/")
        key = str(config.get("api_key") or "")
        try:
            if not base or not key.startswith("ag_"):
                raise ValueError("unpaired")
            base = require_secure_transport(base, allow_insecure_http=config.get("allow_insecure_http") is True)
            with create_http_client(
                base, timeout=10, trust_env=bool(config.get("trust_env_proxy", False)),
                follow_redirects=False,
            ) as client:
                response = client.post(
                    f"{base}/agent/music/library/{track_id}/play",
                    headers={"X-Api-Key": key, "Accept": "application/json"},
                    json={"output_id": "default", "volume": round(min(100, max(0, volume)))},
                )
            if response.status_code in {401, 403}:
                self.error = "Доступ отозван. Перепривяжите ПК."
            elif response.status_code == 415:
                self.error = "Этот формат не играет на Windows. Выберите MP3, WAV, FLAC или OGG."
            else:
                response.raise_for_status()
        except Exception:
            if not self.error:
                self.error = "Не удалось запустить трек. Проверьте сеть и повторите."
        finally:
            self.done.set()


def _local_snapshot(player, opening=None, previous: dict | None = None) -> dict:
    """Never wait for the player lock while its background open job is active."""
    if opening is not None and not opening.done.is_set():
        shown = dict(previous or {})
        shown.update(state="loading", error="", state_label="Открываю трек…")
        shown.setdefault("volume", 70)
        return shown
    value = player.snapshot()
    return value if isinstance(value, dict) else {}


def _clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _fit(text: str, font, limit: int) -> str:
    if limit < 24 or font.measure(text) <= limit:
        return text
    trimmed = text
    while trimmed and font.measure(trimmed + "…") > limit:
        trimmed = trimmed[:-1]
    return (trimmed.rstrip() + "…") if trimmed else ""


_COVER = 184
_STAMP = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
_LYRIC_META = re.compile(r"^\[(?:ar|al|ti|au|by|re|ve|length|offset):.*\]$", re.I)
_COVER_FONT = None
_PHONE_COVER_SHA = ""
_PHONE_COVER_DATA = b""


def _art_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16] if data else ""


def _cover_font() -> ImageFont.ImageFont:
    global _COVER_FONT
    if _COVER_FONT is None:
        try:
            _COVER_FONT = ImageFont.truetype("segoeui.ttf", 72)
        except OSError:
            _COVER_FONT = ImageFont.load_default()
    return _COVER_FONT


def _square_cover(image: Image.Image) -> Image.Image:
    fitted = ImageOps.fit(image.convert("RGB"), (_COVER, _COVER), Image.Resampling.LANCZOS)
    mask = Image.new("L", (_COVER, _COVER), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, _COVER - 1, _COVER - 1), radius=18, fill=255)
    output = Image.new("RGBA", (_COVER, _COVER), (0, 0, 0, 0))
    output.paste(fitted, (0, 0), mask)
    return output


def _render_cover(artwork: bytes, letter: str) -> Image.Image:
    if artwork:
        try:
            import io
            with Image.open(io.BytesIO(artwork), formats=("JPEG", "PNG", "GIF")) as source:
                if source.format in {"JPEG", "PNG", "GIF"} and 0 < source.size[0] * source.size[1] <= 20_000_000:
                    return _square_cover(source.copy())
        except (OSError, ValueError):
            pass
    image = Image.new("RGBA", (_COVER, _COVER), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, _COVER - 1, _COVER - 1), radius=18, fill=(18, 20, 32, 255))
    draw.ellipse((8, 6, 150, 148), fill=(130, 156, 255, 90))
    draw.ellipse((48, 40, 196, 188), fill=(196, 149, 244, 80))
    try:
        draw.text((_COVER / 2, _COVER / 2), (letter or "•")[:1], fill=(245, 245, 247, 255), font=_cover_font(), anchor="mm")
    except (OSError, ValueError):
        draw.text((70, 70), (letter or "•")[:1], fill=(245, 245, 247, 255))
    return image


def _lyric_rows(lyrics: str) -> tuple[list[str], bool, list[float]]:
    parsed: list[tuple[float | None, str]] = []
    stamps = 0
    for raw in lyrics.splitlines():
        if _LYRIC_META.fullmatch(raw.strip()):
            continue
        times: list[float] = []
        cursor = 0
        while match := _STAMP.match(raw, cursor):
            fraction = match.group(3) or "0"
            scale = 10 ** len(fraction)
            times.append(int(match.group(1)) * 60 + int(match.group(2)) + int(fraction) / scale)
            cursor = match.end()
            stamps += 1
        text = raw[cursor:].strip()
        if text:
            parsed.append((times[0] if times else None, text))
    if not parsed and lyrics.strip():
        return [lyrics.strip()], False, []
    if stamps == 0:
        return [text for _stamp, text in parsed], False, []
    rows, marks = [], []
    for stamp, text in parsed:
        rows.append(text)
        marks.append(float(stamp if stamp is not None else (marks[-1] if marks else 0)))
    return rows, True, marks


def _phone_cover(expected_sha256: str) -> bytes:
    """Return only the cover explicitly named by the atomic playback status."""
    global _PHONE_COVER_SHA, _PHONE_COVER_DATA
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        _PHONE_COVER_SHA, _PHONE_COVER_DATA = "", b""
        return b""
    if expected == _PHONE_COVER_SHA and _PHONE_COVER_DATA:
        return _PHONE_COVER_DATA
    try:
        from music_bridge import read_cover
        data = read_cover(expected_sha256=expected)
    except Exception:
        return b""
    if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != expected:
        # Do not retain a previous track's picture while the next atomic pair is
        # still being published (or when the file was externally replaced).
        _PHONE_COVER_SHA, _PHONE_COVER_DATA = "", b""
        return b""
    _PHONE_COVER_SHA, _PHONE_COVER_DATA = expected, data
    return data


def _player_notes(player) -> dict:
    method = getattr(player, "presentation", None)
    if not callable(method):
        return {}
    try:
        notes = method()
    except Exception:
        return {}
    return notes if isinstance(notes, dict) else {}


def _plate(width: int, height: int) -> Image.Image:
    base = Image.new("RGBA", (width, height), (14, 14, 18, 255))
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    draw.ellipse((int(width * 0.42), -140, width + 180, int(height * 1.05)), fill=(130, 156, 255, 90))
    draw.ellipse((int(width * 0.62), -20, width + 40, height + 120), fill=(196, 149, 244, 70))
    base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(42)))
    canvas = Image.new("RGB", (width, height), "#202022")
    mask = rounded_image(width, height, fill="#ffffff", radius=18).getchannel("A")
    canvas.paste(base.convert("RGB"), (0, 0), mask)
    edge = rounded_image(width, height, fill="#000000", radius=18, border_color="#ffffff", border_width=1)
    edge_mask = ImageChops.multiply(edge.convert("L"), edge.getchannel("A"))
    gradient = Image.new("RGB", (width, 1))
    gradient.putdata([
        tuple(round(a + (b - a) * x / max(1, width - 1)) for a, b in zip((130, 156, 255), (196, 149, 244)))
        for x in range(width)
    ])
    canvas.paste(gradient.resize((width, height), Image.Resampling.NEAREST), (0, 0), edge_mask)
    return canvas


def _reveal_focused_widget(app, widget) -> None:
    canvas = getattr(app, "body_canvas", None)
    if canvas is None:
        return
    bounds = canvas.bbox("all")
    if not bounds:
        return
    top = canvas.canvasy(0)
    start = widget.winfo_rooty() - canvas.winfo_rooty() + top
    end = start + widget.winfo_height()
    target = start if start < top else end - canvas.winfo_height() if end > top + canvas.winfo_height() else None
    if target is not None:
        canvas.yview_moveto(max(0, target) / max(1, bounds[3]))


def _music_row_accessibility(row, activate, app) -> None:
    """One keyboard stop per row, with identical pointer/keyboard activation."""
    row.configure(takefocus=1, highlightthickness=1,
                  highlightbackground=row.cget("bg"), highlightcolor=BLUE)
    armed = False

    def focus(_event):
        _reveal_focused_widget(app, row)

    def press(_event):
        nonlocal armed
        armed = True
        return "break"

    def release(_event):
        nonlocal armed
        if armed:
            armed = False
            activate()
        return "break"

    def blur(_event):
        nonlocal armed
        armed = False

    def click(_event):
        row.focus_set()
        activate()
        return "break"

    def bind_pointer(widget):
        widget.bind("<ButtonRelease-1>", click)
        for child in widget.winfo_children():
            bind_pointer(child)

    bind_pointer(row)
    row.bind("<FocusIn>", focus, add="+")
    row.bind("<FocusOut>", blur, add="+")
    for key in ("Return", "KP_Enter", "space"):
        row.bind(f"<KeyPress-{key}>", press)
        row.bind(f"<KeyRelease-{key}>", release)


class MusicVolume(tk.Canvas):
    """A focusable volume control; server snapshots never emit commands."""
    def __init__(self, parent, command):
        super().__init__(parent, width=120, height=26, bg="#202022", borderwidth=0,
                         takefocus=1, highlightthickness=2,
                         highlightbackground="#202022", highlightcolor=BLUE)
        self.command = command
        self.value = 70.0
        self.create_line(6, 13, 114, 13, fill="#3a3a42", width=8, capstyle="round")
        self.fill = self.create_line(6, 13, 70, 13, fill=LILAC, width=8, capstyle="round")
        self.bind("<Button-1>", self._pointer)
        self.bind("<B1-Motion>", self._pointer)
        for key in ("Left", "Right", "Up", "Down", "Home", "End"):
            self.bind(f"<KeyPress-{key}>", self._key)
        self.set(self.value)

    def set(self, value):
        self.value = min(100.0, max(0.0, float(value)))
        self.coords(self.fill, 6, 13, max(7, 6 + int(108 * self.value / 100)), 13)
        self.itemconfigure(self.fill, state="normal" if self.value > 0.4 else "hidden")

    def _pointer(self, event):
        self.focus_set()
        self.set((event.x - 6) / 108 * 100)
        self.command(self.value)
        return "break"

    def _key(self, event):
        value = 0 if event.keysym == "Home" else 100 if event.keysym == "End" else self.value + (5 if event.keysym in {"Right", "Up"} else -5)
        self.set(value)
        self.command(self.value)
        return "break"


class MusicStage(tk.Canvas):
    """Player surface. Artwork is drawn; the title and controls stay real widgets."""

    def __init__(self, parent, player, actions: dict) -> None:
        super().__init__(parent, bg="#202022", height=300, highlightthickness=2, borderwidth=0,
                         takefocus=1, highlightbackground="#202022", highlightcolor=BLUE)
        self.player = player
        self.actions = actions
        self._plate = None
        self._plate_size = None
        self._covers: dict[tuple[str, str], Image.Image] = {}
        self._cover_photos = {}
        self._photo = None
        self._animation = None
        self._resize_job = None
        self._playing = False
        self._last_snapshot = None
        self._artwork = b""
        self._art_key = ""
        self._drag = None
        self._duration = 0.0
        self._position = 0.0
        self._title_text = "Тишина"
        self._artist_text = "Локальный файл"
        self._state_text = STATE_LABELS["idle"]
        self._state_fill = LILAC
        self._title_font = tkfont.Font(self, family="Segoe UI Semibold", size=26)
        self._artist_font = tkfont.Font(self, family="Segoe UI", size=13)
        self._state_font = tkfont.Font(self, family="Segoe UI", size=11)
        self._time_font = tkfont.Font(self, family="Segoe UI", size=11)
        self._art = self.create_image(0, 0, anchor="nw")
        self._cover_art = self.create_image(0, 0, anchor="nw")
        self._title = self.create_text(252, 84, anchor="w", fill=TEXT, font=self._title_font)
        self._artist = self.create_text(252, 126, anchor="nw", fill="#c6c6ce", font=self._artist_font)
        self._state = self.create_text(252, 158, anchor="nw", fill=LILAC, font=self._state_font)
        self._elapsed = self.create_text(252, 214, anchor="nw", fill="#ececf1", font=self._time_font)
        self._total = self.create_text(0, 214, anchor="ne", fill="#ececf1", font=self._time_font)
        self._track = self.create_line(0, 0, 0, 0, fill="#3a3a42", width=8, capstyle="round")
        self._fill = self.create_line(0, 0, 0, 0, fill=BLUE, width=8, capstyle="round")
        self.play_button = None
        self.bind("<Configure>", self._resized, add="+")
        self.bind("<Button-1>", self._seek_press, add="+")
        self.bind("<B1-Motion>", self._seek_drag, add="+")
        self.bind("<ButtonRelease-1>", self._seek_release, add="+")
        for key in ("Left", "Right", "Home", "End"):
            self.bind(f"<KeyPress-{key}>", self._seek_key)
        self.bind("<KeyPress-space>", lambda _event: "break")
        self.bind("<KeyRelease-space>", self._toggle_key)
        self.bind("<Destroy>", self._destroyed, add="+")

    def _destroyed(self, event) -> None:
        if event.widget is self:
            for name in ("_animation", "_resize_job"):
                callback = getattr(self, name)
                if callback:
                    self.after_cancel(callback)
                    setattr(self, name, None)
            self._plate = None
            self._photo = None
            self._cover_photos.clear()
            self._covers.clear()

    def _resized(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        if event is not None and (event.width, event.height) == self._plate_size:
            return
        if self._resize_job is None:
            self._resize_job = self.after(35, self._layout)

    def _layout(self) -> None:
        self._resize_job = None
        width = max(1, self.winfo_width())
        target = self._target_height(width)
        if int(self["height"]) != target:
            self.configure(height=target)
            return
        self._paint()

    def _target_height(self, width: int) -> int:
        wide = width < 8 or width >= 720
        title_height = self._title_font.metrics("linespace")
        # Keep the compact title below the 184px cover, even when point fonts
        # grow at 150–200% Windows scaling. The text stack must also leave room
        # for complete status/time lines and the scrubber rather than overlap.
        title_top = max(24 if wide else 228, (86 if wide else 248) - title_height // 2)
        stack = (title_height + 8 + self._artist_font.metrics("linespace") + 10
                 + self._state_font.metrics("linespace") + 14
                 + self._time_font.metrics("linespace") + 8 + 8 + 12)
        return max(300 if wide else 460, title_top + stack + 2)

    def _cover_image(self) -> Image.Image:
        letter = (self._title_text[:1] or "•").upper()
        key = (self._art_key, letter)
        cached = self._covers.get(key)
        if cached is None:
            if len(self._covers) > 8:
                self._covers.clear()
                self._cover_photos.clear()
            cached = _render_cover(self._artwork, letter)
            self._covers[key] = cached
        return cached

    def _paint_cover(self) -> None:
        key = (self._art_key, (self._title_text[:1] or "•").upper())
        if key not in self._cover_photos:
            self._cover_photos[key] = ImageTk.PhotoImage(self._cover_image(), master=self)
        self.itemconfigure(self._cover_art, image=self._cover_photos[key])

    def _paint(self, force_plate: bool = False) -> None:
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        if width < 8 or height < 8:
            return
        if force_plate or self._plate_size != (width, height):
            self._plate = _plate(width, height)
            self._plate_size = (width, height)
            self._photo = ImageTk.PhotoImage(self._plate, master=self)
            self.itemconfigure(self._art, image=self._photo)
        wide = width >= 720
        self.coords(self._cover_art, *(36, max(24, (height - 184) // 2)) if wide else (24, 28))
        self._paint_cover()
        text_x = 252 if wide else 28
        title_y = 86 if wide else max(248, 228 + self._title_font.metrics("linespace") // 2)
        wrap = max(160, width - text_x - 40)
        self.coords(self._title, text_x, title_y)
        self.itemconfigure(self._title, text=_fit(self._title_text, self._title_font, wrap), width=0)
        self.itemconfigure(self._artist, text=_fit(self._artist_text, self._artist_font, wrap), width=0)
        self.itemconfigure(self._state, text=_fit(self._state_text, self._state_font, wrap), width=0,
                           fill=self._state_fill, font=self._state_font)
        # Tk's point fonts change height with Windows DPI. A fixed middle anchor
        # can overlap the artist's descenders; position the complete status line
        # beneath its actual glyph box using the same font we measure for fitting.
        title_box = self.bbox(self._title)
        artist_top = max(title_y + 42 - self._artist_font.metrics("linespace") // 2,
                         (title_box[3] if title_box else title_y) + 8)
        self.coords(self._artist, text_x, artist_top)
        artist_box = self.bbox(self._artist)
        state_y = max(title_y + 64, (artist_box[3] if artist_box else title_y + 54) + 10)
        self.coords(self._state, text_x, state_y)
        self.tag_raise(self._state)
        bar_y = self._bar_span()[2]
        time_y = bar_y - self._time_font.metrics("linespace") - 8
        self.coords(self._elapsed, text_x, time_y)
        self.coords(self._total, width - 36, time_y)
        self._draw_bar()

    def _bar_span(self) -> tuple[int, int, int]:
        width = max(1, self.winfo_width())
        x0 = 252 if width >= 720 else 32
        height = self.winfo_height()
        state_box = self.bbox(self._state)
        minimum = (state_box[3] if state_box else 0) + self._time_font.metrics("linespace") + 22
        y = min(height - 20, max(height - 58, minimum))
        return x0, max(x0 + 40, width - 36), y

    def _draw_bar(self) -> None:
        x0, x1, y = self._bar_span()
        ratio = 0.0 if self._duration <= 0 else min(1.0, max(0.0, (self._drag if self._drag is not None else self._position) / self._duration))
        mid = y + 4
        filled = x0 + max(0, int((x1 - x0) * ratio))
        self.coords(self._track, x0, mid, x1, mid)
        self.coords(self._fill, x0, mid, max(x0 + 1, filled), mid)
        self.itemconfigure(self._fill, state="normal" if ratio > 0.004 else "hidden")
        self.itemconfigure(self._elapsed, text=_clock(self._drag if self._drag is not None else self._position))
        self.itemconfigure(self._total, text=_clock(self._duration))

    def _seek_ratio(self, event) -> float | None:
        x0, x1, y = self._bar_span()
        if not (y - 12 <= event.y <= y + 18 and x0 <= event.x <= x1):
            return None
        return (event.x - x0) / max(1, x1 - x0)

    def _seek_press(self, event) -> None:
        ratio = self._seek_ratio(event)
        if ratio is None or self._duration <= 0:
            return
        self.focus_set()
        self._drag = ratio * self._duration
        self._draw_bar()

    def _seek_drag(self, event) -> None:
        if self._drag is None:
            return
        x0, x1, _y = self._bar_span()
        ratio = min(1.0, max(0.0, (event.x - x0) / max(1, x1 - x0)))
        self._drag = ratio * self._duration
        self._draw_bar()

    def _seek_release(self, _event) -> None:
        if self._drag is None:
            return
        target = self._drag
        self._drag = None
        self.actions["seek"](target)

    def _seek_key(self, event):
        if self._duration > 0:
            target = 0 if event.keysym == "Home" else self._duration if event.keysym == "End" else self._position + (5 if event.keysym == "Right" else -5)
            self._position = min(self._duration, max(0, target))
            self._draw_bar()
            self.actions["seek"](self._position)
        return "break"

    def _toggle_key(self, _event):
        if callable(self.actions.get("toggle")):
            self.actions["toggle"]()
        return "break"

    def show(self, snapshot: dict) -> None:
        if not self.winfo_exists():
            return
        state = str(snapshot.get("state") or "idle")
        error = str(snapshot.get("error") or "").strip()
        playing = state == "playing"
        phone = snapshot.get("source") == "phone"
        if self.play_button is not None:
            if state == "loading" or (phone and state == "stopping"):
                label = ("Загрузка…" if phone else "Открытие…") if state == "loading" else "Остановка…"
                self.play_button.configure(text=label, icon="play" if state == "loading" else "stop", state="disabled")
            elif phone and state == "error":
                self.play_button.configure(text="Сбросить", icon="stop", state="normal")
            else:
                self.play_button.configure(
                    text="Пауза" if playing else "Играть",
                    icon="pause" if playing else "play",
                    state="normal",
                )
        self._playing = playing
        if not playing and self._animation is not None:
            self.after_cancel(self._animation)
            self._animation = None
        artwork = snapshot.get("artwork") if isinstance(snapshot.get("artwork"), (bytes, bytearray)) else b""
        artwork = bytes(artwork) if artwork.startswith(b"\xff\xd8\xff") and len(artwork) <= 512 * 1024 else b""
        art_key = _art_key(artwork)
        title = str(snapshot.get("title") or "").strip()[:512] or "Выберите музыку"
        artist = str(snapshot.get("artist") or "").strip()[:512] or "Файл на этом компьютере"
        label = str(snapshot.get("state_label") or "").strip()
        signature = (state, error, title, artist, snapshot.get("duration_sec"), snapshot.get("position_sec"), art_key, label)
        if signature == self._last_snapshot:
            return
        copy_changed = self._last_snapshot is None or signature[:4] != self._last_snapshot[:4] or signature[6:] != self._last_snapshot[6:]
        self._last_snapshot = signature
        self._artwork = artwork
        self._art_key = art_key
        self._title_text = title
        self._artist_text = artist
        self._state_text = error or label or STATE_LABELS.get(state, state)
        self._state_fill = "#f36b76" if error else LILAC
        self._duration = float(snapshot.get("duration_sec") or 0)
        self._position = float(snapshot.get("position_sec") or 0)
        if copy_changed:
            self._paint()
        self._draw_bar()


class MusicControls(tk.Frame):
    """Transport stays together; volume moves below it on compact windows."""

    def __init__(self, parent):
        super().__init__(parent, bg="#202022")
        self.transport = tk.Frame(self, bg="#202022", width=1, height=1)
        self.volume_box = tk.Frame(self, bg="#202022")
        self._wide = None
        self._button_layout = None
        self.columnconfigure(0, weight=1)
        self.bind("<Configure>", self._arrange, add="+")
        self.transport.bind("<Configure>", self._arrange_buttons, add="+")

    def _arrange(self, event=None):
        width = event.width if event is not None else self.winfo_width()
        single_row = sum(button.winfo_reqwidth() + 10 for button in self.transport.winfo_children())
        wide = width >= max(720, single_row + self.volume_box.winfo_reqwidth() + 24)
        if wide == self._wide:
            self._arrange_buttons()
            return
        self._wide = wide
        self.transport.grid(row=0, column=0, sticky="ew")
        self.volume_box.grid(row=0 if wide else 1, column=1 if wide else 0,
                             sticky="e" if wide else "w", pady=0 if wide else (12, 0), padx=(18, 0) if wide else 0)
        self._arrange_buttons()

    def _arrange_buttons(self, _event=None):
        width = self.transport.winfo_width()
        buttons = self.transport.winfo_children()
        key = (width, tuple((button.winfo_reqwidth(), button.winfo_reqheight()) for button in buttons))
        if width < 8 or key == self._button_layout:
            return
        self._button_layout = key
        x = y = line_height = 0
        # Independent positioned rows avoid grid columns sharing the widest
        # button on a different row and overflowing at high DPI.
        for button in buttons:
            w, h = button.winfo_reqwidth(), button.winfo_reqheight()
            if x and x + w > width:
                y += line_height + 10
                x = line_height = 0
            button.pack_forget()
            button.place(x=x, y=y, width=w, height=h)
            x += w + 10
            line_height = max(line_height, h)
        self.transport.configure(height=max(1, y + line_height))


class LyricsPane(tk.Frame):
    """Embedded lyrics for the current track. The text never comes from a PC transcript."""

    def __init__(self, parent) -> None:
        super().__init__(parent, bg=CARD)
        self.lyrics = ""
        self._synced = False
        self._times: list[float] = []
        self._active = -1
        self.view = tk.Text(
            self, height=14, wrap="word", bg=CARD, fg="#d5d5dc", relief="flat",
            highlightthickness=0, borderwidth=0, padx=8, pady=6, font=("Segoe UI", 12),
            cursor="arrow", state="disabled",
        )
        self.view.pack(fill="both", expand=True)
        self.view.tag_configure("line", spacing1=1, spacing3=5)
        self.view.tag_configure("current", background="#323238", foreground=TEXT)
        self.view.tag_configure("empty", foreground="#8d8d98")
        self.show("", 0)

    def show(self, lyrics: str, position: float) -> None:
        lyrics = lyrics if isinstance(lyrics, str) else ""
        if lyrics != self.lyrics:
            self.lyrics = lyrics
            self._fill(lyrics)
            self._active = -1
        self._mark(position)

    def _fill(self, lyrics: str) -> None:
        rows, self._synced, self._times = _lyric_rows(lyrics)
        self.view.configure(state="normal")
        self.view.delete("1.0", "end")
        if not rows:
            self.view.insert("1.0", "Встроенного текста в этом треке нет.", "empty")
        else:
            for index, text in enumerate(rows):
                if index:
                    self.view.insert("end", "\n")
                self.view.insert("end", text, ("line", f"row-{index}"))
        self.view.configure(state="disabled")

    def _mark(self, position: float) -> None:
        if not self._synced or not self.view.winfo_exists():
            return
        try:
            position = float(position)
        except (TypeError, ValueError):
            position = 0.0
        active = 0
        for index, stamp in enumerate(self._times):
            if stamp <= position + 0.05:
                active = index
            else:
                break
        if active == self._active:
            return
        self.view.tag_remove("current", "1.0", "end")
        self.view.tag_add("current", f"row-{active}.first", f"row-{active}.last")
        self.view.see(f"row-{active}.first")
        self._active = active


class MusicDetails(tk.Frame):
    """Queue and lyrics sit together; they stack when the window is narrow."""

    def __init__(self, parent) -> None:
        super().__init__(parent, bg="#202022")
        self.queue = RoundedPanel(self, bg=CARD, padx=8, pady=8)
        self.lyrics_card = RoundedPanel(self, bg=CARD, padx=14, pady=12)
        self._wide = None
        self.columnconfigure(0, weight=1)
        self.bind("<Configure>", self._arrange, add="+")

    def _arrange(self, event=None) -> None:
        width = event.width if event is not None else self.winfo_width()
        wide = width >= 860
        if wide == self._wide and self.queue.winfo_manager():
            return
        self._wide = wide
        self.queue.grid_forget()
        self.lyrics_card.grid_forget()
        if wide:
            self.columnconfigure(0, weight=1, uniform="music")
            self.columnconfigure(1, weight=1, uniform="music")
            self.queue.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
            self.lyrics_card.grid(row=0, column=1, sticky="nsew")
        else:
            self.columnconfigure(0, weight=1, uniform="")
            self.columnconfigure(1, weight=0, uniform="")
            self.queue.grid(row=0, column=0, columnspan=2, sticky="nsew")
            self.lyrics_card.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))


def build_music(app) -> None:
    player = app.local_music()
    app._header("Музыка", "Сейчас играет, обложка, очередь и текст.")
    cast_state = {
        "status": _cast_status(),
        "submitted": 0,
        "hold_refresh_until": 0.0,
        "local_snapshot": {},
        "local_presentation": {},
        "silenced_cast": None,
    }
    previous_pump = getattr(app, "_music_cast_pump", None)
    if previous_pump is not None:
        previous_pump.close()
    cast_pump = CastCommandPump()
    app._music_cast_pump = cast_pump

    def silence_local_for_cast() -> None:
        status = cast_state["status"]
        if not _cast_live(status):
            cast_state["silenced_cast"] = None
            return
        opening = getattr(app, "_local_music_job", None)
        pending = opening if opening is not None and not opening.done.is_set() else None
        signature = (status.get("track_id"), id(pending) if pending is not None else 0)
        if signature == cast_state["silenced_cast"]:
            return
        cast_state["silenced_cast"] = signature
        app._local_music_silence_job = LocalSilenceJob(player, pending)

    def cast_drives() -> bool:
        live = _cast_live(cast_state["status"])
        if live:
            # Desktop-local playback and the agent player live in different
            # processes. A cast must silence the local one instead of hiding it.
            silence_local_for_cast()
        else:
            cast_state["silenced_cast"] = None
        return live

    def shown_snapshot() -> dict:
        if cast_drives():
            status = cast_state["status"]
            shown = dict(_cast_display(status))
            state = str(shown.get("state") or "")
            error = str(shown.get("error") or "").strip()
            shown["error"] = error
            shown["state_label"] = error or f"{CAST_LABELS.get(state, state)} · плеер XASS"
            lyrics = status.get("lyrics")
            shown["lyrics"] = lyrics if isinstance(lyrics, str) else ""
            shown["artwork"] = _phone_cover(str(status.get("cover_sha256") or ""))
            shown["source"] = "phone"
            return shown
        opening = getattr(app, "_local_music_job", None)
        opening_active = opening is not None and not opening.done.is_set()
        shown = dict(_local_snapshot(player, opening, cast_state["local_snapshot"]))
        if not opening_active:
            cast_state["local_snapshot"] = dict(shown)
            notes = _player_notes(player)
            cast_state["local_presentation"] = dict(notes)
        else:
            notes = cast_state["local_presentation"]
        lyrics = notes.get("lyrics")
        artwork = notes.get("artwork")
        shown["lyrics"] = lyrics if isinstance(lyrics, str) else ""
        shown["artwork"] = bytes(artwork) if isinstance(artwork, (bytes, bytearray)) else b""
        shown["source"] = "local"
        return shown

    previous = getattr(app, "_music_poll", None)
    if previous is not None:
        try:
            app.root.after_cancel(previous)
        except tk.TclError:
            pass

    notice = tk.StringVar(value="")
    catalog = {"tracks": [], "total": 0, "error": "", "loading": False, "revision": 0, "retry_at": 0.0,
               "offset": 0, "next_offset": None, "query": ""}
    search_query = tk.StringVar(value="")

    def refresh_catalog(*, offset=None, query=None) -> None:
        current = getattr(app, "_music_catalog_job", None)
        if current is not None and not current.done.is_set():
            return
        catalog["loading"] = True
        catalog["error"] = ""
        if offset is not None:
            catalog["offset"] = offset
        if query is not None:
            catalog["query"] = str(query).strip()[:120]
        app._music_catalog_job = ServerCatalogJob(getattr(app, "config", {}),
                                                  offset=catalog["offset"], query=catalog["query"])
        try:
            refresh_rows()
        except (NameError, UnboundLocalError):
            pass

    refresh_catalog()

    def active_opening():
        opening = getattr(app, "_local_music_job", None)
        return opening if opening is not None and not opening.done.is_set() else None

    def allow_local_start() -> bool:
        if cast_drives():
            notice.set("Сначала остановите трек с телефона — XASS не запустит второй плеер поверх него.")
            return False
        return True

    def play_path(path: Path) -> None:
        if getattr(app, "preview", False):
            notice.set("Предпросмотр: открытие файлов и звук отключены.")
            return
        if not allow_local_start():
            return
        previous = getattr(app, "_local_music_job", None)
        if previous is not None and not previous.done.is_set():
            notice.set("Открываю трек…")
            return
        notice.set("Открываю трек…")
        app._local_music_job = LocalOpenJob(player, path)

    def choose() -> None:
        if getattr(app, "preview", False):
            notice.set("Предпросмотр: открытие файлов и звук отключены.")
            return
        if not allow_local_start():
            return
        selected = filedialog.askopenfilenames(
            parent=app.root, title="Добавить музыку с этого ПК", filetypes=AUDIO_TYPES,
        )
        paths = [Path(item) for item in selected if Path(item).suffix.lower() in {".mp3", ".wav", ".flac", ".ogg"}]
        if paths:
            try:
                for path in reversed(paths):
                    remember_track(path)
            except OSError:
                notice.set("Не удалось сохранить список, но первый трек можно воспроизвести.")
            refresh_rows()
            play_path(paths[0])

    def ready_paths() -> list[str]:
        return [item for item in load_playlist() if Path(item).is_file()]

    def step(direction: int) -> None:
        if not allow_local_start():
            return
        paths = ready_paths()
        chosen = next_path(paths, str(getattr(player, "_path", "") or ""), direction)
        if chosen is None:
            notice.set("Сначала откройте файл")
            return
        play_path(chosen)

    def play_server(track: dict) -> None:
        if getattr(app, "preview", False):
            notice.set("Предпросмотр: запуск музыки отключён.")
            return
        current = getattr(app, "_music_server_play_job", None)
        if current is not None and not current.done.is_set():
            notice.set("Предыдущая команда ещё отправляется…")
            return
        try:
            volume_value = float(shown_snapshot().get("volume") or 70)
        except (TypeError, ValueError):
            volume_value = 70
        notice.set(f"Запускаю «{str(track.get('title') or 'трек')[:80]}» на этом ПК…")
        app._music_server_play_job = ServerPlayJob(
            getattr(app, "config", {}), int(track["id"]), volume=volume_value,
        )

    def cast_control(command: str, payload: dict | None = None) -> None:
        notice.set("")
        sequence = cast_pump.submit(command, payload)
        if sequence is None:
            notice.set("Плеер уже закрывается")
            return
        cast_state["submitted"] = sequence
        cast_state["status"] = _optimistic_cast(cast_state["status"], command, payload)
        # The bridge status file is written on the heartbeat cadence. Do not
        # replace the immediate state with its previous revision meanwhile.
        cast_state["hold_refresh_until"] = time.monotonic() + 2.5
        reveal()

    def apply_cast_results() -> None:
        for sequence, _command, _payload, response, error in cast_pump.drain():
            if error:
                notice.set(error)
                if sequence >= cast_state["submitted"]:
                    cast_state["hold_refresh_until"] = 0.0
                continue
            player_state = response.get("player") if isinstance(response, dict) else None
            if sequence >= cast_state["submitted"] and isinstance(player_state, dict):
                cast_state["status"] = {**cast_state["status"], **player_state}
                cast_state["hold_refresh_until"] = time.monotonic() + 0.9
                notice.set("")

    def refresh_cast(*, force: bool = False) -> None:
        if not stage.winfo_exists():
            return
        if not force and (cast_pump.busy() or time.monotonic() < cast_state["hold_refresh_until"]):
            return
        cast_state["status"] = _cast_status()

    def control(command: str) -> None:
        if command == "music_stop" and cast_drives():
            cast_control("music_stop")
            return
        opening = active_opening()
        if opening is not None:
            if command == "music_stop":
                opening.cancel()
                app._local_music_silence_job = LocalSilenceJob(player, opening)
                notice.set("Останавливаю открытие трека…")
            else:
                notice.set("Дождитесь открытия трека")
            return
        try:
            notice.set("")
            player.command(command, {}, {})
            reveal()
        except MusicError as exc:
            notice.set(str(exc))

    def toggle() -> None:
        if cast_drives():
            state = str(cast_state["status"].get("state") or "")
            if state == "playing":
                cast_control("music_pause")
            elif state in {"paused", "ended"}:
                cast_control("music_resume")
            elif state == "error":
                cast_control("music_stop")
            return
        state = str(player.snapshot().get("state") or "idle")
        if state == "playing":
            control("music_pause")
        elif state in {"paused", "ended"}:
            control("music_resume")
        else:
            choose()

    def control_seek(seconds: float) -> None:
        if cast_drives():
            cast_control("music_seek", {"position_sec": seconds})
            return
        if active_opening() is not None:
            notice.set("Дождитесь открытия трека")
            return
        try:
            notice.set("")
            player.command("music_seek", {"position_sec": seconds}, {})
            reveal()
        except MusicError as exc:
            notice.set(str(exc))

    def control_volume(value: float) -> None:
        if cast_drives():
            cast_control("music_volume", {"volume": value})
            return
        if active_opening() is not None:
            notice.set("Дождитесь открытия трека")
            return
        try:
            player.command("music_volume", {"volume": value}, {})
        except MusicError as exc:
            notice.set(str(exc))

    stage = MusicStage(app.content, player, {
        "toggle": toggle,
        "previous": lambda: step(-1),
        "next": lambda: step(1),
        "stop": lambda: control("music_stop"),
        "open": choose,
        "seek": lambda seconds: control_seek(seconds),
        "volume": lambda value: control_volume(value),
    })
    stage.pack(fill="x", pady=(0, 12))
    stage.bind("<FocusIn>", lambda _event: _reveal_focused_widget(app, stage), add="+")
    controls = MusicControls(app.content)
    controls.pack(fill="x", pady=(0, 8))

    def icon_button(icon: str, command):
        button = ModernButton(
            controls.transport, text="", icon=icon, icon_size=18, command=command,
            bg="#252529", fg=TEXT, activebackground="#33333a", parent_bg="#202022",
            border_color="#44444c", padx=12, pady=12,
        )
        button.pack(side="left", padx=(0, 10))
        return button

    previous_button = icon_button("previous", lambda: step(-1))
    stage.play_button = ModernButton(
        controls.transport, text="Играть", icon="play", command=toggle,
        bg=BLUE, fg="#12131c", activebackground="#9aafff", parent_bg="#202022",
        padx=18, pady=11, font=("Segoe UI Semibold", 11),
    )
    stage.play_button.pack(side="left", padx=(0, 10))
    next_button = icon_button("next", lambda: step(1))
    icon_button("stop", lambda: control("music_stop"))
    open_button = ModernButton(
        controls.transport, text="Добавить музыку", command=choose,
        bg="#252529", fg=TEXT, activebackground="#33333a", parent_bg="#202022",
        border_color="#44444c", padx=16, pady=11, font=("Segoe UI Semibold", 11),
    )
    open_button.pack(side="left")

    def update_local_controls() -> None:
        state = "disabled" if cast_drives() or active_opening() is not None else "normal"
        for button in (previous_button, next_button, open_button):
            button.configure(state=state)

    volume_box = controls.volume_box
    tk.Label(volume_box, text="Громкость", bg="#202022", fg=MUTED, font=("Segoe UI", 10)).pack(side="left", padx=(0, 10))
    volume = MusicVolume(volume_box, control_volume)
    volume.pack(side="left")
    volume.bind("<FocusIn>", lambda _event: _reveal_focused_widget(app, volume), add="+")

    def paint_volume(value: float) -> None:
        volume.set(value)
    initial_volume = _local_snapshot(
        player, getattr(app, "_local_music_job", None), cast_state["local_snapshot"],
    ).get("volume")
    paint_volume(float(70 if initial_volume is None else initial_volume))
    controls._arrange()
    tk.Label(app.content, textvariable=notice, bg="#202022", fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 10))

    details = MusicDetails(app.content)
    details.pack(fill="x")
    library = details.queue
    heading = tk.Frame(library, bg=CARD)
    heading.pack(fill="x", padx=10, pady=(8, 4))
    tk.Label(heading, text="Моя музыка", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 13)).pack(side="left")
    count = tk.Label(heading, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 11))
    count.pack(side="right", padx=(10, 0))
    refresh_button = ModernButton(
        heading, text="Обновить", command=refresh_catalog,
        bg="#29292f", fg="#c9c9d2", activebackground="#36363e", parent_bg=CARD,
        border_color="#404048", padx=10, pady=5, font=("Segoe UI Semibold", 9),
    )
    refresh_button.pack(side="right")
    search_bar = tk.Frame(library, bg=CARD)
    search_bar.pack(fill="x", padx=12, pady=(8, 4))
    search_bar.columnconfigure(0, weight=1)
    search_entry = tk.Entry(search_bar, textvariable=search_query, bg="#202022", fg=TEXT,
                            insertbackground=TEXT, relief="flat", font=("Segoe UI", 11),
                            width=10, highlightthickness=2, highlightbackground="#41414b", highlightcolor=BLUE)
    search_entry.grid(row=0, column=0, sticky="ew", ipady=7, padx=(0, 8))
    def search_catalog(_event=None):
        refresh_catalog(offset=0, query=search_query.get())
        return "break"
    search_entry.bind("<Return>", search_catalog)
    search_button = ModernButton(search_bar, text="Найти", command=search_catalog, bg="#29292f",
                                 fg=TEXT, border_color="#404048", padx=12, pady=8)
    search_button.grid(row=0, column=1)
    pagination = tk.Frame(library, bg=CARD)
    pagination.pack(fill="x", padx=12, pady=(4, 8))
    previous_page = ModernButton(pagination, text="Назад", command=lambda: refresh_catalog(offset=max(0, catalog["offset"] - 100)),
                                padx=12, pady=7)
    previous_page.pack(side="left")
    next_page = ModernButton(pagination, text="Далее", command=lambda: refresh_catalog(offset=catalog["next_offset"]),
                            padx=12, pady=7)
    next_page.pack(side="right")
    page_label = tk.Label(pagination, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 10))
    page_label.pack(side="left", fill="x", expand=True, padx=8)
    rows = tk.Frame(library, bg=CARD)
    rows.pack(fill="x")
    lyric_heading = tk.Frame(details.lyrics_card, bg=CARD)
    lyric_heading.pack(fill="x")
    tk.Label(lyric_heading, text="Текст", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 13)).pack(side="left")
    lyrics_pane = LyricsPane(details.lyrics_card)
    lyrics_pane.pack(fill="both", expand=True, pady=(8, 0))

    def refresh_rows() -> None:
        if not rows.winfo_exists():
            return
        focused = rows.focus_get()
        focused_key = None
        while focused is not None and focused is not rows:
            if hasattr(focused, "_music_row_key"):
                focused_key = focused._music_row_key
                break
            focused = getattr(focused, "master", None)
        focus_target = None
        for child in rows.winfo_children():
            child.destroy()
        items = load_playlist()
        server_tracks = list(catalog["tracks"])
        current = str(getattr(player, "_path", "") or "")
        phone = cast_drives()
        total = max(len(server_tracks), int(catalog["total"] or 0))
        count.configure(text=f"{len(server_tracks)} из {total}" if total else "")
        loading = catalog["loading"]
        for control_widget in (refresh_button, search_button, search_entry):
            control_widget.configure(state="disabled" if loading else "normal")
        previous_page.configure(state="normal" if not loading and catalog["offset"] > 0 else "disabled")
        next_page.configure(state="normal" if not loading and catalog["next_offset"] is not None else "disabled")
        page_label.configure(text="Загрузка…" if loading else
                             f"{catalog['offset'] + 1}–{catalog['offset'] + len(server_tracks)} из {total}" if server_tracks else "Нет треков")

        def section(title: str, subtitle: str = "") -> None:
            bar = tk.Frame(rows, bg=CARD)
            bar.pack(fill="x", padx=12, pady=(12, 5))
            tk.Label(bar, text=title, bg=CARD, fg="#b9b9c4", font=("Segoe UI Semibold", 9), anchor="w").pack(side="left")
            if subtitle:
                tk.Label(bar, text=subtitle, bg=CARD, fg="#777783", font=("Segoe UI", 8), anchor="e").pack(side="right")

        if phone:
            status = cast_state["status"]
            row = tk.Frame(rows, bg="#323238", cursor="hand2")
            row.pack(fill="x", padx=6, pady=2)
            tk.Frame(row, bg=LILAC, width=3).pack(side="left", fill="y", padx=(0, 12))
            copy = tk.Frame(row, bg="#323238")
            copy.pack(side="left", fill="x", expand=True, pady=9)
            tk.Label(
                copy, text=str(status.get("title") or "").strip() or "Трек XASS",
                bg="#323238", fg=BLUE, font=("Segoe UI Semibold", 12), anchor="w",
            ).pack(fill="x")
            artist = str(status.get("artist") or "").strip() or "Библиотека XASS"
            tk.Label(copy, text=artist, bg="#323238", fg=MUTED, font=("Segoe UI", 9), anchor="w").pack(fill="x")
            tk.Label(row, text="сейчас", bg="#323238", fg=LILAC, font=("Segoe UI", 9)).pack(side="right", padx=12)

        section("НА СЕРВЕРЕ", "результаты поиска" if catalog["query"] else "обновляется автоматически")
        if catalog["loading"]:
            tk.Label(
                rows, text="Загружаю библиотеку…",
                bg=CARD, fg=MUTED, font=("Segoe UI", 11), anchor="w",
            ).pack(fill="x", padx=12, pady=10)
        elif catalog["error"]:
            tk.Label(
                rows, text=str(catalog["error"]), bg=CARD, fg="#d8a7aa",
                font=("Segoe UI", 10), anchor="w", justify="left",
            ).pack(fill="x", padx=12, pady=10)
        elif not server_tracks:
            tk.Label(
                rows, text="Ничего не найдено. Измените запрос." if catalog["query"] else "На сервере пока нет музыки. Добавьте треки с iPhone, Telegram или сайта.",
                bg=CARD, fg=MUTED, font=("Segoe UI", 10), anchor="w", justify="left",
            ).pack(fill="x", padx=12, pady=10)
        for track in server_tracks:
            active = phone and int(cast_state["status"].get("track_id") or 0) == track["id"]
            tone = "#323238" if active else CARD
            row = tk.Frame(rows, bg=tone, cursor="hand2")
            row.pack(fill="x", padx=6, pady=2)
            badge = tk.Label(
                row, text="♫", width=3, bg="#242630", fg=LILAC,
                font=("Segoe UI Symbol", 15), padx=5, pady=7,
            )
            badge.pack(side="left", padx=(4, 12), pady=5)
            copy = tk.Frame(row, bg=tone)
            copy.pack(side="left", fill="x", expand=True, pady=8)
            tk.Label(
                copy, text=track["title"], bg=tone, fg=BLUE if active else TEXT,
                font=("Segoe UI Semibold", 11), anchor="w",
            ).pack(fill="x")
            album = f" · {track['album']}" if track["album"] else ""
            tk.Label(
                copy, text=f"{track['artist']}{album}", bg=tone, fg=MUTED,
                font=("Segoe UI", 9), anchor="w",
            ).pack(fill="x")
            tail = "сейчас" if active else ("★  " if track["favorite"] else "") + _clock(track["duration"])
            tk.Label(row, text=tail, bg=tone, fg=LILAC if active else MUTED,
                     font=("Segoe UI", 9)).pack(side="right", padx=12)

            def play_remote(_event=None, chosen: dict = track) -> None:
                play_server(chosen)

            _music_row_accessibility(row, play_remote, app)
            row._music_row_key = ("server", track["id"])
            if row._music_row_key == focused_key:
                focus_target = row

        section("НА ЭТОМ КОМПЬЮТЕРЕ", "локальные файлы")
        if not items:
            tk.Label(
                rows, text="Добавьте MP3, WAV, FLAC или OGG — файлы останутся только на этом ПК.",
                bg=CARD, fg=MUTED, font=("Segoe UI", 10), anchor="w", justify="left",
            ).pack(fill="x", padx=12, pady=10)
        for item in items:
            path = Path(item)
            missing = not path.is_file()
            active = item == current and not missing and not phone
            tone = "#323238" if active else CARD
            row = tk.Frame(rows, bg=tone, cursor="hand2")
            row.pack(fill="x", padx=6, pady=2)
            mark = tk.Frame(row, bg=BLUE if active else tone, width=3)
            mark.pack(side="left", fill="y", padx=(0, 12))
            copy = tk.Frame(row, bg=tone)
            copy.pack(side="left", fill="x", expand=True, pady=9)
            tk.Label(
                copy, text=path.stem, bg=tone, fg=BLUE if active else ("#6d6d76" if missing else TEXT),
                font=("Segoe UI Semibold", 12), anchor="w",
            ).pack(fill="x")
            folder = "Файл удалён с диска" if missing else path.parent.name
            tk.Label(copy, text=folder, bg=tone, fg=MUTED, font=("Segoe UI", 9), anchor="w").pack(fill="x")
            if active:
                tk.Label(row, text="сейчас", bg=tone, fg=LILAC, font=("Segoe UI", 9)).pack(side="right", padx=12)

            def play(_event=None, chosen: Path = path, gone: bool = missing) -> None:
                if gone:
                    notice.set("Этого файла уже нет на диске")
                    return
                play_path(chosen)

            _music_row_accessibility(row, play, app)
            row._music_row_key = ("local", item)
            if row._music_row_key == focused_key:
                focus_target = row
        if focus_target is not None:
            focus_target.focus_set()

    def reveal() -> dict:
        snapshot = shown_snapshot()
        update_local_controls()
        stage.show(snapshot)
        controls._arrange()
        if lyrics_pane.winfo_exists():
            try:
                position = float(snapshot.get("position_sec") or 0)
            except (TypeError, ValueError):
                position = 0.0
            lyrics_pane.show(str(snapshot.get("lyrics") or ""), position)
        paint_volume(float(snapshot.get("volume") or 0))
        return snapshot

    def tick() -> None:
        if app.current_view != "music" or not stage.winfo_exists():
            cast_pump.close()
            if getattr(app, "_music_cast_pump", None) is cast_pump:
                app._music_cast_pump = None
            app._music_poll = None
            return
        apply_cast_results()
        refresh_cast()
        catalog_job = getattr(app, "_music_catalog_job", None)
        if catalog_job is not None and catalog_job.done.is_set():
            app._music_catalog_job = None
            catalog["tracks"] = list(catalog_job.tracks)
            catalog["total"] = int(catalog_job.total)
            catalog["offset"] = catalog_job.offset
            catalog["next_offset"] = catalog_job.next_offset
            catalog["query"] = catalog_job.query
            search_query.set(catalog_job.query)
            catalog["error"] = str(catalog_job.error or "")
            catalog["loading"] = False
            catalog["revision"] += 1
            catalog["retry_at"] = time.monotonic() + (30.0 if catalog["error"] else 120.0)
        elif (catalog_job is None and catalog["retry_at"]
              and time.monotonic() >= catalog["retry_at"]):
            catalog["retry_at"] = 0.0
            refresh_catalog()
        play_job = getattr(app, "_music_server_play_job", None)
        if play_job is not None and play_job.done.is_set():
            app._music_server_play_job = None
            notice.set(play_job.error or "Команда отправлена. XASS подключает плеер…")
        job = getattr(app, "_local_music_job", None)
        if job is not None and job.done.is_set():
            app._local_music_job = None
            notice.set(job.error)
            refresh_rows()
        reveal()
        marker = (str(getattr(player, "_path", "") or ""), bool(cast_drives()),
                  str(cast_state["status"].get("track_id") or ""), catalog["revision"])
        if marker != getattr(rows, "_shown_path", None):
            rows._shown_path = marker
            refresh_rows()
        app._music_poll = app.root.after(250, tick)

    refresh_rows()
    refresh_cast()
    reveal()
    details._arrange()
    app._music_poll = app.root.after(250, tick)
