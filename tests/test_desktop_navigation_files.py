"""Invisible Tk geometry regressions; no agent, real files, audio or network."""
from __future__ import annotations

import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc_client"))
try:
    import desktop_app as desktop
    from desktop_widgets import RoundedPanel
finally:
    sys.path.pop(0)


class NavigationAndFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.root.withdraw()
        # Mapping is required for pack/canvas geometry. The window stays fully
        # transparent and never receives keyboard focus during these tests.
        self.root.attributes("-alpha", 0)
        self.root.geometry("900x620+3000+3000")
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.addCleanup(self.finish)

    def finish(self):
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        for child in self.root.winfo_children():
            child.destroy()
        self.root.withdraw()
        self.assertFalse(self.errors, self.errors)

    def make_shell(self, scaling=1.333):
        self.root.tk.call("tk", "scaling", scaling)
        app = desktop.XassDesktop.__new__(desktop.XassDesktop)
        app.root = self.root
        app.preview = False
        app._closing = False
        app.brand_image = None
        app.nav_buttons = {}
        app.status_color = desktop.ACCENT
        app.connection_var = tk.StringVar(self.root, value="Fixture")
        app._build_shell()
        self.root.deiconify()
        self.settle()
        return app

    def settle(self):
        for _ in range(6):
            self.root.update()
            time.sleep(0.01)

    def assert_navigation_accessible(self, scaling):
        app = self.make_shell(scaling)
        self.assertEqual(len(app.nav_buttons), 9)
        for key, button in app.nav_buttons.items():
            with self.subTest(key=key, scaling=scaling):
                self.assertTrue(button.winfo_ismapped())
                self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
                self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
                button.event_generate("<FocusIn>")
                self.settle()
                top = app.navigation_canvas.canvasy(0)
                self.assertGreaterEqual(button.winfo_y(), top - 1)
                self.assertLessEqual(button.winfo_y() + button.winfo_height(),
                                     top + app.navigation_canvas.winfo_height() + 1)
        app.navigation_canvas.yview_moveto(0)
        app._on_mousewheel(SimpleNamespace(widget=app.nav_buttons["overview"], delta=-120))
        self.assertGreater(app.navigation_canvas.yview()[0], 0, "Wheel reaches the lower navigation")

    def test_all_navigation_remains_reachable_at_150_percent(self):
        self.assert_navigation_accessible(2.0)

    def test_all_navigation_remains_reachable_at_200_percent(self):
        self.assert_navigation_accessible(2.667)

    def assert_file_batches_keep_background(self, count):
        app = self.make_shell()
        entries = [{"name": f"fixture-{index}.mp3", "type": "file", "size": index + 1}
                   for index in range(count)]
        with patch.object(desktop, "list_files", return_value={"entries": entries}):
            app.show_view("files")
            card = next(w for w in app.content.winfo_children() if isinstance(w, RoundedPanel))
            background = card._background
            deadline = time.monotonic() + 5
            expected = f"fixture-{count - 1}.mp3"
            while expected not in self.label_texts(card) and time.monotonic() < deadline:
                self.settle()
            self.assertIn(expected, self.label_texts(card))
            self.assertEqual(sum(text.startswith("fixture-") for text in self.label_texts(card)), count)
            self.assertIs(card._background, background)
            self.assertTrue(background.winfo_exists())
            self.assertIsNotNone(card._background_image)
            card.configure(border_color="#7c88ab")
            self.settle()
            self.assertTrue(background.find_all(), "Rounded backdrop still paints after the asynchronous replacement")
            self.assertFalse(self.errors)

    @classmethod
    def label_texts(cls, widget):
        values = [str(widget.cget("text"))] if isinstance(widget, tk.Label) else []
        for child in widget.winfo_children():
            values.extend(cls.label_texts(child))
        return values

    def test_single_file_result_preserves_rounded_canvas(self):
        self.assert_file_batches_keep_background(1)

    def test_multiple_progressive_batches_preserve_rounded_canvas(self):
        self.assert_file_batches_keep_background(65)

    def test_dynamic_long_filename_wraps_without_hiding_its_size(self):
        app = self.make_shell()
        name = "long-audio-filename-" * 12 + ".mp3"
        with patch.object(desktop, "list_files", return_value={"entries": [
                {"name": name, "type": "file", "size": 123456}]}):
            app.show_view("files")
            self.settle()
            def walk(widget):
                yield widget
                for child in widget.winfo_children():
                    yield from walk(child)
            labels = [w for w in walk(app.content) if isinstance(w, tk.Label)]
            title = next(w for w in labels if w.cget("text") == name)
            size = next(w for w in labels if w.cget("text") == app._format_bytes(123456))
            self.assertGreater(title.winfo_pixels(str(title.cget("wraplength"))), 0)
            self.assertGreater(title.winfo_height(), 30, "The full long name is shown on multiple lines")
            self.assertTrue(size.winfo_ismapped())
            self.assertGreaterEqual(size.winfo_width(), size.winfo_reqwidth())


if __name__ == "__main__":
    unittest.main()
