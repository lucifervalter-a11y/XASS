"""Stable transfer error codes exposed to authenticated playback clients."""
from types import SimpleNamespace
import unittest

from app.services.music_diagnostics import TRANSFER_ERROR_CODES, classify_transfer_failure, transfer_failure_code


class MusicDiagnosticsTests(unittest.TestCase):
    def test_known_server_reasons_have_stable_categories(self):
        cases = {
            "ПК не подтвердил остановку звука": "source_stop_failed",
            "Не удалось остановить прежний ПК": "source_stop_failed",
            "ПК не подтвердил запуск музыки": "target_start_failed",
            "Запуск был прерван. Повторите переключение": "target_start_failed",
            "ПК не запустил музыку": "target_start_failed",
            "Переключение отменено": "transfer_cancelled",
            "Переключение заменено новым действием": "replaced",
        }
        for message, code in cases.items():
            self.assertEqual(classify_transfer_failure(message), code)

    def test_timeout_and_availability_preserve_failure_phase(self):
        for message in ("Время переключения истекло", "Время переключения истекло. Повторите действие",
                        "Предыдущее устройство не подтвердило переключение. Музыка не запущена повторно; попробуйте ещё раз"):
            self.assertEqual(classify_transfer_failure(message, phase="waiting"), "source_timeout")
            self.assertEqual(classify_transfer_failure(message, phase="starting"), "target_timeout")
        self.assertEqual(classify_transfer_failure("Компьютер не в сети", phase="starting"), "target_unavailable")
        self.assertEqual(classify_transfer_failure("Компьютер не в сети", phase="waiting"), "source_stop_failed")

    def test_arbitrary_agent_text_never_becomes_a_diagnostic_code(self):
        for value in ("fixture secret URL ?ticket=private", "Переключение отменено\nprivate", "" , None, {"secret": "fixture"}):
            result = classify_transfer_failure(value)
            self.assertIn(result, TRANSFER_ERROR_CODES)
            self.assertEqual(result, "agent_command_failed" if isinstance(value, str) and value else "unknown")

    def test_persisted_codes_are_allowlisted_and_old_rows_have_safe_fallback(self):
        value = SimpleNamespace(target={"failure_code": "target_timeout"}, start_command_id=None,
                                detail="arbitrary agent text")
        self.assertEqual(transfer_failure_code(value), "target_timeout")
        for invalid in ("private", ["private"], {"private": True}):
            value.target = {"failure_code": invalid}
            self.assertEqual(transfer_failure_code(value), "agent_command_failed")
        value.target = {}
        value.detail = "Время переключения истекло"
        self.assertEqual(transfer_failure_code(value), "source_timeout")
        value.start_command_id = 1
        self.assertEqual(transfer_failure_code(value), "target_timeout")
