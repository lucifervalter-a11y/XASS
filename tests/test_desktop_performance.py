"""Work-count regressions for Tk responsiveness; no agent or visible window."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc_client"))
try:
    import desktop_app
    import desktop_home
    import desktop_widgets as widgets
finally:
    sys.path.pop(0)


class RenderWorkTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.addCleanup(self.finish)

    def finish(self):
        self.root.update_idletasks()
        for callback in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", callback)
        self.root.destroy()
        self.assertFalse(self.errors, self.errors)

    def test_large_cards_only_supersample_small_cached_corners(self):
        widgets._rounded_corners.cache_clear()
        resize = widgets.Image.Image.resize
        work = []

        def record(image, size, *args, **kwargs):
            work.append((image.size, size))
            return resize(image, size, *args, **kwargs)

        with patch.object(widgets.Image.Image, "resize", record):
            for width in (1000, 1100, 1200):
                image = widgets.rounded_image(width, 600, fill=widgets.CARD, border_color=widgets.LINE)
                self.assertEqual(image.size, (width, 600))
                self.assertEqual(image.getpixel((width // 2, 300)), (43, 43, 47, 255))
        self.assertEqual(widgets._rounded_corners.cache_info().misses, 1)
        self.assertEqual(widgets._rounded_corners.cache_info().hits, 2)
        # Pillow can recursively resize premultiplied alpha; neither operation
        # is allowed to scale with the surface area of the full card.
        self.assertTrue(work)
        self.assertTrue(all(max(source) <= 100 for source, _ in work))

    def test_button_reuses_font_background_and_icon_across_hover_cycles(self):
        button = widgets.ModernButton(self.root, "Подключение", icon="link", bg=widgets.CARD)
        button.place(x=0, y=0, width=240, height=48)
        self.root.update_idletasks()
        for hovered in (True, False):
            button._set_hover(hovered)
            self.root.update_idletasks()
        with patch.object(widgets.ImageTk, "PhotoImage", wraps=widgets.ImageTk.PhotoImage) as photo, \
                patch.object(widgets.tkfont, "Font", wraps=widgets.tkfont.Font) as font:
            for _ in range(8):
                button.configure(text="Подключение", state="normal", bg=widgets.CARD)
                button._set_hover(True)
                self.root.update_idletasks()
                button._set_hover(False)
                self.root.update_idletasks()
            photo.assert_not_called()
            font.assert_not_called()
        self.assertGreaterEqual(len(button._icon_cache), 1)

    def test_card_position_only_configure_does_not_queue_repaint(self):
        panel = widgets.RoundedPanel(self.root)
        panel.place(x=0, y=0, width=420, height=180)
        self.root.update_idletasks()
        panel._paint()
        self.assertIsNotNone(panel._last_paint)
        with patch.object(panel, "after_idle") as after:
            panel._schedule_paint(SimpleNamespace(width=420, height=180, x=0, y=-50))
            after.assert_not_called()

    def test_hero_name_and_scroll_reuse_artwork_but_resize_invalidates_it(self):
        app = SimpleNamespace(name_var=tk.StringVar(self.root, value="My PC"),
                              open_miniapp=Mock(), show_view=Mock())
        hero = desktop_home.HomeHero(self.root, app)
        hero.place(x=0, y=0, width=900, height=340)
        self.root.update_idletasks()
        if hero._pending:
            hero.after_cancel(hero._pending)
        hero._draw()
        photo = hero._photo
        with patch.object(hero, "_render_backdrop", wraps=hero._render_backdrop) as backdrop:
            with patch.object(hero, "after") as after:
                hero._schedule_draw(SimpleNamespace(width=900, height=340, y=-20))
                after.assert_not_called()
            app.name_var.set("Renamed computer")
            hero._draw()
            self.assertIs(hero._photo, photo)
            self.assertTrue(hero.itemcget(hero._heading, "text").endswith("red!"))
            self.assertIn("Renamed computer", hero.itemcget(hero._description, "text"))
            backdrop.assert_not_called()
            hero.place_configure(width=800)
            self.root.update_idletasks()
            hero._draw()
            backdrop.assert_called_once_with(800, 340)

    def test_label_wrapping_has_one_binding_and_skips_unchanged_geometry(self):
        parent = tk.Frame(self.root, padx=12)
        label = tk.Label(parent, text="A long caption", wraplength="2i")
        label.pack()
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        with patch.object(parent, "bind") as bind:
            app._wrap_labels(parent)
            app._wrap_labels(parent)
            bind.assert_called_once()
            resize = bind.call_args.args[1]
        with patch.object(label, "configure", wraps=label.configure) as configure:
            resize(SimpleNamespace(width=500))
            for _ in range(30):
                resize(SimpleNamespace(width=500))
            configure.assert_called_once_with(wraplength=468, justify="left")
            resize(SimpleNamespace(width=400))
            self.assertEqual(configure.call_count, 2)

    def test_files_shell_is_ready_while_slow_directory_listing_runs_off_tk(self):
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app.root = self.root
        app.content = tk.Frame(self.root, bg=desktop_app.BG)
        app.content.pack(fill="both", expand=True)
        app.preview = False
        app._closing = False
        app.current_view = "files"
        app._view_revision = 7
        app._file_root = "desktop"
        app._header = lambda title, subtitle, kicker="": tk.Label(
            app.content, text=title, bg=desktop_app.BG,
        ).pack(fill="x")
        app._button = lambda parent, text, command, kind="secondary": tk.Button(
            parent, text=text, command=command,
        )
        app._flow_actions = lambda _parent: None
        app._card = lambda parent, padding=0: tk.Frame(parent, bg=desktop_app.CARD, padx=padding, pady=padding)
        started, release = threading.Event(), threading.Event()
        worker_ids = []

        def slow_list(*_args):
            worker_ids.append(threading.get_ident())
            started.set()
            release.wait(2)
            return {"entries": [{"name": "ready.mp3", "type": "file", "size": 12}]}

        try:
            with patch.object(desktop_app, "list_files", side_effect=slow_list):
                app._build_files()
                self.assertTrue(started.wait(1))
                self.root.update_idletasks()
                self.assertNotEqual(worker_ids, [threading.get_ident()])
                self.assertIn("Загружаю файлы…", self._label_texts(app.content))
                release.set()
                deadline = time.monotonic() + 2
                while "ready.mp3" not in self._label_texts(app.content) and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(0.01)
                self.assertIn("ready.mp3", self._label_texts(app.content))
        finally:
            release.set()

    def test_archive_shell_is_ready_while_status_and_sqlite_run_off_tk(self):
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app.root = self.root
        app.content = tk.Frame(self.root, bg=desktop_app.BG)
        app.content.pack(fill="both", expand=True)
        app.config = {"archive_folder": str(Path(tempfile.gettempdir()) / "xass-archive-perf")}
        app.current_view = "archive"
        app._view_revision = 9
        app._closing = False
        app._archive_moving = False
        app._archive_cancel = threading.Event()
        app._archive_poll = None
        app._archive_render_after = None
        app.archive_state_var = tk.StringVar(self.root)
        app._header = lambda title, subtitle, kicker="": tk.Label(app.content, text=title).pack()
        app._card = lambda parent, padding=0: tk.Frame(parent, bg=desktop_app.CARD, padx=padding, pady=padding)
        app._button = lambda parent, text, command, kind="secondary": tk.Button(parent, text=text, command=command)
        app._flow_actions = lambda _parent: None
        started, release = threading.Event(), threading.Event()
        worker_ids = []

        def slow_status(_config):
            worker_ids.append(threading.get_ident())
            started.set()
            release.wait(2)
            return {"folder": "archive", "cursor": 1, "database_size": 0, "free_bytes": 0}

        try:
            with patch.object(desktop_app, "archive_status", side_effect=slow_status), \
                    patch.object(desktop_app, "conversation_rows", return_value=[]):
                app._build_archive()
                self.assertTrue(started.wait(1))
                self.assertNotEqual(worker_ids, [threading.get_ident()])
                self.root.update_idletasks()
                self.assertIn("Загрузка", app.archive_state_var.get())
                app.current_view = "settings"
                app._view_revision += 1
                release.set()
                self.assertTrue(app._archive_load_job.done.wait(2))
                self.root.update()
                self.assertIn("Загрузка", app.archive_state_var.get())
        finally:
            release.set()

    def test_archive_rows_render_in_bounded_batches(self):
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app.root = self.root
        app.current_view = "archive"
        app._view_revision = 3
        app._closing = False
        app._archive_render_after = None
        app.archive_state_var = tk.StringVar(self.root)
        app._card = lambda parent, padding=0: tk.Frame(parent, bg=desktop_app.CARD, padx=padding, pady=padding)
        app._style_scrolled_text = lambda _widget: None
        body = tk.Frame(self.root, bg=desktop_app.BG)
        body.pack()
        rows = [{"id": index, "chat_id": 1, "text_content": f"row-{index}"} for index in range(100)]
        status = {"folder": "archive", "cursor": 100, "database_size": 0, "free_bytes": 0}

        app._render_archive_snapshot(body, 3, status, rows, "")
        initial = app.archive_messages.text.get("1.0", "end")
        self.assertEqual(initial.count("row-"), 32)
        deadline = time.monotonic() + 2
        while app.archive_messages.text.get("1.0", "end").count("row-") < 100 and time.monotonic() < deadline:
            self.root.update()
        self.assertEqual(app.archive_messages.text.get("1.0", "end").count("row-"), 100)

    def test_archive_cleanup_runs_off_tk_and_rejects_duplicate_start(self):
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app.root = self.root
        app.config = {}
        app.current_view = "archive"
        app._view_revision = 4
        app._closing = False
        app._archive_cleanup_running = False
        app._archive_cleanup_generation = 0
        app._archive_cleanup_poll = None
        app.archive_state_var = tk.StringVar(self.root)
        app._log = Mock()
        app.show_view = Mock()
        started, release = threading.Event(), threading.Event()
        worker_ids = []

        def slow_cleanup(_config, *, force=False):
            worker_ids.append(threading.get_ident())
            started.set()
            release.wait(2)
            return {"removed_files": 2, "freed_bytes": 30}

        try:
            with patch.object(desktop_app.messagebox, "askyesno", return_value=True) as confirm, \
                    patch.object(desktop_app, "cleanup_archive", side_effect=slow_cleanup):
                app.cleanup_archive_files()
                self.assertTrue(started.wait(1))
                app.cleanup_archive_files()
                self.assertEqual(confirm.call_count, 1)
                self.assertNotEqual(worker_ids, [threading.get_ident()])
                release.set()
                self.assertTrue(app._archive_cleanup_job.done.wait(2))
                deadline = time.monotonic() + 2
                while app._archive_cleanup_running and time.monotonic() < deadline:
                    self.root.update()
                self.assertFalse(app._archive_cleanup_running)
                app._log.assert_called_once()
                app.show_view.assert_called_once_with("archive")
        finally:
            release.set()

    def test_log_drain_is_bounded_and_home_refresh_is_coalesced(self):
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app.root = self.root
        app.preview = True
        app._closing = False
        app.current_view = "overview"
        app.log_queue = queue.Queue()
        app.history = []
        app._home_events_refresh_after = None
        app._render_home_events = Mock()
        for index in range(200):
            app.log_queue.put(f"line-{index}")

        app._drain_logs()

        self.assertEqual(len(app.history), desktop_app.LOG_DRAIN_BATCH)
        self.assertEqual(app.log_queue.qsize(), 200 - desktop_app.LOG_DRAIN_BATCH)
        self.root.update_idletasks()
        app._render_home_events.assert_called_once()

    def test_server_theme_alias_does_not_trigger_full_rebuild(self):
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app.sync_server_theme_var = tk.BooleanVar(self.root, value=True)
        app.theme_var = tk.StringVar(self.root, value=desktop_app.THEME_CYBER)
        app.set_theme = Mock()

        app._sync_server_theme({"server_theme": "cyberpunk"})
        app.set_theme.assert_not_called()
        app._sync_server_theme({"server_theme": "not-a-theme"})
        app.set_theme.assert_not_called()
        app._sync_server_theme({"server_theme": "matrix"})
        app.set_theme.assert_called_once_with(desktop_app.THEME_MATRIX, save=True)

    @staticmethod
    def _label_texts(widget):
        rows = []
        if isinstance(widget, tk.Label):
            rows.append(str(widget.cget("text") or ""))
        for child in widget.winfo_children():
            rows.extend(RenderWorkTests._label_texts(child))
        return rows


class SystemSamplingTests(unittest.TestCase):
    def make_app(self):
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app._closing = app._hidden_to_tray = app._metrics_sampling = False
        app._metrics_results = queue.Queue(maxsize=1)
        app.root = Mock()
        return app

    def test_log_persistence_and_shutdown_never_wait_on_slow_disk(self):
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app._closing = False
        app._log_persist_queue = queue.Queue(maxsize=4)
        entered, release = threading.Event(), threading.Event()
        writer_ids = []

        def slow_append(_line):
            writer_ids.append(threading.get_ident())
            entered.set()
            release.wait(2)

        with patch.object(desktop_app, "append_log", side_effect=slow_append):
            writer = threading.Thread(target=app._persist_logs, daemon=True)
            app._log_persist_thread = writer
            writer.start()
            app._queue_persisted_log("line")
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            app._closing = True
            app._stop_log_persistence()
            app._stop_log_persistence()
            self.assertLess(time.monotonic() - started, 0.1)
            release.set()
            writer.join(timeout=1)

        self.assertFalse(writer.is_alive())
        self.assertNotEqual(writer_ids, [threading.get_ident()])

    def test_metrics_are_collected_off_tk_thread_and_never_overlap(self):
        app = self.make_app()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        worker_ids = []

        def collect():
            worker_ids.append(threading.get_ident())
            started.set()
            try:
                release.wait(2)
                return {}
            finally:
                finished.set()

        app._collect_local_metrics = Mock(side_effect=collect)
        try:
            app._refresh_local_metrics()
            self.assertTrue(started.wait(1))
            self.assertNotEqual(worker_ids, [threading.get_ident()])
            app._refresh_local_metrics()
            app._collect_local_metrics.assert_called_once()
            self.assertTrue(app._metrics_sampling)
        finally:
            release.set()
            self.assertTrue(finished.wait(2))

    def test_failed_worker_releases_sampling_state_and_hidden_window_does_no_io(self):
        app = self.make_app()
        app._collect_local_metrics = Mock(side_effect=RuntimeError("unavailable"))
        app._refresh_local_metrics()
        result = app._metrics_results.get(timeout=2)
        self.assertEqual(result, {})
        app._metrics_results.put(result)
        app._apply_local_metrics()
        self.assertFalse(app._metrics_sampling)
        app._hidden_to_tray = True
        app._refresh_local_metrics()
        app._collect_local_metrics.assert_called_once()

    def test_metrics_use_one_snapshot_per_resource_and_a_real_cpu_interval(self):
        with patch.object(desktop_app.psutil, "virtual_memory") as memory, \
                patch.object(desktop_app.psutil, "disk_usage") as disk, \
                patch.object(desktop_app.psutil, "cpu_percent", return_value=24) as cpu, \
                patch.object(desktop_app.psutil, "boot_time", return_value=123):
            result = desktop_app.XassDesktop._collect_local_metrics()
        memory.assert_called_once()
        disk.assert_called_once()
        cpu.assert_called_once_with(interval=0.1)
        self.assertEqual(result["cpu"], 24)

    def test_process_sample_uses_bounded_isolated_helper_for_source_and_frozen(self):
        app = self.make_app()
        outputs = []

        def run(command, **options):
            self.assertIn("--desktop-process-sample", command)
            self.assertNotIn("--agent", command)
            self.assertEqual(options["timeout"], 15)
            self.assertEqual(options["stdout"], subprocess.DEVNULL)
            output = Path(command[-1])
            outputs.append(output)
            output.write_text(json.dumps([{"pid": 42, "name": "editor.exe"}]), encoding="utf-8")
            return SimpleNamespace(returncode=0)

        for frozen in (False, True):
            with self.subTest(frozen=frozen), patch.object(desktop_app.sys, "frozen", frozen, create=True), \
                    patch.object(desktop_app.subprocess, "run", side_effect=run) as launch:
                self.assertEqual(app._top_processes(), [{"pid": 42, "name": "editor.exe"}])
                self.assertEqual(len(launch.call_args.args[0]), 3 if frozen else 4)
        self.assertTrue(all(not output.parent.exists() for output in outputs))

    def test_helper_timeout_returns_empty_and_cleans_up_private_output(self):
        app = self.make_app()
        outputs = []

        def timeout(command, **_):
            outputs.append(Path(command[-1]))
            raise subprocess.TimeoutExpired(command, 15)

        with patch.object(desktop_app.subprocess, "run", side_effect=timeout):
            self.assertEqual(app._top_processes(), [])
        self.assertFalse(outputs[0].parent.exists())

    def test_private_sample_cli_never_constructs_gui_or_touches_config(self):
        with tempfile.TemporaryDirectory(prefix="xass-process-sample-") as folder:
            output = Path(folder) / "processes.json"
            with patch.object(sys, "argv", ["XASS.exe", "--desktop-process-sample", str(output)]), \
                    patch.object(desktop_app.XassDesktop, "_collect_process_rows", return_value=[{"pid": 42}]), \
                    patch.object(desktop_app.tk, "Tk") as root, \
                    patch.object(desktop_app, "load_config") as config:
                desktop_app.main()
                self.assertEqual(json.loads(output.read_text()), [{"pid": 42}])
                # Exclusive creation also prevents overwriting even our own old result.
                output.write_text("preserve", encoding="utf-8")
                desktop_app.main()
                self.assertEqual(output.read_text(), "preserve")
            root.assert_not_called()
            config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
