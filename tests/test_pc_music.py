from __future__ import annotations

import io
import sys
import tempfile
import threading
import time
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import patch

import httpx

CLIENT_ROOT = Path(__file__).resolve().parents[1] / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import music_player as mp

CONFIG = {"server_url": "https://music.example", "api_key": "not-a-real-key"}
URL = "https://music.example/agent/music/tracks/7/stream?ticket=test-ticket"


def silent_wav():
    data = io.BytesIO()
    with wave.open(data, "wb") as sound:
        sound.setnchannels(2)
        sound.setsampwidth(2)
        sound.setframerate(mp.SAMPLE_RATE)
        sound.writeframes(b"\0" * (mp.SAMPLE_RATE * 4 // 10))
    return data.getvalue()


class FakeDevice:
    def __init__(self):
        self.running = False
        self.callback = None
        self.closed = False

    def start(self, callback):
        self.callback = callback
        self.running = True

    def close(self):
        self.running = False
        self.closed = True


class FakeAudio:
    def __init__(self):
        self.ids = {"default": None, "stable-one": object(), "stable-two": object()}
        self.devices = []
        self.selected = []
        self.seek_positions = []

    def outputs(self):
        return ([{"id": key, "name": "Same name", "is_default": key == "default"} for key in self.ids], self.ids)

    def duration(self, path):
        return 100.0

    def stream(self, path, position):
        self.seek_positions.append(position)
        def generator():
            count = yield array("h")
            while True:
                count = yield array("h", [1000, -1000] * count)
        value = generator()
        next(value)
        return value

    def device(self, output_id):
        self.selected.append(output_id)
        device = FakeDevice()
        self.devices.append(device)
        return device


class ApprovedMusicUrlTests(unittest.TestCase):
    def test_exact_origin_route_and_ticket(self):
        self.assertEqual(mp.approved_media_url(CONFIG["server_url"], URL, 7), (URL, 7))
        nested = "https://music.example/api/agent/music/tracks/7/stream?ticket=test-ticket"
        self.assertEqual(mp.approved_media_url("https://music.example/api", nested, "7"), (nested, 7))

    def test_arbitrary_urls_credentials_traversal_and_duplicate_ticket_rejected(self):
        bad = [
            URL.replace("music.example", "evil.example"), URL.replace("https:", "http:"),
            URL.replace("music.example", "music.example:444"), URL.replace("https://", "https://user:pass@"),
            URL.replace("https://", "https://@"), URL.replace("tracks/7/", "tracks/8/"),
            URL.replace("/agent/", "/other/../agent/"), URL.replace("/agent/", "/%61gent/"),
            URL + "#fragment", URL + "&ticket=other-ticket", URL + "&url=https://evil.example", URL.replace("?ticket=test-ticket", ""),
            URL.replace("?ticket=test-ticket", "?ticket="), URL + "\n", "file:///C:/Windows/win.ini",
            URL.replace("music.example", "music.example\\@evil.example"),
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(mp.MusicError):
                mp.approved_media_url(CONFIG["server_url"], value, 7)
        for value in (True, -1, 0, 1.1, "7x", "9" * 5000, "٧"):
            with self.subTest(track_id=value), self.assertRaises(mp.MusicError):
                mp.approved_media_url(CONFIG["server_url"], URL, value)


class MusicPlayerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xass-music-test-")
        self.audio = FakeAudio()
        self.requests = []
        self.handler = lambda request: httpx.Response(200, content=silent_wav())
        self.release = threading.Event()
        def request_handler(request):
            self.requests.append(request)
            return self.handler(request)
        def factory(*_args, **_kwargs):
            return httpx.Client(transport=httpx.MockTransport(request_handler))
        self.player = mp.MusicPlayer(audio_factory=lambda: self.audio, client_factory=factory, cache_parent=self.directory.name)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.release.set()
        self.player.close()
        if self.player._worker is not None:
            self.player._worker.join(timeout=2)
        self.directory.cleanup()

    def command(self, command, **payload):
        return self.player.command(command, payload, CONFIG)

    def play(self, **kwargs):
        return self.command("music_play", track_id=7, url=URL, **kwargs)

    def wait_state(self, state):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = self.player.snapshot()
            if result["state"] == state:
                return result
            time.sleep(0.005)
        self.fail(f"Expected {state}, got {self.player.snapshot()}")

    def test_outputs_and_real_native_id_not_friendly_name_or_index(self):
        result = self.command("music_outputs")
        self.assertEqual(len(result["outputs"]), 3)
        self.assertEqual(result["default_output_id"], "default")
        self.play(output_id="stable-two")
        self.wait_state("playing")
        self.assertIs(self.audio.selected[-1], self.audio.ids["stable-two"])
        self.assertEqual(self.requests[0].headers["X-Api-Key"], CONFIG["api_key"])

    def test_server_relative_media_route_uses_paired_legacy_origin_not_public_url(self):
        legacy = {**CONFIG, "server_url": "http://private.example:8000/base"}
        path = "/agent/music/tracks/7/stream?ticket=test-ticket"
        self.player.command("music_play", {"track_id": 7, "url": URL, "media_path": path}, legacy)
        self.wait_state("playing")
        self.assertEqual(str(self.requests[0].url), legacy["server_url"] + path)
        for unsafe in ("//evil.example" + path, "https://evil.example" + path, "/../" + path, path + "&url=evil", "/agent/music/tracks/8/stream?ticket=test-ticket"):
            with self.subTest(path=unsafe), self.assertRaises(mp.MusicError):
                self.player.command("music_play", {"track_id": 7, "url": URL, "media_path": unsafe}, legacy)
        self.assertEqual(len(self.requests), 1)

    def test_play_pause_seek_resume_stop_and_volume(self):
        self.assertEqual(self.play(volume=50)["state"], "loading")
        self.wait_state("playing")
        device = self.audio.devices[-1]
        self.assertEqual(list(device.callback.send(2)), [500, -500] * 2)
        self.command("music_volume", volume=0)
        self.assertEqual(list(device.callback.send(2)), [0, 0] * 2)
        self.assertEqual(self.command("music_pause")["state"], "paused")
        self.assertTrue(device.closed)
        self.assertEqual(self.command("music_seek", position_sec=27)["position_sec"], 27)
        self.assertEqual(self.command("music_resume")["state"], "playing")
        self.assertEqual(self.audio.seek_positions[-1], 27)
        self.command("music_seek", position_sec=90)
        self.assertEqual(self.audio.seek_positions[-1], 90)
        saved = self.player._path
        self.assertEqual(self.command("music_stop")["state"], "stopped")
        self.assertFalse(saved.exists())
        self.assertFalse(self.audio.devices[-1].running)

    def test_play_invalid_output_does_not_download_or_interrupt_existing(self):
        self.play()
        self.wait_state("playing")
        with self.assertRaises(mp.MusicError):
            self.play(output_id="stale-output")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.player.snapshot()["state"], "playing")

    def test_disconnected_output_never_falls_back_to_other_speakers(self):
        self.play(output_id="stable-one")
        self.wait_state("playing")
        self.command("music_pause")
        self.audio.ids.pop("stable-one")
        with self.assertRaises(mp.MusicError):
            self.command("music_resume")
        self.assertEqual(len(self.audio.devices), 1)
        self.assertEqual(self.player.snapshot()["state"], "error")

    def test_invalid_numeric_values_never_reach_audio(self):
        for value in (True, "NaN", float("inf"), -1, 101, None):
            with self.subTest(volume=value), self.assertRaises(mp.MusicError):
                self.command("music_volume", volume=value)
        self.play()
        self.wait_state("playing")
        for value in (True, "NaN", float("inf"), -1, 101, None):
            with self.subTest(position=value), self.assertRaises(mp.MusicError):
                self.command("music_seek", position_sec=value)
        self.assertEqual(len(self.audio.devices), 1)

    def test_expired_ticket_and_network_errors_do_not_leak_url(self):
        self.handler = lambda request: httpx.Response(403)
        self.play()
        result = self.wait_state("error")
        self.assertIn("истекла", result["error"])
        self.assertNotIn("test-ticket", str(result))
        def timeout(request):
            raise httpx.ReadTimeout(str(request.url))
        self.handler = timeout
        self.play()
        result = self.wait_state("error")
        self.assertNotIn("test-ticket", str(result))

    def test_redirect_not_followed(self):
        self.handler = lambda request: httpx.Response(302, headers={"location": "https://evil.example/audio.mp3"})
        self.play()
        self.wait_state("error")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.audio.devices), 0)

    def test_size_limit_even_without_content_length_and_bad_format(self):
        with patch.object(mp, "MAX_DOWNLOAD_BYTES", 32):
            self.play()
            self.assertIn("256", self.wait_state("error")["error"])
        self.handler = lambda request: httpx.Response(200, content=b"<!DOCTYPE html>not music")
        self.play()
        self.assertIn("Формат", self.wait_state("error")["error"])
        self.assertEqual(len(self.audio.devices), 0)
        self.assertEqual(list(Path(self.directory.name).rglob("*.part")), [])

    def test_streaming_limit_deadline_and_incomplete_payload(self):
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"RIFF" + b"\0" * 4 + b"WAVE"
                yield b"x" * 64
        self.handler = lambda request: httpx.Response(200, stream=Stream())
        with patch.object(mp, "MAX_DOWNLOAD_BYTES", 32):
            self.play()
            self.assertIn("256", self.wait_state("error")["error"])
        with patch.object(mp, "MAX_DOWNLOAD_SECONDS", -1):
            self.play()
            self.assertIn("времени", self.wait_state("error")["error"])
        self.handler = lambda request: httpx.Response(200, headers={"content-length": "100"}, stream=Stream())
        self.play()
        self.assertIn("не полностью", self.wait_state("error")["error"])
        self.assertEqual(list(Path(self.directory.name).rglob("*.part")), [])

    def test_stop_cancels_pending_download_without_late_playback(self):
        entered = threading.Event()
        def blocked(request):
            entered.set()
            self.release.wait(2)
            return httpx.Response(200, content=silent_wav())
        self.handler = blocked
        self.play()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.command("music_stop")["state"], "stopped")
        self.release.set()
        time.sleep(0.03)
        self.assertEqual(self.player.snapshot()["state"], "stopped")
        self.assertEqual(len(self.audio.devices), 0)

    def test_rapid_play_requests_share_one_worker_and_only_newest_can_start(self):
        entered = threading.Event()
        def blocked(request):
            if len(self.requests) == 1:
                entered.set()
                self.release.wait(2)
            return httpx.Response(200, content=silent_wav())
        self.handler = blocked
        self.play()
        self.assertTrue(entered.wait(1))
        worker = self.player._worker
        self.play(output_id="stable-one")
        self.play(output_id="stable-two")
        self.assertIs(worker, self.player._worker)
        self.release.set()
        result = self.wait_state("playing")
        self.assertEqual(result["output_id"], "stable-two")
        self.assertEqual(len(self.audio.devices), 1)
        self.assertEqual(len(self.requests), 2)

    def test_end_of_stream_and_device_loss_surface_in_status(self):
        self.play()
        self.wait_state("playing")
        self.player._progress["ended"] = True
        self.assertEqual(self.player.snapshot()["state"], "ended")
        self.command("music_resume")
        self.assertEqual(self.audio.seek_positions[-1], 0)
        self.audio.devices[-1].running = False
        self.assertEqual(self.player.snapshot()["state"], "error")

    def test_no_device_or_network_for_status_before_first_play(self):
        with patch.object(self.player, "_audio_factory") as factory:
            self.assertEqual(self.command("music_status")["state"], "idle")
        factory.assert_not_called()
        self.assertIsNone(self.player._worker)
        self.assertIsNone(self.player._tempdir)


class NativeDecodeTests(unittest.TestCase):
    def test_native_windows_adapter_passes_exact_selected_endpoint_to_wasapi(self):
        try:
            import miniaudio
        except ImportError:
            self.skipTest("Install pc_client requirements for native adapter tests")
        engine = object.__new__(mp.NativeAudio)
        engine.ma = miniaudio
        endpoint = miniaudio.ffi.new("ma_device_id *")
        with patch.object(miniaudio, "PlaybackDevice") as playback:
            engine.device(endpoint)
        self.assertIs(playback.call_args.kwargs["device_id"], endpoint)
        self.assertEqual(playback.call_args.kwargs["backends"], [miniaudio.Backend.WASAPI])

    def test_real_native_callback_ends_silent_fixture_on_null_backend_only(self):
        try:
            import miniaudio
        except ImportError:
            self.skipTest("Install pc_client requirements for native adapter tests")
        class NullAudio(mp.NativeAudio):
            def __init__(self):
                self.ma = miniaudio
            def outputs(self):
                return [{"id": "default", "name": "TEST NULL OUTPUT", "is_default": True}], {"default": None}
            def device(self, _output_id):
                # NULL is explicit: never open the real Windows default output.
                return miniaudio.PlaybackDevice(backends=[miniaudio.Backend.NULL], sample_rate=mp.SAMPLE_RATE,
                                                nchannels=2, buffersize_msec=20)
        def factory(*_args, **_kwargs):
            return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=silent_wav())))
        with tempfile.TemporaryDirectory(prefix="xass-null-audio-test-") as folder:
            player = mp.MusicPlayer(audio_factory=NullAudio, client_factory=factory, cache_parent=folder)
            try:
                player.command("music_play", {"track_id": 7, "url": URL}, CONFIG)
                deadline = time.monotonic() + 2
                result = player.snapshot()
                while result["state"] not in {"ended", "error"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    result = player.snapshot()
                self.assertEqual(result["state"], "ended", result)
                self.assertAlmostEqual(result["position_sec"], 0.1, places=2)
                self.assertEqual(result["error"], "")
            finally:
                player.close()
                player._worker.join(timeout=2)

    def test_native_output_ids_stay_stable_after_reordering_and_ignore_union_padding(self):
        try:
            import miniaudio
        except ImportError:
            self.skipTest("Install pc_client requirements for native adapter tests")
        first = miniaudio.ffi.new("ma_device_id *")
        second = miniaudio.ffi.new("ma_device_id *")
        endpoint = "{0.0.0.00000000}.{test-speaker}".encode("utf-16-le") + b"\0\0"
        miniaudio.ffi.buffer(first)[:len(endpoint)] = endpoint
        miniaudio.ffi.buffer(second)[:len(endpoint)] = endpoint
        # Unused union data must not change the endpoint identity.
        miniaudio.ffi.buffer(second)[len(endpoint):len(endpoint) + 4] = b"junk"
        engine = object.__new__(mp.NativeAudio)
        engine.ma = miniaudio
        with patch.object(miniaudio, "Devices") as devices:
            devices.return_value.get_playbacks.return_value = [{"id": first, "name": "Speakers"}]
            rows, _ = engine.outputs()
            devices.return_value.get_playbacks.return_value = [{"id": second, "name": "Renamed Speakers"}]
            renamed, _ = engine.outputs()
            self.assertEqual(rows[1]["id"], renamed[1]["id"])
            self.assertTrue(rows[1]["id"].startswith("wasapi-"))
            self.assertNotIn("test-speaker", rows[1]["id"])

    def test_bundled_decoder_reads_and_seeks_own_silent_fixture_without_audio_output(self):
        try:
            import miniaudio
        except ImportError:
            self.skipTest("Install pc_client requirements for native decoder test")
        engine = object.__new__(mp.NativeAudio)
        engine.ma = miniaudio
        with tempfile.TemporaryDirectory(prefix="xass-decoder-test-") as folder:
            path = Path(folder) / "silent.wav"
            path.write_bytes(silent_wav())
            self.assertAlmostEqual(engine.duration(path), 0.1, places=3)
            stream = engine.stream(path, 0.05)
            samples = stream.send(512)
            self.assertEqual(len(samples), 1024)
            self.assertFalse(any(samples))
            stream.close()


if __name__ == "__main__":
    unittest.main()
