"""Local music page. Files on this PC play here; the server is not involved."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk

from client_update import DATA_ROOT
from desktop_widgets import CARD, MUTED, TEXT, ModernButton, RoundedPanel, rounded_image
from music_player import MusicError
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
        self.error = ""
        self.snapshot = None
        self.thread = threading.Thread(target=self._run, args=(player, path), name="xass-local-music", daemon=True)
        self.thread.start()

    def _run(self, player, path):
        try:
            self.snapshot = player.play_local(path)
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


def _disc(size: int, angle: int) -> Image.Image:
    scale = 3
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((scale, scale, canvas - scale, canvas - scale), fill=(12, 12, 16, 255))
    for inset, alpha in ((18, 70), (30, 90), (42, 70), (54, 50)):
        pad = inset * scale
        draw.ellipse((pad, pad, canvas - pad, canvas - pad), outline=(170, 176, 198, alpha), width=max(1, scale))
    draw.ellipse((10 * scale, 10 * scale, canvas - 11 * scale, canvas - 11 * scale), outline=(130, 156, 255, 230), width=3 * scale)
    draw.arc((22 * scale, 22 * scale, canvas - 23 * scale, canvas - 23 * scale), start=angle, end=angle + 150, fill=(196, 149, 244, 255), width=5 * scale)
    hole = canvas // 2
    radius = 16 * scale
    draw.ellipse((hole - radius, hole - radius, hole + radius, hole + radius), fill=(236, 236, 242, 255))
    inner = 5 * scale
    draw.ellipse((hole - inner, hole - inner, hole + inner, hole + inner), fill=(12, 12, 16, 255))
    return image.resize((size, size), Image.Resampling.LANCZOS)


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


class MusicStage(tk.Canvas):
    """Player surface. Artwork is drawn; the title and controls stay real widgets."""

    def __init__(self, parent, player, actions: dict) -> None:
        super().__init__(parent, bg="#202022", height=300, highlightthickness=0, borderwidth=0)
        self.player = player
        self.actions = actions
        self._plate = None
        self._plate_size = None
        self._discs: dict[int, Image.Image] = {}
        self._photo = None
        self._disc_photos = {}
        self._animation = None
        self._resize_job = None
        self._playing = False
        self._last_snapshot = None
        self._angle = 0
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
        self._disc_art = self.create_image(0, 0, anchor="nw")
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
        self.bind("<Destroy>", self._destroyed, add="+")
        self.bind("<Map>", lambda _event: self._arm_animation(), add="+")

    def _destroyed(self, event) -> None:
        if event.widget is self:
            for name in ("_animation", "_resize_job"):
                callback = getattr(self, name)
                if callback:
                    self.after_cancel(callback)
                    setattr(self, name, None)
            self._plate = None
            self._photo = None
            self._disc_photos.clear()

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
        # Keep the compact title below the 184px disc, even when point fonts
        # grow at 150–200% Windows scaling. The text stack must also leave room
        # for complete status/time lines and the scrubber rather than overlap.
        title_top = max(24 if wide else 228, (86 if wide else 248) - title_height // 2)
        stack = (title_height + 8 + self._artist_font.metrics("linespace") + 10
                 + self._state_font.metrics("linespace") + 14
                 + self._time_font.metrics("linespace") + 8 + 8 + 12)
        return max(300 if wide else 460, title_top + stack + 2)

    def _disc(self, angle: int) -> Image.Image:
        key = int(angle) % 360
        cached = self._discs.get(key)
        if cached is None:
            cached = _disc(184, key)
            self._discs[key] = cached
        return cached

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
        self.coords(self._disc_art, *(36, max(24, (height - 184) // 2)) if wide else (24, 28))
        self._paint_disc()
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

    def _paint_disc(self) -> None:
        if self._angle not in self._disc_photos:
            self._disc_photos[self._angle] = ImageTk.PhotoImage(self._disc(self._angle), master=self)
        self.itemconfigure(self._disc_art, image=self._disc_photos[self._angle])

    def _arm_animation(self) -> None:
        if self._playing and self._animation is None and self.winfo_viewable():
            self._animation = self.after(50, self._animate)

    def _animate(self) -> None:
        self._animation = None
        if not self._playing or not self.winfo_viewable():
            return
        self._angle = (self._angle + 18) % 360
        self._paint_disc()
        self._arm_animation()

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

    def show(self, snapshot: dict) -> None:
        if not self.winfo_exists():
            return
        state = str(snapshot.get("state") or "idle")
        error = str(snapshot.get("error") or "").strip()
        playing = state == "playing"
        self._playing = playing
        if not playing and self._animation is not None:
            self.after_cancel(self._animation)
            self._animation = None
        self._arm_animation()
        title = str(snapshot.get("title") or "").strip()[:512] or "Выберите музыку"
        artist = str(snapshot.get("artist") or "").strip()[:512] or "Файл на этом компьютере"
        signature = (state, error, title, artist, snapshot.get("duration_sec"), snapshot.get("position_sec"))
        if signature == self._last_snapshot:
            return
        copy_changed = self._last_snapshot is None or signature[:4] != self._last_snapshot[:4]
        self._last_snapshot = signature
        self._title_text = title
        self._artist_text = artist
        self._state_text = error or STATE_LABELS.get(state, state)
        self._state_fill = "#f36b76" if error else LILAC
        self._duration = float(snapshot.get("duration_sec") or 0)
        self._position = float(snapshot.get("position_sec") or 0)
        if self.play_button is not None:
            self.play_button.configure(text="Пауза" if playing else "Играть", icon="pause" if playing else "play")
        if copy_changed:
            self._paint()
        self._draw_bar()


class MusicControls(tk.Frame):
    """Transport stays together; volume moves below it on compact windows."""

    def __init__(self, parent):
        super().__init__(parent, bg="#202022")
        self.transport = tk.Frame(self, bg="#202022")
        self.volume_box = tk.Frame(self, bg="#202022")
        self._wide = None
        self.columnconfigure(0, weight=1)
        self.bind("<Configure>", self._arrange, add="+")

    def _arrange(self, event=None):
        width = event.width if event is not None else self.winfo_width()
        wide = width >= max(720, self.transport.winfo_reqwidth() + self.volume_box.winfo_reqwidth() + 24)
        if wide == self._wide:
            return
        self._wide = wide
        self.transport.grid(row=0, column=0, sticky="w")
        self.volume_box.grid(row=0 if wide else 1, column=1 if wide else 0,
                             sticky="e" if wide else "w", pady=0 if wide else (12, 0), padx=(18, 0) if wide else 0)


def build_music(app) -> None:
    player = app.local_music()
    app._header("Музыка", "Локальные файлы. Музыка из Telegram управляется отдельно в Mini App.")
    previous = getattr(app, "_music_poll", None)
    if previous is not None:
        try:
            app.root.after_cancel(previous)
        except tk.TclError:
            pass

    notice = tk.StringVar(value="")

    def play_path(path: Path) -> None:
        if getattr(app, "preview", False):
            notice.set("Предпросмотр: открытие файлов и звук отключены.")
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
        selected = filedialog.askopenfilename(parent=app.root, title="Открыть музыку на этом ПК", filetypes=AUDIO_TYPES)
        if selected:
            play_path(Path(selected))

    def ready_paths() -> list[str]:
        return [item for item in load_playlist() if Path(item).is_file()]

    def step(direction: int) -> None:
        paths = ready_paths()
        chosen = next_path(paths, str(getattr(player, "_path", "") or ""), direction)
        if chosen is None:
            notice.set("Сначала откройте файл")
            return
        play_path(chosen)

    def control(command: str) -> None:
        try:
            notice.set("")
            stage.show(player.command(command, {}, {}))
        except MusicError as exc:
            notice.set(str(exc))

    def toggle() -> None:
        state = str(player.snapshot().get("state") or "idle")
        if state == "playing":
            control("music_pause")
        elif state in {"paused", "ended"}:
            control("music_resume")
        else:
            choose()

    def control_seek(seconds: float) -> None:
        try:
            notice.set("")
            stage.show(player.command("music_seek", {"position_sec": seconds}, {}))
        except MusicError as exc:
            notice.set(str(exc))

    def control_volume(value: float) -> None:
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

    icon_button("previous", lambda: step(-1))
    stage.play_button = ModernButton(
        controls.transport, text="Играть", icon="play", command=toggle,
        bg=BLUE, fg="#12131c", activebackground="#9aafff", parent_bg="#202022",
        padx=18, pady=11, font=("Segoe UI Semibold", 11),
    )
    stage.play_button.pack(side="left", padx=(0, 10))
    icon_button("next", lambda: step(1))
    icon_button("stop", lambda: control("music_stop"))
    ModernButton(
        controls.transport, text="Открыть файл", command=choose,
        bg="#252529", fg=TEXT, activebackground="#33333a", parent_bg="#202022",
        border_color="#44444c", padx=16, pady=11, font=("Segoe UI Semibold", 11),
    ).pack(side="left")

    volume_box = controls.volume_box
    tk.Label(volume_box, text="Громкость", bg="#202022", fg=MUTED, font=("Segoe UI", 10)).pack(side="left", padx=(0, 10))
    volume = tk.Canvas(volume_box, width=120, height=22, bg="#202022", highlightthickness=0, borderwidth=0)
    volume.pack(side="left")
    volume.create_line(6, 11, 114, 11, fill="#3a3a42", width=8, capstyle="round")
    volume_fill = volume.create_line(6, 11, 70, 11, fill=LILAC, width=8, capstyle="round")

    def paint_volume(value: float) -> None:
        span = 6 + int(108 * min(1.0, max(0.0, value / 100)))
        volume.coords(volume_fill, 6, 11, max(7, span), 11)
        volume.itemconfigure(volume_fill, state="normal" if value > 0.4 else "hidden")

    def volume_at(event) -> None:
        ratio = min(1.0, max(0.0, (event.x - 6) / 108))
        paint_volume(ratio * 100)
        control_volume(ratio * 100)

    volume.bind("<Button-1>", volume_at)
    volume.bind("<B1-Motion>", volume_at)
    initial_volume = player.snapshot().get("volume")
    paint_volume(float(70 if initial_volume is None else initial_volume))
    controls._arrange()
    tk.Label(app.content, textvariable=notice, bg="#202022", fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 10))

    library = RoundedPanel(app.content, bg=CARD, padx=8, pady=8)
    library.pack(fill="x")
    heading = tk.Frame(library, bg=CARD)
    heading.pack(fill="x", padx=10, pady=(8, 4))
    tk.Label(heading, text="На этом компьютере", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 13)).pack(side="left")
    count = tk.Label(heading, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 11))
    count.pack(side="right")
    rows = tk.Frame(library, bg=CARD)
    rows.pack(fill="x")

    def refresh_rows() -> None:
        if not rows.winfo_exists():
            return
        for child in rows.winfo_children():
            child.destroy()
        items = load_playlist()
        current = str(getattr(player, "_path", "") or "")
        count.configure(text=str(len(items)) if items else "")
        if not items:
            tk.Label(
                rows, text="Пока пусто. Откройте первый файл — он останется в этом списке.",
                bg=CARD, fg=MUTED, font=("Segoe UI", 11), anchor="w",
            ).pack(fill="x", padx=12, pady=16)
            return
        for item in items:
            path = Path(item)
            missing = not path.is_file()
            active = item == current and not missing
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

            for widget in (row, copy, mark, *copy.winfo_children(), *row.winfo_children()):
                widget.bind("<ButtonRelease-1>", play)

    def tick() -> None:
        if app.current_view != "music" or not stage.winfo_exists():
            app._music_poll = None
            return
        snapshot = player.snapshot()
        job = getattr(app, "_local_music_job", None)
        if job is not None and job.done.is_set():
            app._local_music_job = None
            notice.set(job.error)
            refresh_rows()
        stage.show(snapshot)
        paint_volume(float(snapshot.get("volume") or 0))
        if str(getattr(player, "_path", "") or "") != getattr(rows, "_shown_path", None):
            rows._shown_path = str(getattr(player, "_path", "") or "")
            refresh_rows()
        app._music_poll = app.root.after(250, tick)

    refresh_rows()
    stage.show(player.snapshot())
    app._music_poll = app.root.after(250, tick)
