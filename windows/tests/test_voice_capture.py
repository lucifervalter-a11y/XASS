"""Native capture lifetime tests with a fake WinMM device; no microphone I/O."""
import ctypes
from pathlib import Path
import sys
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


if __name__ == "__main__":
    unittest.main()
