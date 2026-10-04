"""Native capture lifetime tests with a fake WinMM device; no microphone I/O."""
import ctypes
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pc_client"))
import voice_capture as vc


@unittest.skipUnless(vc.os.name == "nt", "WinMM ABI tests require Windows")
class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        for name in ("waveInOpen", "waveInPrepareHeader", "waveInAddBuffer", "waveInStart",
                     "waveInReset", "waveInClose", "waveInUnprepareHeader"):
            getattr(self.api, name).return_value = 0
        def finish(handle, pointer, size):
            header = pointer._obj
            ctypes.memmove(header.data, b"\x01\x00" * 10, 20)
            header.recorded = 20
            header.flags = 1
            return 0
        self.api.waveInAddBuffer.side_effect = finish
        self.patch = patch.object(vc.ctypes, "WinDLL", return_value=self.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_capture_returns_only_recorded_bytes_and_releases_device(self):
        self.assertEqual(vc.record_pcm(1), b"\x01\x00" * 10)
        self.api.waveInReset.assert_called_once()
        self.api.waveInUnprepareHeader.assert_called_once()
        self.api.waveInClose.assert_called_once()

    def test_device_open_failure_does_not_start_capture(self):
        self.api.waveInOpen.return_value = 1
        with self.assertRaises(vc.AssistantError):
            vc.record_pcm(1)
        self.api.waveInStart.assert_not_called()
        self.api.waveInClose.assert_not_called()

    def test_prepare_failure_still_closes_handle(self):
        self.api.waveInPrepareHeader.return_value = 1
        with self.assertRaises(vc.AssistantError):
            vc.record_pcm(1)
        self.api.waveInClose.assert_called_once()
        self.api.waveInUnprepareHeader.assert_not_called()

    def test_start_failure_releases_prepared_buffer(self):
        self.api.waveInStart.return_value = 1
        with self.assertRaises(vc.AssistantError):
            vc.record_pcm(1)
        self.api.waveInReset.assert_called_once()
        self.api.waveInUnprepareHeader.assert_called_once()
        self.api.waveInClose.assert_called_once()

    def test_duration_is_bounded_before_native_call(self):
        for seconds in (-1, 0, 11, True, 1.5):
            with self.assertRaises(vc.AssistantError):
                vc.record_pcm(seconds)
        self.api.waveInOpen.assert_not_called()

    def test_stalled_device_times_out_and_releases_microphone(self):
        self.api.waveInAddBuffer.side_effect = None
        with patch.object(vc.time, "monotonic", side_effect=[0, 5]):
            with self.assertRaises(vc.AssistantError):
                vc.record_pcm(1)
        self.api.waveInReset.assert_called_once()
        self.api.waveInClose.assert_called_once()


class LocalModelValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        for filename in ("model.bin", "config.json", "tokenizer.json"):
            (self.path / filename).write_bytes(b"fixture")
        self.factory = Mock()
        self.modules = patch.dict(sys.modules, {
            "numpy": SimpleNamespace(), "faster_whisper": SimpleNamespace(WhisperModel=self.factory)})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_complete_folder_preserves_exact_explicit_model_and_offline_flag(self):
        vc.WhisperTranscriber(str(self.path))
        self.factory.assert_called_once_with(str(self.path), device="cpu", compute_type="int8",
                                            cpu_threads=4, num_workers=1, local_files_only=True)

    def test_each_missing_or_empty_required_file_fails_before_model_load(self):
        for filename in ("model.bin", "config.json", "tokenizer.json"):
            for empty in (False, True):
                with self.subTest(filename=filename, empty=empty):
                    target = self.path / filename
                    target.unlink()
                    if empty:
                        target.touch()
                    with self.assertRaisesRegex(vc.AssistantError, "Автозагрузка отключена"):
                        vc.WhisperTranscriber(str(self.path))
                    self.factory.assert_not_called()
                    target.write_bytes(b"fixture")

    def test_relative_or_missing_folder_never_starts_model_load(self):
        for path in ("small", "Systran/faster-whisper-small", str(self.path / "missing")):
            with self.subTest(path=path):
                with self.assertRaises(vc.AssistantError):
                    vc.WhisperTranscriber(path)
                self.factory.assert_not_called()

    def test_directory_instead_of_required_file_is_rejected(self):
        target = self.path / "tokenizer.json"
        target.unlink()
        target.mkdir()
        with self.assertRaises(vc.AssistantError):
            vc.WhisperTranscriber(str(self.path))
        self.factory.assert_not_called()

    def test_unreadable_metadata_fails_closed(self):
        with patch.object(Path, "stat", side_effect=PermissionError("not readable")):
            with self.assertRaises(vc.AssistantError):
                vc.WhisperTranscriber(str(self.path))
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
