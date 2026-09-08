"""Work-count regressions for Tk responsiveness; no agent or visible window."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import tempfile
import threading
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
            self.assertEqual(hero.itemcget(hero._heading, "text"), "Renamed computer")
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


class SystemSamplingTests(unittest.TestCase):
    def make_app(self):
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app._closing = app._hidden_to_tray = app._metrics_sampling = False
        app._metrics_results = queue.Queue(maxsize=1)
        app.root = Mock()
        return app

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
