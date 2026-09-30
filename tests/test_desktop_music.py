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
    def test_server_jobs_never_send_agent_key_to_remote_plaintext(self):
        config = {"server_url": "http://music.example.invalid:8001", "api_key": "ag_fixture"}
        with patch.object(music, "create_http_client") as client:
            catalog = music.ServerCatalogJob(config)
            play = music.ServerPlayJob(config, 1, volume=50)
            self.assertTrue(catalog.done.wait(2)); self.assertTrue(play.done.wait(2))
        client.assert_not_called()
        self.assertTrue(catalog.error); self.assertTrue(play.error)

    def test_phone_cover_is_digest_bound_and_reused_without_disk_reads(self):
        picture = b"\xff\xd8\xff" + b"cover-fixture"
        digest = music.hashlib.sha256(picture).hexdigest()
        music._PHONE_COVER_SHA, music._PHONE_COVER_DATA = "", b""
        bridge = SimpleNamespace(read_cover=Mock(return_value=picture))
        with patch.dict(sys.modules, {"music_bridge": bridge}):
            self.assertEqual(music._phone_cover(digest), picture)
            self.assertEqual(music._phone_cover(digest), picture)
            bridge.read_cover.assert_called_once_with(expected_sha256=digest)
        bridge = SimpleNamespace(read_cover=Mock(return_value=b"\xff\xd8\xffwrong"))
        with patch.dict(sys.modules, {"music_bridge": bridge}):
            self.assertEqual(music._phone_cover("0" * 64), b"")
        self.assertEqual(music._phone_cover("invalid"), b"")

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

    def test_cancelled_open_job_stops_the_late_player_without_saving_history(self):
        started, release = threading.Event(), threading.Event()
        player = Mock()

        def play(_path):
            started.set()
            release.wait(2)
            return {"state": "playing"}

        player.play_local.side_effect = play
        player.command.return_value = {"state": "stopped"}
        with patch.object(music, "remember_track") as remember:
            job = music.LocalOpenJob(player, Path("fixture.wav"))
            self.assertTrue(started.wait(1))
            job.cancel()
            release.set()
            self.assertTrue(job.done.wait(2))
        player.command.assert_called_once_with("music_stop", {}, {})
        remember.assert_not_called()
        self.assertEqual(job.snapshot["state"], "stopped")

    def test_pending_open_snapshot_never_waits_for_the_player_lock(self):
        opening = SimpleNamespace(done=threading.Event())
        player = Mock()
        player.snapshot.side_effect = AssertionError("Tk touched the player while it was opening")
        previous = {"state": "paused", "title": "Previous", "volume": 31}
        shown = music._local_snapshot(player, opening, previous)
        player.snapshot.assert_not_called()
        self.assertEqual((shown["state"], shown["state_label"], shown["volume"]),
                         ("loading", "Открываю трек…", 31))


class CastCommandPumpTests(unittest.TestCase):
    def test_volume_drag_runs_off_caller_and_keeps_only_latest_waiting_value(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        worker_ids = []

        def sender(command, payload):
            calls.append((command, payload))
            worker_ids.append(threading.get_ident())
            if len(calls) == 1:
                started.set()
                release.wait(2)
            return {"player": {"state": "playing", "volume": payload["volume"]}}

        pump = music.CastCommandPump(sender)
        try:
            first = pump.submit("music_volume", {"volume": 1})
            self.assertTrue(started.wait(1))
            for value in range(2, 101):
                pump.submit("music_volume", {"volume": value})
            self.assertEqual(len(calls), 1)
            release.set()
            self.assertTrue(pump.wait_idle())
            self.assertEqual([row[1]["volume"] for row in calls], [1, 100])
            self.assertTrue(all(ident != threading.get_ident() for ident in worker_ids))
            results = pump.drain()
            self.assertEqual(results[0][0], first)
            self.assertEqual(results[-1][2]["volume"], 100)
            self.assertFalse(pump.busy())
        finally:
            release.set()
            pump.close(wait=True)

    def test_optimistic_state_keeps_remote_controls_from_snapping_back(self):
        status = {"state": "playing", "volume": 20, "position_sec": 3, "error": ""}
        status = music._optimistic_cast(status, "music_volume", {"volume": 87})
        status = music._optimistic_cast(status, "music_seek", {"position_sec": 45})
        status = music._optimistic_cast(status, "music_pause")
        self.assertEqual(status["volume"], 87)
        self.assertEqual(status["position_sec"], 45)
        self.assertEqual(status["state"], "paused")
        self.assertEqual(music._optimistic_cast(status, "music_resume")["state"], "playing")
        stopped = music._optimistic_cast({**status, "error": "failed"}, "music_stop")
        self.assertEqual((stopped["state"], stopped["error"]), ("stopping", ""))
        self.assertTrue(music._cast_live({**stopped, "track_id": 4}))


class MusicRenderTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.cast = patch.object(music, "_cast_status", return_value={})
        self.cast.start()
        self.addCleanup(self.cast.stop)
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

    def test_position_only_resize_does_not_rebuild_and_cover_frame_is_cached(self):
        stage = self.stage()
        if stage._resize_job:
            stage.after_cancel(stage._resize_job)
            stage._resize_job = None
        with patch.object(stage, "after") as after:
            stage._resized(SimpleNamespace(widget=stage, width=900, height=300, y=-40))
            after.assert_not_called()
        stage._paint_cover()
        self.assertEqual(len(stage._cover_photos), 1)
        photo = stage._photo
        with patch.object(music.ImageTk, "PhotoImage") as created:
            stage._paint_cover()
            created.assert_not_called()
        self.assertIs(stage._photo, photo)
        self.assertTrue(all(image.size == (184, 184) for image in stage._covers.values()))

    def test_all_text_rows_have_measured_gaps_at_windows_dpi_scales(self):
        original = float(self.root.tk.call("tk", "scaling"))
        try:
            for scaling in (1.067, 1.333, 1.667, 2.0, 2.667):
                self.root.tk.call("tk", "scaling", scaling)
                for width in (1184, 560):
                    with self.subTest(scaling=scaling, width=width):
                        stage = self.stage(width)
                        height = stage._target_height(width)
                        stage.place_configure(height=height)
                        self.root.update_idletasks()
                        stage._layout()
                        stage.show({"state": "idle"})
                        self.assertEqual(int(stage["height"]), height)
                        title_box, artist_box = stage.bbox(stage._title), stage.bbox(stage._artist)
                        self.assertGreaterEqual(title_box[1], 0 if width >= 720 else 224)
                        self.assertGreaterEqual(artist_box[1], title_box[3] + 8)
                        for item in (stage._title, stage._artist):
                            box = stage.bbox(item)
                            self.assertGreaterEqual(box[0], 0)
                            self.assertLessEqual(box[2], width)
                        self.assertEqual(stage.itemcget(stage._state, "font"), str(stage._state_font))
                        left, top, right, bottom = stage.bbox(stage._state)
                        self.assertGreaterEqual(top, artist_box[3] + 10)
                        self.assertGreaterEqual(bottom - top, stage._state_font.metrics("linespace"))
                        for item in (stage._elapsed, stage._total):
                            self.assertEqual(stage.itemcget(item, "font"), str(stage._time_font))
                            time_box = stage.bbox(item)
                            self.assertGreaterEqual(time_box[1], bottom + 12)
                            self.assertGreaterEqual(time_box[3] - time_box[1], stage._time_font.metrics("linespace"))
                            self.assertLess(time_box[3], stage._bar_span()[2])
                        self.assertLess(stage._bar_span()[2] + 8, height)
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
        with patch.object(music, "load_playlist", return_value=[]), patch.object(music.filedialog, "askopenfilenames") as picker:
            music.build_music(app)
            self.root.update_idletasks()
            controls = next(child for child in content.winfo_children() if isinstance(child, music.MusicControls))
            for button in controls.transport.winfo_children():
                if button.cget("text") in {"Добавить музыку", "Играть"}:
                    button.invoke()
            picker.assert_not_called()
            player.play_local.assert_not_called()

    def test_music_page_does_not_touch_player_lock_while_open_job_is_pending(self):
        content = tk.Frame(self.root)
        content.place(x=0, y=0, width=900, height=1200)
        player = Mock()
        player._path = None
        player.snapshot.side_effect = AssertionError("blocking snapshot")
        player.presentation.side_effect = AssertionError("blocking presentation")
        opening = SimpleNamespace(done=threading.Event(), cancel=Mock())
        app = SimpleNamespace(root=self.root, content=content, preview=False, local_music=lambda: player,
                              current_view="music", _header=Mock(), _local_music_job=opening)
        with patch.object(music, "load_playlist", return_value=[]):
            music.build_music(app)
            self.root.update_idletasks()
        player.snapshot.assert_not_called()
        player.presentation.assert_not_called()
        stage = next(child for child in content.winfo_children() if isinstance(child, music.MusicStage))
        self.assertEqual((stage.play_button.cget("text"), stage.play_button.cget("state")),
                         ("Открытие…", "disabled"))
        controls = next(child for child in content.winfo_children() if isinstance(child, music.MusicControls))
        local_only = [button for button in controls.transport.winfo_children()
                      if isinstance(button, music.ModernButton)
                      and (button.cget("icon") in {"previous", "next"} or button.cget("text") == "Добавить музыку")]
        self.assertTrue(local_only)
        self.assertTrue(all(button.cget("state") == "disabled" for button in local_only))

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

    def jpeg(self):
        import io
        buffer = io.BytesIO()
        music.Image.new("RGB", (12, 8), "#829cff").save(buffer, format="JPEG")
        return buffer.getvalue()

    def walk(self, widget):
        yield widget
        for child in widget.winfo_children():
            yield from self.walk(child)

    def test_cover_queue_and_lyrics_share_one_screen(self):
        content = tk.Frame(self.root)
        content.place(x=0, y=0, width=1100, height=1400)
        picture = self.jpeg()
        player = Mock()
        player._path = None
        player.snapshot.return_value = {
            "state": "paused", "title": "Ночь", "artist": "red!", "volume": 40,
            "duration_sec": 90, "position_sec": 12,
        }
        player.presentation.return_value = {"lyrics": "[00:10.00]Первая строка\n[00:20.00]Вторая строка", "artwork": picture}
        app = SimpleNamespace(root=self.root, content=content, local_music=lambda: player,
                              current_view="music", _header=Mock())
        music.build_music(app)
        self.root.update_idletasks()
        stage = next(child for child in content.winfo_children() if isinstance(child, music.MusicStage))
        stage._layout()
        self.root.update_idletasks()
        stage._layout()
        details = next(child for child in content.winfo_children() if isinstance(child, music.MusicDetails))
        pane = next(widget for widget in self.walk(content) if isinstance(widget, music.LyricsPane))
        titles = [widget.cget("text") for widget in self.walk(content) if isinstance(widget, tk.Label)]
        self.assertIn("Моя музыка", titles)
        self.assertIn("Текст", titles)
        self.assertEqual(pane.lyrics, "[00:10.00]Первая строка\n[00:20.00]Вторая строка")
        self.assertEqual(pane._active, 0)
        self.assertEqual(stage.itemcget(stage._title, "text"), "Ночь")
        self.assertTrue(stage._art_key)
        self.assertEqual(next(iter(stage._covers.values())).size, (184, 184))
        for width, wide in ((1100, True), (560, False)):
            details._arrange(SimpleNamespace(width=width))
            self.assertEqual(details._wide, wide)
            self.assertEqual(int(details.lyrics_card.grid_info()["row"]), 0 if wide else 1)
        photo = stage._photo
        with patch.object(music.ImageTk, "PhotoImage") as created:
            for position in range(13, 19):
                stage.show({**player.snapshot(), "position_sec": position, "artwork": picture})
            created.assert_not_called()
        self.assertIs(stage._photo, photo)
        self.assertEqual(self.root.state(), "withdrawn")

    def test_phone_track_uses_the_same_cover_controls_queue_and_lyrics(self):
        content = tk.Frame(self.root)
        content.place(x=0, y=0, width=1100, height=1400)
        picture = self.jpeg()
        player = Mock()
        player._path = None
        player.snapshot.return_value = {"state": "idle", "volume": 70}
        player.presentation.return_value = {}
        status = {
            "state": "playing", "track_id": 4, "title": "Звонок", "artist": "Телефон",
            "position_sec": 3, "duration_sec": 40, "volume": 20, "error": "",
            "lyrics": "Куплет с телефона",
        }
        app = SimpleNamespace(root=self.root, content=content, local_music=lambda: player,
                              current_view="music", _header=Mock())
        with patch.object(music, "_cast_status", return_value=status), patch.object(music, "_phone_cover", return_value=picture):
            with patch.object(music, "LocalOpenJob") as local_job:
                music.build_music(app)
                stage = next(child for child in content.winfo_children() if isinstance(child, music.MusicStage))
                stage.actions["previous"]()
                stage.actions["next"]()
                local_job.assert_not_called()
            self.root.update_idletasks()
            stage._layout()
            self.root.update_idletasks()
            stage._layout()
            pane = next(widget for widget in self.walk(content) if isinstance(widget, music.LyricsPane))
            controls = next(child for child in content.winfo_children() if isinstance(child, music.MusicControls))
            labels = [widget.cget("text") for widget in self.walk(content) if isinstance(widget, tk.Label)]
            self.assertEqual(stage.itemcget(stage._title, "text"), "Звонок")
            self.assertIn("плеер xass", stage.itemcget(stage._state, "text").lower())
            self.assertEqual(pane.lyrics, "Куплет с телефона")
            self.assertIn("Телефон", labels)
            self.assertTrue(stage._art_key)
            self.assertIn("Пауза", [button.cget("text") for button in controls.transport.winfo_children()])
            self.assertNotIn("Дальше", [button.cget("text") for button in self.walk(content) if isinstance(button, music.ModernButton)])
            local_only = [button for button in self.walk(content) if isinstance(button, music.ModernButton)
                          and (button.cget("icon") in {"previous", "next"} or button.cget("text") == "Добавить музыку")]
            self.assertTrue(local_only)
            self.assertTrue(all(button.cget("state") == "disabled" for button in local_only))
            player.play_local.assert_not_called()

    def test_live_cast_silences_and_replaces_an_already_playing_local_track(self):
        content = tk.Frame(self.root)
        content.place(x=0, y=0, width=900, height=1200)
        player = Mock()
        player._path = Path("local.wav")
        player.snapshot.return_value = {
            "state": "playing", "title": "Local", "artist": "PC", "volume": 70,
            "duration_sec": 120, "position_sec": 10,
        }
        player.presentation.return_value = {}
        player.command.return_value = {"state": "stopped"}
        status = {
            "state": "playing", "track_id": 9, "title": "Remote", "artist": "Phone",
            "position_sec": 4, "duration_sec": 80, "volume": 25, "error": "",
        }
        app = SimpleNamespace(root=self.root, content=content, local_music=lambda: player,
                              current_view="music", _header=Mock())
        with patch.object(music, "_cast_status", return_value=status), \
                patch.object(music, "load_playlist", return_value=[]):
            music.build_music(app)
            self.assertTrue(app._local_music_silence_job.done.wait(2))
            self.root.update_idletasks()
        player.command.assert_called_once_with("music_stop", {}, {})
        stage = next(child for child in content.winfo_children() if isinstance(child, music.MusicStage))
        stage._layout()
        self.root.update_idletasks()
        stage._layout()
        self.assertEqual(stage.itemcget(stage._title, "text"), "Remote")
        self.assertIn("плеер xass", stage.itemcget(stage._state, "text").lower())
        controls = next(child for child in content.winfo_children() if isinstance(child, music.MusicControls))
        local_only = [button for button in controls.transport.winfo_children()
                      if isinstance(button, music.ModernButton)
                      and (button.cget("icon") in {"previous", "next"} or button.cget("text") == "Добавить музыку")]
        self.assertTrue(local_only)
        self.assertTrue(all(button.cget("state") == "disabled" for button in local_only))

    def test_phone_loading_disables_play_and_error_button_really_clears_it(self):
        content = tk.Frame(self.root)
        content.place(x=0, y=0, width=900, height=1200)
        player = Mock()
        player._path = None
        player.snapshot.return_value = {"state": "idle", "volume": 70}
        player.presentation.return_value = {}
        app = SimpleNamespace(root=self.root, content=content, local_music=lambda: player,
                              current_view="music", _header=Mock())
        status = {
            "state": "loading", "track_id": 8, "title": "Track", "artist": "Artist",
            "position_sec": 0, "duration_sec": 0, "volume": 50, "error": "",
        }
        sender = Mock(return_value={"ok": True, "player": {**status, "state": "stopped"}})
        with patch.object(music, "_cast_status", side_effect=lambda: dict(status)), \
                patch.object(music, "_send_cast", sender), patch.object(music, "load_playlist", return_value=[]):
            music.build_music(app)
            stage = next(child for child in content.winfo_children() if isinstance(child, music.MusicStage))
            self.assertEqual(stage.play_button.cget("text"), "Загрузка…")
            self.assertEqual(stage.play_button.cget("state"), "disabled")
            stage.play_button.invoke()
            sender.assert_not_called()

            for child in content.winfo_children():
                child.destroy()
            status.update(state="error", error="Download failed")
            music.build_music(app)
            stage = next(child for child in content.winfo_children() if isinstance(child, music.MusicStage))
            self.assertEqual(stage.play_button.cget("text"), "Сбросить")
            self.assertEqual(stage.play_button.cget("state"), "normal")
            stage.play_button.invoke()
            self.assertTrue(app._music_cast_pump.wait_idle())
            sender.assert_called_once_with("music_stop", {})
            app._music_cast_pump.close(wait=True)


if __name__ == "__main__":
    unittest.main()
