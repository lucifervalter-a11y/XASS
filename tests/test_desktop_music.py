"""Local music UI regressions: withdrawn Tk, private fixtures, no audio output."""
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc_client"))
try:
    import desktop_music as music
    from desktop_widgets import ModernButton
finally:
    sys.path.pop(0)


class LibraryTests(unittest.TestCase):
    def test_playing_rows_preserves_queue_order_across_three_nexts_and_previous(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(music, "DATA_ROOT", Path(folder)):
            files = [Path(folder) / name for name in ("A.wav", "B.wav", "C.wav")]
            for file in reversed(files):
                music.remember_track(file)
            expected = [str(file.resolve()) for file in files]
            self.assertEqual(music.load_playlist(), expected)
            current = files[0]
            for selected in (files[1], files[2], files[0]):
                current = music.next_path(music.load_playlist(), str(current), 1)
                self.assertEqual(current, selected)
                music.remember_track(current)
                self.assertEqual(music.load_playlist(), expected)
            self.assertEqual(music.next_path(expected, str(files[0]), -1), files[2])
            self.assertIsNone(music.next_path([], "", 1))

    def test_open_job_does_no_audio_or_disk_work_on_caller_thread(self):
        started, release = threading.Event(), threading.Event()
        thread_ids = []
        def play(path):
            thread_ids.append(threading.get_ident())
            started.set()
            release.wait(2)
            return {"state": "playing"}
        player = SimpleNamespace(play_local=play)
        with patch.object(music, "remember_track") as remember:
            job = music.LocalOpenJob(player, Path("fixture.wav"))
            try:
                self.assertTrue(started.wait(1))
                self.assertFalse(job.done.is_set())
                self.assertNotEqual(thread_ids, [threading.get_ident()])
            finally:
                release.set()
                self.assertTrue(job.done.wait(2))
            remember.assert_called_once()
            self.assertEqual(job.error, "")

    def test_cancelled_job_does_not_add_history_and_history_error_is_nonfatal(self):
        with patch.object(music, "remember_track", side_effect=OSError("fixture")) as remember:
            player = Mock()
            player.play_local.return_value = {"state": "stopped"}
            job = music.LocalOpenJob(player, Path("fixture.wav"))
            self.assertTrue(job.done.wait(2))
            remember.assert_not_called()
            player.play_local.return_value = {"state": "playing"}
            job = music.LocalOpenJob(player, Path("fixture.wav"))
            self.assertTrue(job.done.wait(2))
            self.assertEqual(job.snapshot["state"], "playing")
            self.assertIn("список", job.error)


class MusicRenderTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.addCleanup(self.finish)

    def finish(self):
        for callback in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", callback)
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def stage(self, width=900):
        stage = music.MusicStage(self.root, Mock(), {"seek": Mock()})
        stage.place(x=0, y=0, width=width, height=300)
        self.root.update_idletasks()
        stage._paint()
        return stage

    def test_idle_and_progress_do_not_rebuild_pillow_background_or_disc(self):
        stage = self.stage()
        data = {"state": "paused", "title": "Локальный трек", "duration_sec": 90, "position_sec": 1}
        stage.show(data)
        photo = stage._photo
        with patch.object(music.ImageTk, "PhotoImage", wraps=music.ImageTk.PhotoImage) as created, \
                patch.object(music, "_plate", wraps=music._plate) as plate, \
                patch.object(stage, "_paint", wraps=stage._paint) as paint:
            for _ in range(20):
                stage.show(data)
            for position in range(2, 22):
                stage.show({**data, "position_sec": position})
            created.assert_not_called()
            plate.assert_not_called()
            paint.assert_not_called()
        self.assertIs(stage._photo, photo)
        self.assertIsNone(stage._animation)

    def test_position_only_resize_does_not_rebuild_and_small_disc_frames_are_cached(self):
        stage = self.stage()
        if stage._resize_job:
            stage.after_cancel(stage._resize_job)
            stage._resize_job = None
        with patch.object(stage, "after") as after:
            stage._resized(SimpleNamespace(widget=stage, width=900, height=300, y=-40))
            after.assert_not_called()
        for angle in range(0, 360, 18):
            stage._angle = angle
            stage._paint_disc()
        self.assertEqual(len(stage._disc_photos), 20)
        photo = stage._photo
        with patch.object(music.ImageTk, "PhotoImage") as created:
            for angle in range(0, 360, 18):
                stage._angle = angle
                stage._paint_disc()
            created.assert_not_called()
        self.assertIs(stage._photo, photo)
        self.assertTrue(all(image.size == (184, 184) for image in stage._discs.values()))

    def test_status_uses_measured_font_and_sits_below_artist_at_windows_dpi_scales(self):
        original = float(self.root.tk.call("tk", "scaling"))
        try:
            for scaling in (1.067, 1.333, 1.667, 2.0, 2.667):
                self.root.tk.call("tk", "scaling", scaling)
                for width, height in ((1184, 300), (560, 460)):
                    with self.subTest(scaling=scaling, width=width):
                        stage = self.stage(width)
                        stage.place_configure(height=height)
                        self.root.update_idletasks()
                        stage.show({"state": "idle"})
                        self.assertEqual(stage.itemcget(stage._state, "font"), str(stage._state_font))
                        left, top, right, bottom = stage.bbox(stage._state)
                        self.assertGreaterEqual(top, stage.bbox(stage._artist)[3] + 9)
                        self.assertGreaterEqual(bottom - top, stage._state_font.metrics("linespace"))
                        for item in (stage._elapsed, stage._total):
                            self.assertEqual(stage.itemcget(item, "font"), str(stage._time_font))
                            time_box = stage.bbox(item)
                            self.assertGreaterEqual(time_box[1], bottom + 12)
                            self.assertGreaterEqual(time_box[3] - time_box[1], stage._time_font.metrics("linespace"))
                            self.assertLess(time_box[3], stage._bar_span()[2])
                        self.assertLessEqual(right, width)
                        stage.destroy()
        finally:
            self.root.tk.call("tk", "scaling", original)

    def test_preview_open_button_never_opens_file_picker_or_starts_audio(self):
        content = tk.Frame(self.root)
        content.place(x=0, y=0, width=900, height=1200)
        player = Mock()
        player._path = None
        player.snapshot.return_value = {"state": "idle", "volume": 70}
        app = SimpleNamespace(root=self.root, content=content, preview=True, local_music=lambda: player,
                              current_view="music", _header=Mock())
        with patch.object(music, "load_playlist", return_value=[]), patch.object(music.filedialog, "askopenfilename") as picker:
            music.build_music(app)
            self.root.update_idletasks()
            controls = next(child for child in content.winfo_children() if isinstance(child, music.MusicControls))
            for button in controls.transport.winfo_children():
                if button.cget("text") in {"Открыть файл", "Играть"}:
                    button.invoke()
            picker.assert_not_called()
            player.play_local.assert_not_called()

    def test_compact_title_and_transport_fit_then_return_to_wide(self):
        stage = self.stage(560)
        stage.place_configure(height=460)
        self.root.update_idletasks()
        stage.show({"state": "paused", "title": "Очень длинное название " * 20, "artist": "Исполнитель " * 20, "duration_sec": 90})
        for item in (stage._title, stage._artist, stage._state):
            left, top, right, bottom = stage.bbox(item)
            self.assertGreaterEqual(left, 0)
            self.assertLessEqual(right, 560)
            self.assertLess(bottom, stage._bar_span()[2])
        controls = music.MusicControls(self.root)
        controls.place(x=0, y=470, width=560, height=120)
        for title in ("Назад", "Играть", "Далее", "Стоп", "Открыть файл"):
            ModernButton(controls.transport, text=title, padx=8).pack(side="left", padx=(0, 5))
        tk.Label(controls.volume_box, text="Громкость").pack(side="left")
        tk.Canvas(controls.volume_box, width=120, height=22).pack(side="left")
        for width, wide in ((560, False), (1100, True), (560, False)):
            controls.place_configure(width=width)
            self.root.update_idletasks()
            controls._arrange()
            self.root.update_idletasks()
            self.assertEqual(controls._wide, wide)
            self.assertLessEqual(controls.transport.winfo_reqwidth(), width)
            self.assertEqual(int(controls.volume_box.grid_info()["row"]), 0 if wide else 1)
        self.assertEqual(self.root.state(), "withdrawn")

    def test_full_music_page_rebuild_leaves_one_poll_and_closes_without_callbacks(self):
        content = tk.Frame(self.root)
        content.place(x=0, y=0, width=560, height=1200)
        player = Mock()
        player._path = None
        player.snapshot.return_value = {"state": "idle", "volume": 0}
        app = SimpleNamespace(root=self.root, content=content, local_music=lambda: player,
                              current_view="music", _header=Mock())
        with patch.object(music, "load_playlist", return_value=[]):
            music.build_music(app)
            self.root.update_idletasks()
            first = app._music_poll
            for child in content.winfo_children():
                child.destroy()
            music.build_music(app)
            self.root.update_idletasks()
            self.assertNotEqual(app._music_poll, first)
            self.assertNotIn(first, self.root.tk.call("after", "info"))
            stage = next(child for child in content.winfo_children() if isinstance(child, music.MusicStage))
            controls = next(child for child in content.winfo_children() if isinstance(child, music.MusicControls))
            stage._layout()
            controls._arrange(SimpleNamespace(width=560))
            self.assertFalse(controls._wide)
            self.assertLessEqual(controls.transport.winfo_reqwidth(), 560)
            player.play_local.assert_not_called()
            self.assertEqual(self.root.state(), "withdrawn")


if __name__ == "__main__":
    unittest.main()
