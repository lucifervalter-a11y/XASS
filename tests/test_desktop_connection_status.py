from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# The executable imports sibling modules directly, as PyInstaller does.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc_client"))
try:
    import desktop_app
finally:
    sys.path.pop(0)


class DesktopConnectionStatusTests(unittest.TestCase):
    def test_only_a_fresh_report_for_the_current_process_is_online(self) -> None:
        report = {"state": "online", "process_id": 24, "updated_at": 990, "latency_ms": 123}
        actual = desktop_app.connection_snapshot(report, active_pid=24, started_at=900, now=1000)
        self.assertEqual(actual, {"state": "online", "age": 10, "latency_ms": 123})
        self.assertEqual(desktop_app.connection_snapshot(report, active_pid=25, now=1000)["state"], "connecting")
        self.assertEqual(desktop_app.connection_snapshot(report, active_pid=24, started_at=995, now=1000)["state"], "connecting")

    def test_old_online_report_expires_even_while_the_process_is_running(self) -> None:
        report = {"state": "online", "process_id": 24, "updated_at": 890}
        self.assertEqual(desktop_app.connection_snapshot(report, active_pid=24, interval_sec=30, now=1000)["state"], "stale")

    def test_bad_status_data_cannot_break_the_ui_refresh_loop(self) -> None:
        for invalid in (None, [], {"updated_at": "bad"}, {"updated_at": float("nan")}, {"process_id": 24, "updated_at": 1500}, {"process_id": 24, "updated_at": 0}):
            with self.subTest(invalid=invalid):
                self.assertEqual(desktop_app.connection_snapshot(invalid, active_pid=24, now=1000)["state"], "connecting")

    def test_health_check_does_not_claim_authenticated_agent_connection(self) -> None:
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app._connection_checking = True
        app.server_state_var = Mock()
        app._set_status = Mock()
        app._log = Mock()
        app._connection_checked(True, "server reachable")
        self.assertFalse(app._connection_checking)
        app.server_state_var.set.assert_called_once_with("Доступен")
        app._set_status.assert_not_called()

    def test_exited_external_process_clears_online_and_schedules_recovery(self) -> None:
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app._closing = app.preview = app._pairing = False
        app.process = None
        app.external_agent_pid = 24
        app._external_agent_started_at = 900
        app._start_after_id = None
        app.config = {"api_key": "test"}
        app.agent_pid_var = Mock()
        app.latency_var = Mock()
        app.server_state_var = Mock()
        app.root = Mock()
        app._set_status = Mock()
        app._schedule_start = Mock(side_effect=lambda _: setattr(app, "_start_after_id", "scheduled"))
        with patch.object(desktop_app.psutil, "Process", side_effect=desktop_app.psutil.NoSuchProcess(24)):
            app._refresh_agent_status()
        self.assertEqual(app.external_agent_pid, 0)
        app.agent_pid_var.set.assert_called_with("—")
        app._set_status.assert_called_once_with("Агент остановлен", desktop_app.RED)
        app._schedule_start.assert_called_once_with(750)

    def test_pid_reuse_is_not_treated_as_the_agent(self) -> None:
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app.preview = False
        app.process = None
        app._start_after_id = None
        app.external_agent_pid = 24
        app._external_agent_started_at = 900
        app._set_status = Mock()
        unrelated = Mock()
        unrelated.create_time.return_value = 999
        with patch.object(desktop_app.psutil, "Process", return_value=unrelated):
            app.stop_agent()
        unrelated.terminate.assert_not_called()

    def test_preview_cannot_start_or_stop_a_real_agent(self) -> None:
        app = desktop_app.XassDesktop.__new__(desktop_app.XassDesktop)
        app.preview = True
        app._closing = False
        with patch.object(desktop_app.subprocess, "Popen") as launch, patch.object(desktop_app.psutil, "Process") as process:
            app.start_agent()
            app.stop_agent()
            app.restart_agent()
        launch.assert_not_called()
        process.assert_not_called()


class DesktopLayoutTests(unittest.TestCase):
    def test_all_pages_resize_without_clipped_panels_or_callback_errors(self) -> None:
        try:
            root = desktop_app.tk.Tk()
        except desktop_app.tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        errors = []
        root.report_callback_exception = lambda *error: errors.append(error)
        app = desktop_app.XassDesktop(root, preview=True)
        try:
            for width in (900, 1360):
                root.geometry(f"{width}x620+20000+20000")
                root.update()
                for view in ("overview", "connection", "settings", "updates", "diagnostics", "archive"):
                    with self.subTest(width=width, view=view):
                        app.show_view(view)
                        root.update()
                        def check_panels(widget):
                            if isinstance(widget, desktop_app.ResponsiveColumns):
                                for panel in widget.panels:
                                    self.assertLessEqual(panel.winfo_x() + panel.winfo_width(), widget.winfo_width())
                            for child in widget.winfo_children():
                                check_panels(child)
                        check_panels(app.content)
                        self.assertFalse(errors, errors)
            self.assertEqual(app.connection_var.get(), "Предпросмотр")
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
