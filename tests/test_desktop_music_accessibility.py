"""Bounded catalog and keyboard contracts. Fixtures never produce audio."""
from __future__ import annotations

import sys
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc_client"))
try:
    import desktop_music as music
finally:
    sys.path.pop(0)


class CatalogPaginationTests(unittest.TestCase):
    def request(self, body, *, offset=0, query=""):
        response = Mock(content=b"{}")
        response.status_code = 200
        response.json.return_value = body
        with patch.object(music, "create_http_client") as factory:
            client = factory.return_value.__enter__.return_value
            client.get.return_value = response
            job = music.ServerCatalogJob({"server_url": "https://fixture.invalid", "api_key": "ag_fixture"},
                                          offset=offset, query=query)
            self.assertTrue(job.done.wait(2))
            return job, client.get.call_args

    def test_offset_and_unicode_search_are_forwarded_and_next_page_is_available(self):
        job, request = self.request({"tracks": [{"id": 101, "title": "Fixture"}],
                                     "total": 102, "offset": 100, "has_more": True, "next_offset": 101},
                                    offset=100, query="Тест + альбом")
        self.assertFalse(job.error)
        self.assertEqual(request.kwargs["params"], {"limit": 100, "offset": 100, "q": "Тест + альбом"})
        self.assertEqual((job.total, job.offset, job.next_offset), (102, 100, 101))

    def test_last_page_has_no_next_and_malformed_offsets_cannot_loop(self):
        job, _ = self.request({"tracks": [{"id": 101}], "total": 101, "offset": 100, "has_more": False}, offset=100)
        self.assertIsNone(job.next_offset)
        self.assertFalse(job.error)
        for body in (
                {"tracks": [{"id": 101}], "offset": 0},
                {"tracks": [{"id": 101}], "has_more": True, "next_offset": 100},
                {"tracks": [], "has_more": True, "next_offset": 101},
                {"tracks": [{"id": 101}], "has_more": True, "next_offset": True}):
            with self.subTest(body=body):
                job, _ = self.request(body, offset=100)
                self.assertTrue(job.error)
                self.assertEqual(job.tracks, [])
                self.assertIsNone(job.next_offset)


class MusicKeyboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(str(exc)) from exc
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)

    def tearDown(self):
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        for child in self.root.winfo_children():
            child.destroy()
        self.assertFalse(self.errors, self.errors)

    def test_track_row_supports_tab_enter_space_and_focus_ring(self):
        row = tk.Frame(self.root)
        tk.Label(row, text="Fixture track").pack()
        activate = Mock()
        with patch.object(row, "bind", wraps=row.bind) as bind:
            music._music_row_accessibility(row, activate, SimpleNamespace())
            callbacks = {call.args[0]: call.args[1] for call in bind.call_args_list}
        self.assertEqual(str(row.cget("takefocus")), "1")
        self.assertGreater(int(row.cget("highlightthickness")), 0)
        for key in ("Return", "space"):
            callbacks[f"<KeyPress-{key}>"](None)
            callbacks[f"<KeyPress-{key}>"](None)
            callbacks[f"<KeyRelease-{key}>"](None)
        self.assertEqual(activate.call_count, 2, "Auto-repeat must not double-activate a track")
        callbacks["<KeyPress-space>"](None)
        callbacks["<FocusOut>"](None)
        callbacks["<KeyRelease-space>"](None)
        self.assertEqual(activate.call_count, 2)

    def test_keyboard_seek_is_bounded_and_space_uses_existing_transport(self):
        seek, toggle = Mock(), Mock()
        stage = music.MusicStage(self.root, Mock(), {"seek": seek, "toggle": toggle})
        stage._duration = 20
        stage._position = 18
        for key in ("Right", "Left", "Home", "Left", "End"):
            self.assertTrue(stage.bind(f"<KeyPress-{key}>"))
            stage._seek_key(SimpleNamespace(keysym=key))
        self.assertEqual([call.args[0] for call in seek.call_args_list], [20, 15, 0, 0, 20])
        stage._toggle_key(None)
        toggle.assert_called_once()
        self.assertEqual(str(stage.cget("takefocus")), "1")
        self.assertGreater(int(stage.cget("highlightthickness")), 0)

    def test_volume_keyboard_and_snapshot_sync_do_not_emit_spurious_commands(self):
        command = Mock()
        volume = music.MusicVolume(self.root, command)
        volume.set(98)
        command.assert_not_called()
        for key in ("Right", "Down", "Home", "Left", "End"):
            self.assertTrue(volume.bind(f"<KeyPress-{key}>"))
            volume._key(SimpleNamespace(keysym=key))
        self.assertEqual([call.args[0] for call in command.call_args_list], [100, 95, 0, 0, 100])
        self.assertEqual(str(volume.cget("takefocus")), "1")
        self.assertGreater(int(volume.cget("highlightthickness")), 0)

    def test_transport_wraps_actual_buttons_at_200_percent_without_clipping(self):
        previous_scaling = float(self.root.tk.call("tk", "scaling"))
        self.root.tk.call("tk", "scaling", 2.667)
        try:
            controls = music.MusicControls(self.root)
            controls.place(x=0, y=0, width=520, height=280)
            for title, icon in (("", "previous"), ("Играть", "play"), ("", "next"),
                                ("", "stop"), ("Добавить музыку", "folder")):
                music.ModernButton(controls.transport, text=title, icon=icon, padx=16,
                                   font=("Segoe UI Semibold", 11)).pack(side="left")
            tk.Label(controls.volume_box, text="Громкость").pack(side="left")
            music.MusicVolume(controls.volume_box, Mock()).pack(side="left")
            for _ in range(4):
                self.root.update_idletasks()
                controls._arrange()
            for button in controls.transport.winfo_children():
                self.assertEqual(button.winfo_width(), button.winfo_reqwidth())
                self.assertLessEqual(button.winfo_x() + button.winfo_width(), 520)
                self.assertLessEqual(button.winfo_y() + button.winfo_height(), controls.transport.winfo_height())
            self.assertGreater(controls.transport.winfo_children()[-1].winfo_y(), 0)
        finally:
            self.root.tk.call("tk", "scaling", previous_scaling)

    def test_ui_next_page_and_search_reach_tracks_beyond_first_hundred(self):
        calls = []
        class FixtureCatalog:
            def __init__(self, config, *, offset=0, query=""):
                calls.append((offset, query))
                self.offset, self.query = offset, query
                self.error = ""
                self.total = 1 if query else 101
                ids = [101] if query or offset else list(range(1, 101))
                self.tracks = [{"id": id, "title": f"Fixture {id}", "artist": "Fixture", "album": "",
                                "duration": 60, "favorite": False} for id in ids]
                self.next_offset = 100 if not query and offset == 0 else None
                self.done = threading.Event()
                self.done.set()

        player = Mock()
        player._path = None
        player.snapshot.return_value = {"state": "idle", "volume": 70}
        player.presentation.return_value = {}
        content = tk.Frame(self.root)
        content.place(x=0, y=0, width=900, height=1000)
        app = SimpleNamespace(root=self.root, content=content, local_music=lambda: player,
                              current_view="music", _header=Mock(), preview=True)
        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)
        def wait_for(predicate):
            deadline = time.monotonic() + 4
            while not predicate() and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            self.assertTrue(predicate())
        with patch.object(music, "ServerCatalogJob", FixtureCatalog), \
                patch.object(music, "_cast_status", return_value={}), \
                patch.object(music, "load_playlist", return_value=[]):
            music.build_music(app)
            next_page = next(w for w in walk(content) if isinstance(w, music.ModernButton) and w.cget("text") == "Далее")
            wait_for(lambda: next_page.cget("state") == "normal")
            self.assertEqual(len([w for w in walk(content) if hasattr(w, "_music_row_key")]), 100)
            next_page.invoke()
            wait_for(lambda: any(getattr(w, "_music_row_key", None) == ("server", 101) for w in walk(content)))
            self.assertEqual(next_page.cget("state"), "disabled")
            self.assertEqual(len([w for w in walk(content) if hasattr(w, "_music_row_key")]), 1,
                             "Pagination replaces the page instead of growing an unbounded widget tree")
            entry = next(w for w in walk(content) if isinstance(w, tk.Entry))
            entry.insert(0, "Fixture 101")
            search = next(w for w in walk(content) if isinstance(w, music.ModernButton) and w.cget("text") == "Найти")
            search.invoke()
            wait_for(lambda: search.cget("state") == "normal")
            self.assertEqual(calls, [(0, ""), (100, ""), (0, "Fixture 101")])
            app._music_cast_pump.close(wait=True)
        player.command.assert_not_called()
        player.play_local.assert_not_called()


if __name__ == "__main__":
    unittest.main()
