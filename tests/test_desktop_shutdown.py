"""Tk timer shutdown regressions; no agent, networking or playback."""
import sys
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc_client"))
try:
    import desktop_app as desktop
finally:
    sys.path.pop(0)


class DesktopShutdownTests(unittest.TestCase):
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
        self.app = desktop.XassDesktop.__new__(desktop.XassDesktop)
        self.app.root = self.root
        self.app.preview = True
        self.app._closing = False
        self.app._close_local_music = Mock()
        self.app.stop_agent = Mock()
        self.app.tray_icon = None
        self.app._install_after_guard()

    def tearDown(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.after = self.app._tk_after
        self.root.unbind("<Destroy>")

    def schedule_real_poll_names(self):
        for callback in (self.app._drain_logs, self.app._refresh_update_status,
                         self.app._refresh_local_metrics):
            self.root.after(60_000, callback)
        child = tk.Frame(self.root)
        child.after_idle(lambda: None)
        self.assertEqual(len(self.root.tk.call("after", "info")), 4)
        return child

    def test_every_shutdown_path_cancels_timers_before_destroy(self):
        real_destroy = self.root.destroy
        for method in ("close", "_destroy_for_update", "exit_application"):
            with self.subTest(method=method):
                self.app._closing = False
                child = self.schedule_real_poll_names()
                observed = []
                self.root.destroy = lambda: observed.append(tuple(self.root.tk.call("after", "info")))
                try:
                    getattr(self.app, method)()
                finally:
                    self.root.destroy = real_destroy
                    child.destroy()
                self.assertEqual(observed, [()], "No deleted Python Tcl command can survive as a pending timer")

    def test_late_worker_and_idle_callbacks_are_not_enqueued_after_shutdown(self):
        self.app._cancel_after_callbacks()
        callback = Mock()
        self.assertIsNone(self.root.after(0, callback))
        self.assertIsNone(self.root.after_idle(callback))
        self.assertEqual(tuple(self.root.tk.call("after", "info")), ())
        self.root.update_idletasks()
        callback.assert_not_called()

    def test_destroying_a_child_does_not_stop_the_application(self):
        child = self.schedule_real_poll_names()
        child.destroy()
        self.assertFalse(self.app._closing)
        self.assertGreater(len(self.root.tk.call("after", "info")), 0)

    def test_root_destroy_event_cancels_direct_destroy_and_is_idempotent(self):
        from types import SimpleNamespace
        child = self.schedule_real_poll_names()
        pump = Mock()
        self.app._music_cast_pump = pump
        self.app._on_root_destroy(SimpleNamespace(widget=self.root))
        self.app._cancel_after_callbacks()
        self.assertTrue(self.app._closing)
        self.assertEqual(tuple(self.root.tk.call("after", "info")), ())
        pump.close.assert_called_once()
        child.destroy()


if __name__ == "__main__":
    unittest.main()
