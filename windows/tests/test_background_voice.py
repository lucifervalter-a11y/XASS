"""Background listener protocol, races, segmentation, and fake WinMM; no mic I/O."""
from __future__ import annotations

import ctypes
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pc_client"))
sys.path.insert(0, str(ROOT / "windows"))
import background_voice as bv
import background_voice_bridge as bridge

SPEECH = b"\xe8\x03" * (bv.FRAME_BYTES // 2)
SILENCE = b"\x00\x00" * (bv.FRAME_BYTES // 2)
COMMAND = "Джарвис, открой YouTube"


class Events:
    def __init__(self):
        self.values = []
        self.condition = threading.Condition()

    def __call__(self, event):
        with self.condition:
            self.values.append(event)
            self.condition.notify_all()

    def wait(self, predicate):
        with self.condition:
            if not self.condition.wait_for(lambda: predicate(self.values), timeout=3):
                raise AssertionError(f"Missing expected lifecycle event: {self.values!r}")
            return list(self.values)

    def state(self, state, count=1):
        return self.wait(lambda events: sum(e.get("state") == state for e in events) >= count)


class FakeCapture:
    def __init__(self, pcm=None, *, gate=None, on_close=lambda: None):
        self.pcm = pcm
        self.gate = gate
        self.on_close = on_close
        self.closed = False
        self.started = threading.Event()
        self.finished = threading.Event()

    def read_segment(self, cancelled):
        self.started.set()
        try:
            if self.gate is not None:
                while not self.gate.wait(0.005):
                    if cancelled.is_set():
                        return None
            if self.pcm is None:
                cancelled.wait(3)
            return None if cancelled.is_set() else self.pcm
        finally:
            self.finished.set()

    def close(self):
        self.closed = True
        self.on_close()


class WorkerTests(unittest.TestCase):
    def worker(self, *, transcriber=None, captures=None, loader=None):
        self.events = Events()
        self.transcriber = transcriber or Mock()
        self.loader = loader or Mock(return_value=self.transcriber)
        self.capture_queue = queue.Queue()
        for capture in captures or []:
            self.capture_queue.put(capture)
        self.idle = FakeCapture()

        def factory():
            try:
                return self.capture_queue.get_nowait()
            except queue.Empty:
                return self.idle

        self.factory = Mock(side_effect=factory)
        worker = bv.BackgroundVoiceWorker(self.events, transcriber_factory=self.loader,
                                          capture_factory=self.factory)
        self.addCleanup(worker.stop)
        return worker

    def start(self, worker, **extra):
        worker.handle({"operation": "start", "model_path": "/local/model", **extra})

    def pause(self, worker, id=17):
        worker.handle({"operation": "pause", "id": id})

    def test_default_construct_does_not_load_or_listen(self):
        worker = self.worker()
        self.loader.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(self.events.values, [])

    def test_addressed_command_closes_before_emit_and_requires_resume(self):
        capture = FakeCapture(SPEECH)
        worker = self.worker(captures=[capture])
        def recognize(_):
            self.assertTrue(capture.closed, "Mic must close before inference starts")
            return COMMAND
        self.transcriber.transcribe.side_effect = recognize
        self.start(worker, epoch=3)
        events = self.events.wait(lambda es: any(e["type"] == "command" for e in es))
        self.assertTrue(capture.closed)
        self.assertEqual(events[-2], {"type": "state", "state": "paused", "epoch": 3})
        self.assertEqual(events[-1], {"type": "command", "text": COMMAND, "epoch": 3})
        self.assertEqual(self.factory.call_count, 1)
        worker.handle({"operation": "resume", "epoch": 4})
        self.events.state("listening", 2)
        self.assertEqual(self.loader.call_count, 1)
        self.pause(worker)
        self.assertTrue(self.idle.closed)
        self.assertEqual(self.events.values[-1]["id"], 17)

    def test_ambient_is_not_emitted_or_latched_into_later_command(self):
        ambient = ["Сегодня Джарвис открой YouTube", "Джарвис", "открой YouTube", COMMAND]
        worker = self.worker(captures=[FakeCapture(SPEECH) for _ in ambient])
        self.transcriber.transcribe.side_effect = ambient
        self.start(worker)
        events = self.events.wait(lambda es: any(e["type"] == "command" for e in es))
        commands = [e for e in events if e["type"] == "command"]
        self.assertEqual(commands, [{"type": "command", "text": COMMAND, "epoch": 1}])
        self.assertNotIn(ambient[0], json.dumps(events, ensure_ascii=False))
        self.assertEqual(self.transcriber.transcribe.call_count, 4)

    def test_pause_during_model_load_acknowledges_without_opening_mic(self):
        loading = threading.Event()
        release = threading.Event()
        transcriber = Mock()

        def load(_):
            loading.set()
            release.wait(3)
            return transcriber

        worker = self.worker(loader=load)
        self.addCleanup(release.set)
        self.start(worker)
        self.assertTrue(loading.wait(3))
        self.pause(worker, 25)
        self.assertEqual(self.events.values[-1]["id"], 25)
        self.factory.assert_not_called()
        release.set()
        self.events.state("ready")
        self.events.state("paused", 2)
        self.factory.assert_not_called()
        worker.handle({"operation": "resume", "epoch": 2})
        self.events.state("listening")

    def test_start_paused_loads_once_but_requires_explicit_resume(self):
        worker = self.worker()
        self.start(worker, paused=True)
        self.events.state("paused")
        self.factory.assert_not_called()
        worker.handle({"operation": "resume", "epoch": 8})
        self.events.state("listening")
        self.assertEqual(self.loader.call_count, 1)

    def test_pause_ack_follows_device_close_and_flushes_partial_capture(self):
        gate = threading.Event()
        order = []
        capture = FakeCapture(SPEECH, gate=gate, on_close=lambda: order.append("closed"))
        worker = self.worker(captures=[capture])
        original_emit = worker._emit
        worker._emit = lambda event: (order.append(event.get("state")), original_emit(event))
        self.start(worker)
        self.assertTrue(capture.started.wait(3))
        self.pause(worker)
        self.assertLess(order.index("closed"), order.index("paused"))
        self.assertTrue(capture.finished.wait(3))
        self.transcriber.transcribe.assert_not_called()
        self.assertFalse(any(e["type"] == "command" for e in self.events.values))

    def test_pause_during_inference_discards_result_even_after_resume(self):
        inference = threading.Event()
        release = threading.Event()
        capture = FakeCapture(SPEECH)
        worker = self.worker(captures=[capture])

        def transcribe(_):
            inference.set()
            release.wait(3)
            return COMMAND

        self.transcriber.transcribe.side_effect = transcribe
        self.addCleanup(release.set)
        self.start(worker, epoch=1)
        self.assertTrue(inference.wait(3))
        self.assertTrue(capture.closed)
        self.pause(worker, 70)
        self.assertEqual(self.events.values[-1]["id"], 70)
        worker.handle({"operation": "resume", "epoch": 2})
        release.set()
        self.events.state("listening", 2)
        self.assertFalse(any(e["type"] == "command" for e in self.events.values))

    def test_stop_during_inference_discards_result_and_exits_worker(self):
        inference = threading.Event()
        release = threading.Event()
        worker = self.worker(captures=[FakeCapture(SPEECH)])

        def transcribe(_):
            inference.set()
            release.wait(3)
            return COMMAND

        self.transcriber.transcribe.side_effect = transcribe
        self.addCleanup(release.set)
        self.start(worker)
        self.assertTrue(inference.wait(3))
        worker.stop()
        release.set()
        worker._thread.join(3)
        self.assertFalse(worker._thread.is_alive())
        self.assertFalse(any(e["type"] == "command" for e in self.events.values))
        self.assertEqual(self.events.values[-1]["state"], "stopped")

    def test_broken_output_cancels_and_closes_without_daemon_traceback(self):
        worker = self.worker()
        emit = worker._emit

        def broken(event):
            if event.get("state") == "listening":
                raise BrokenPipeError("PRIVATE")
            emit(event)

        worker._emit = broken
        self.start(worker)
        worker._thread.join(3)
        self.assertFalse(worker._thread.is_alive())
        self.assertTrue(self.idle.closed)
        self.assertNotIn("PRIVATE", json.dumps(self.events.values))

    def test_repeated_pause_has_matching_ack_and_stop_is_idempotent(self):
        worker = self.worker()
        self.start(worker)
        self.events.state("listening")
        self.pause(worker, 1)
        self.pause(worker, 2)
        self.assertEqual([e["id"] for e in self.events.values if "id" in e], [1, 2])
        worker.stop()
        worker.stop()
        self.assertEqual(sum(e.get("state") == "stopped" for e in self.events.values), 1)

    def test_stop_during_load_never_emits_ready_or_opens_mic(self):
        loading = threading.Event()
        release = threading.Event()

        def load(_):
            loading.set()
            release.wait(3)
            return Mock()

        worker = self.worker(loader=load)
        self.addCleanup(release.set)
        self.start(worker)
        self.assertTrue(loading.wait(3))
        worker.stop()
        release.set()
        worker._thread.join(3)
        self.assertFalse(worker._thread.is_alive())
        self.factory.assert_not_called()
        self.assertEqual([e.get("state") for e in self.events.values], ["loading_model", "stopped"])

    def test_model_and_unexpected_inference_errors_are_sanitized(self):
        worker = self.worker(loader=Mock(side_effect=RuntimeError("SECRET model path/transcript")))
        self.start(worker)
        events = self.events.state("stopped")
        self.assertNotIn("SECRET", json.dumps(events))
        self.assertEqual(events[-2]["code"], "model_unavailable")

    def test_recoverable_transcription_rejection_drops_all_text(self):
        worker = self.worker(captures=[FakeCapture(SPEECH), FakeCapture(SPEECH)])
        self.transcriber.transcribe.side_effect = [ValueError("PRIVATE noise"), COMMAND]
        self.start(worker)
        events = self.events.wait(lambda es: any(e["type"] == "command" for e in es))
        self.assertNotIn("PRIVATE", json.dumps(events))
        self.assertFalse(any(e["type"] == "error" for e in events))

    def test_microphone_failure_stops_instead_of_busy_retry(self):
        worker = self.worker()
        self.factory.side_effect = RuntimeError("PRIVATE native path")
        self.start(worker)
        events = self.events.state("stopped")
        self.assertNotIn("PRIVATE", json.dumps(events))
        self.assertEqual(self.factory.call_count, 1)

    def test_close_failure_never_acknowledges_pause(self):
        capture = FakeCapture()
        worker = self.worker(captures=[capture])
        self.start(worker)
        self.events.state("listening")
        capture.close = Mock(side_effect=bv.ListenerError("microphone_close_failed"))
        with self.assertRaises(bv.ListenerError):
            self.pause(worker, 100)
        self.assertFalse(any(e.get("id") == 100 for e in self.events.values))
        worker.fail("microphone_close_failed")
        self.assertEqual(self.events.values[-1]["code"], "microphone_close_failed")
        self.assertFalse(any(e.get("state") == "stopped" for e in self.events.values))

    def test_invalid_lifecycle_and_arbitrary_operations(self):
        worker = self.worker()
        for request in ({"operation": "pause", "id": 1}, {"operation": "resume", "epoch": 1},
                        {"operation": "execute"}, {"operation": "upload"}):
            with self.assertRaises(bv.ListenerError):
                worker.handle(request)
        self.start(worker, paused=True)
        with self.assertRaises(bv.ListenerError):
            self.start(worker)


class SegmentTests(unittest.TestCase):
    def test_prefix_is_anchored_cyrillic_and_same_utterance(self):
        for text in (COMMAND, "  ДЖАРВИС открой дискорд  ", "Джарвис,открой ютуб"):
            self.assertEqual(bv.addressed_command(text), text.strip())
        for text in ("Jarvis open youtube", "расскажи про Джарвис, открой ютуб", "Джарвисов открой",
                     "Джарвис", "Джарвис,", "Джарвис, ...", "Джарвис\nоткрой ютуб",
                     "Джарвис, " + "x" * 512, None, {}, "Джарвис, x\x00"):
            self.assertIsNone(bv.addressed_command(text), repr(text))

    def test_silence_never_accumulates_or_yields_a_segment(self):
        segmenter = bv.SilenceSegmenter()
        for _ in range(500):
            self.assertIsNone(segmenter.feed(SILENCE))
        self.assertEqual(len(segmenter._pcm), 0)
        self.assertEqual(len(segmenter._pre_roll), 2)

    def test_silence_finalizes_bounded_utterance_with_short_pre_roll(self):
        segmenter = bv.SilenceSegmenter()
        for _ in range(20):
            segmenter.feed(SILENCE)
        for _ in range(4):
            self.assertIsNone(segmenter.feed(SPEECH))
        for _ in range(6):
            self.assertIsNone(segmenter.feed(SILENCE))
        result = segmenter.feed(SILENCE)
        self.assertEqual(result, SILENCE * 2 + SPEECH * 4 + SILENCE * 7)
        self.assertEqual(len(segmenter._pcm), 0)

    def test_short_clicks_are_rejected(self):
        segmenter = bv.SilenceSegmenter()
        segmenter.feed(SPEECH)
        for _ in range(7):
            self.assertIsNone(segmenter.feed(SILENCE))

    def test_overlong_speech_is_discarded_until_silence_without_splitting(self):
        segmenter = bv.SilenceSegmenter()
        for _ in range(1000):
            self.assertIsNone(segmenter.feed(SPEECH))
            self.assertLessEqual(len(segmenter._pcm), bv.MAX_PCM_BYTES)
        for _ in range(7):
            self.assertIsNone(segmenter.feed(SILENCE))
        for _ in range(3):
            segmenter.feed(SPEECH)
        results = [segmenter.feed(SILENCE) for _ in range(7)]
        self.assertEqual(results[-1], SPEECH * 3 + SILENCE * 7)

    def test_invalid_pcm_and_clear(self):
        segmenter = bv.SilenceSegmenter()
        for frame in (b"", b"x", b"\x00\x00", SPEECH + SPEECH):
            with self.assertRaises(bv.ListenerError):
                segmenter.feed(frame)
        segmenter.feed(SPEECH)
        segmenter.clear()
        self.assertEqual(segmenter._pcm, b"")
        self.assertFalse(segmenter._speaking)


class WinMMTests(unittest.TestCase):
    def api(self):
        api = Mock()
        for name in ("waveInOpen", "waveInPrepareHeader", "waveInAddBuffer", "waveInStart",
                     "waveInReset", "waveInUnprepareHeader", "waveInClose"):
            getattr(api, name).return_value = 0
        return api

    def test_native_struct_layout_matches_windows(self):
        self.assertEqual(ctypes.sizeof(bv.WaveFormat), 18)
        self.assertEqual(ctypes.sizeof(bv.WaveHeader), 48 if ctypes.sizeof(ctypes.c_void_p) == 8 else 32)

    def test_four_small_buffers_and_idempotent_close_zero_memory(self):
        api = self.api()
        capture = bv.WinMMCapture(_api=api)
        buffers = [buffer for buffer, _ in capture._buffers]
        capture._segmenter.feed(SPEECH)
        self.assertTrue(capture._segmenter._pcm)
        self.assertEqual(len(buffers), 4)
        for buffer in buffers:
            self.assertEqual(len(buffer), bv.FRAME_BYTES)
            ctypes.memmove(buffer, SPEECH, len(SPEECH))
        capture.close()
        capture.close()
        api.waveInReset.assert_called_once()
        api.waveInClose.assert_called_once()
        self.assertEqual(capture._segmenter._pcm, b"")
        self.assertEqual(api.waveInUnprepareHeader.call_count, 4)
        self.assertTrue(all(buffer.raw == SILENCE for buffer in buffers))

    def test_open_failure_never_starts_or_closes_unowned_device(self):
        api = self.api()
        api.waveInOpen.return_value = 1
        with self.assertRaises(bv.ListenerError):
            bv.WinMMCapture(_api=api)
        api.waveInStart.assert_not_called()
        api.waveInClose.assert_not_called()

    def test_prepare_add_and_start_failures_release_all_owned_buffers(self):
        for name in ("waveInPrepareHeader", "waveInAddBuffer", "waveInStart"):
            with self.subTest(name=name):
                api = self.api()
                getattr(api, name).return_value = 1
                with self.assertRaises(bv.ListenerError):
                    bv.WinMMCapture(_api=api)
                api.waveInReset.assert_called_once()
                api.waveInClose.assert_called_once()

    def test_read_rotates_prepared_buffers_and_cancellation_returns_nothing(self):
        api = self.api()
        capture = bv.WinMMCapture(_api=api)
        self.addCleanup(capture.close)
        buffer, header = capture._buffers[0]
        ctypes.memmove(buffer, SPEECH, len(SPEECH))
        header.recorded = len(SPEECH)
        header.flags = 1
        cancel = threading.Event()
        self.assertEqual(capture._read_frame(cancel), SPEECH)
        self.assertEqual(capture._next, 1)
        self.assertEqual(buffer.raw, SILENCE)
        cancel.set()
        self.assertIsNone(capture.read_segment(cancel))

    def test_native_stall_has_bounded_timeout(self):
        capture = bv.WinMMCapture(_api=self.api())
        self.addCleanup(capture.close)
        with patch.object(bv.time, "monotonic", side_effect=[0, 3]):
            with self.assertRaises(bv.ListenerError):
                capture._read_frame(threading.Event())

    def test_partial_buffer_is_not_treated_as_complete_speech(self):
        capture = bv.WinMMCapture(_api=self.api())
        self.addCleanup(capture.close)
        capture._buffers[0][1].flags = 1
        capture._buffers[0][1].recorded = 4
        with self.assertRaises(bv.ListenerError):
            capture._read_frame(threading.Event())

    def test_close_failure_retains_memory_until_driver_releases_it(self):
        api = self.api()
        capture = bv.WinMMCapture(_api=api)
        self.addCleanup(capture.close)
        api.waveInReset.return_value = 1
        with self.assertRaises(bv.ListenerError):
            capture.close()
        self.assertEqual(len(capture._buffers), 4)
        self.assertIn(capture, bv._UNCLOSED_CAPTURES)
        api.waveInUnprepareHeader.assert_not_called()
        api.waveInReset.return_value = 0
        capture.close()
        self.assertNotIn(capture, bv._UNCLOSED_CAPTURES)


class ProtocolTests(unittest.TestCase):
    def run_protocol(self, data):
        out = io.StringIO()
        bridge.run(io.BytesIO(data), out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_only_exact_bounded_operations_and_fields(self):
        invalid = [None, [], True, {}, {"operation": []}, {"operation": "exec"},
                   {"operation": "stop", "payload": "ignored"},
                   {"operation": "start", "model_path": ""},
                   {"operation": "start", "model_path": "a" * 2049},
                   {"operation": "start", "model_path": "/m", "paused": 1},
                   {"operation": "start", "model_path": "/m", "epoch": True},
                   {"operation": "pause", "id": True}, {"operation": "pause", "id": -1},
                   {"operation": "resume"}, {"operation": "resume", "epoch": 0},
                   {"operation": "resume", "epoch": 1 << 64}]
        for request in invalid:
            with self.subTest(request=request):
                with self.assertRaises(bv.ListenerError):
                    bv.validate_request(request)

    def test_invalid_oversized_duplicate_and_non_utf8_lines_fail_closed(self):
        for raw in (b"not json\n", b"x" * (bridge.MAX_REQUEST_BYTES + 1), b"\xff\n",
                    b'{"operation":"stop","operation":"start"}\n', b"[]\n"):
            with self.subTest(raw=raw[:60]):
                events = self.run_protocol(raw)
                self.assertEqual(events[0]["type"], "error")
                self.assertEqual(events[0]["code"], "invalid_request")
                self.assertEqual(events[-1]["state"], "stopped")

    def test_stop_and_eof_stop_without_model_or_device(self):
        for raw in (b"", b'{"operation":"stop"}\n'):
            self.assertEqual(self.run_protocol(raw), [{"type": "state", "state": "stopped", "epoch": 1}])

    def test_stdin_eof_cancels_while_model_load_is_blocked(self):
        release = threading.Event()
        worker_holder = []
        output = io.StringIO()

        def factory(emit):
            worker = bv.BackgroundVoiceWorker(emit, transcriber_factory=lambda _: (release.wait(3), Mock())[1],
                                               capture_factory=Mock(side_effect=AssertionError("must not open")))
            worker_holder.append(worker)
            return worker

        self.addCleanup(release.set)
        bridge.run(io.BytesIO(b'{"operation":"start","model_path":"/model"}\n'), output,
                   worker_factory=factory)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["state"], "stopped")
        release.set()
        worker_holder[0]._thread.join(3)
        self.assertFalse(worker_holder[0]._thread.is_alive())

    def test_stdin_eof_closes_an_active_capture(self):
        capture = FakeCapture()
        workers = []

        def factory(emit):
            worker = bv.BackgroundVoiceWorker(emit, transcriber_factory=Mock(return_value=Mock()),
                                               capture_factory=lambda: capture)
            workers.append(worker)
            return worker

        class Input:
            first = True

            def readline(self, limit):
                if self.first:
                    self.first = False
                    return b'{"operation":"start","model_path":"/model"}\n'
                if not capture.started.wait(3):
                    raise AssertionError("Capture was not started")
                return b""

        output = io.StringIO()
        bridge.run(Input(), output, worker_factory=factory)
        self.assertTrue(capture.closed)
        workers[0]._thread.join(3)
        self.assertFalse(workers[0]._thread.is_alive())
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["state"], "stopped")

    def test_output_is_ascii_json_serialized_across_threads(self):
        out = io.StringIO()
        writer = bridge.JsonLineWriter(out)
        threads = [threading.Thread(target=lambda: [writer({"type": "command", "text": COMMAND})
                                                    for _ in range(100)]) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 400)
        self.assertTrue(out.getvalue().isascii())
        self.assertTrue(all(json.loads(line)["text"] == COMMAND for line in lines))
        with self.assertRaises(bv.ListenerError):
            writer({"text": "x" * bridge.MAX_EVENT_BYTES})

    def test_entrypoint_stop_never_starts_model_and_has_no_stderr(self):
        result = subprocess.run([sys.executable, str(ROOT / "windows" / "background_voice_bridge.py")],
                                input=b'{"operation":"stop"}\n', capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(json.loads(result.stdout)["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
