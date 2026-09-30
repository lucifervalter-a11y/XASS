from __future__ import annotations

import io
import hashlib
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
ART_URL = "https://music.example/agent/music/tracks/7/artwork?ticket=test-ticket"


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

    def test_artwork_requires_exact_paired_route_and_the_audio_ticket(self):
        self.assertEqual(mp.approved_artwork_url(CONFIG["server_url"], ART_URL, 7, URL), ART_URL)
        nested = "https://music.example/api/agent/music/tracks/7/artwork?ticket=test-ticket"
        media = "https://music.example/api/agent/music/tracks/7/stream?ticket=test-ticket"
        self.assertEqual(mp.approved_artwork_url("https://music.example/api", nested, 7, media), nested)
        for value in (
            ART_URL.replace("artwork", "other"),
            ART_URL.replace("tracks/7", "tracks/8"),
            ART_URL.replace("test-ticket", "other-ticket"),
            ART_URL.replace("music.example", "evil.example"),
            ART_URL + "&ticket=second",
            ART_URL + "#fragment",
        ):
            with self.subTest(value=value), self.assertRaises(mp.MusicError):
                mp.approved_artwork_url(CONFIG["server_url"], value, 7, URL)


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

    def test_embedded_lyrics_and_cover_stay_out_of_the_heartbeat_snapshot(self):
        import base64
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        lyrics = "Припев\nВторая строка"
        uslt = b"\x03rus\x00" + lyrics.encode("utf-8")
        uslt = b"USLT" + len(uslt).to_bytes(4, "big") + b"\x00\x00" + uslt
        apic = b"\x00image/png\x00\x03\x00" + png
        apic = b"APIC" + len(apic).to_bytes(4, "big") + b"\x00\x00" + apic
        frames = uslt + apic
        sync = bytes(((len(frames) >> 21) & 0x7F, (len(frames) >> 14) & 0x7F, (len(frames) >> 7) & 0x7F, len(frames) & 0x7F))
        path = Path(self.directory.name) / "night.mp3"
        path.write_bytes(b"ID3" + bytes((3, 0, 0)) + sync + frames + b"\xff\xfb\x90\x00")
        snapshot = self.player.play_local(path, title="Ночь", artist="red!")
        self.assertEqual(snapshot["state"], "playing")
        self.assertNotIn("lyrics", snapshot)
        self.assertNotIn("artwork", snapshot)
        notes = self.player.presentation()
        self.assertEqual(notes["lyrics"], lyrics)
        self.assertTrue(notes["artwork"].startswith(b"\xff\xd8\xff"))
        self.assertEqual(self.player.command("music_stop", {}, CONFIG)["state"], "stopped")
        self.assertEqual(self.player.presentation(), {"lyrics": "", "artwork": b""})
        self.assertTrue(path.is_file())

    def test_stored_server_lyrics_and_same_ticket_artwork_override_local_empty_metadata(self):
        from PIL import Image
        encoded = io.BytesIO()
        Image.new("RGB", (4, 4), (35, 80, 170)).save(encoded, format="JPEG")
        picture = encoded.getvalue()

        def handler(request):
            if request.url.path.endswith("/artwork"):
                return httpx.Response(200, content=picture, headers={"Content-Type": "image/jpeg"})
            return httpx.Response(200, content=silent_wav())

        self.handler = handler
        self.play(lyrics="[00:01.25]Первая строка\n[00:04.00]Вторая",
                  artwork_path="/agent/music/tracks/7/artwork?ticket=test-ticket")
        self.wait_state("playing")
        deadline = time.monotonic() + 1
        while not self.player.presentation()["artwork"] and time.monotonic() < deadline:
            time.sleep(0.005)
        notes = self.player.presentation()
        self.assertEqual(notes["lyrics"], "[00:01.25]Первая строка\n[00:04.00]Вторая")
        self.assertTrue(notes["artwork"].startswith(b"\xff\xd8\xff"))
        self.assertEqual([request.url.path for request in self.requests],
                         ["/agent/music/tracks/7/stream", "/agent/music/tracks/7/artwork"])
        self.assertEqual(self.requests[-1].headers["X-Api-Key"], CONFIG["api_key"])
        self.assertNotIn("lyrics", self.player.snapshot())
        self.assertNotIn("artwork", self.player.snapshot())

    def test_invalid_server_presentation_never_replaces_embedded_fallback(self):
        def handler(request):
            if request.url.path.endswith("/artwork"):
                return httpx.Response(200, content=b"not-a-jpeg", headers={"Content-Type": "image/jpeg"})
            return httpx.Response(200, content=silent_wav())

        self.handler = handler
        self.play(lyrics="bad\x00lyrics", artwork_path="/agent/music/tracks/7/artwork?ticket=test-ticket")
        self.wait_state("playing")
        deadline = time.monotonic() + 1
        while len(self.requests) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.player.presentation(), {"lyrics": "", "artwork": b""})

    def test_artwork_with_a_different_ticket_is_rejected_before_network(self):
        with self.assertRaises(mp.MusicError):
            self.play(artwork_path="/agent/music/tracks/7/artwork?ticket=another-ticket")
        self.assertEqual(self.requests, [])

    def test_id3v23_extended_header_keeps_embedded_lyrics(self):
        lyrics = "Строка после расширенного заголовка"
        uslt_data = b"\x03rus\x00" + lyrics.encode("utf-8")
        frame = b"USLT" + len(uslt_data).to_bytes(4, "big") + b"\x00\x00" + uslt_data
        # v2.3 size is six bytes *after* this size field: flags + padding size.
        extended = (6).to_bytes(4, "big") + b"\x00\x00" + (0).to_bytes(4, "big")
        body = extended + frame
        sync = bytes(((len(body) >> 21) & 0x7F, (len(body) >> 14) & 0x7F,
                      (len(body) >> 7) & 0x7F, len(body) & 0x7F))
        path = Path(self.directory.name) / "extended.mp3"
        path.write_bytes(b"ID3" + bytes((3, 0, 0x40)) + sync + body + b"\xff\xfb\x90\x00")
        self.player.play_local(path)
        self.assertEqual(self.player.presentation()["lyrics"], lyrics)

    def test_ogg_comment_packet_split_across_pages_keeps_lyrics(self):
        lyrics = "Длинный комментарий Vorbis"
        vendor = b"x" * 240
        comment = b"LYRICS=" + lyrics.encode("utf-8")
        packet = (b"\x03vorbis" + len(vendor).to_bytes(4, "little") + vendor
                  + (1).to_bytes(4, "little") + len(comment).to_bytes(4, "little") + comment + b"\x01")

        def page(sequence, header_type, chunks):
            table = bytes(len(chunk) for chunk in chunks)
            header = (b"OggS\x00" + bytes((header_type,)) + b"\x00" * 8
                      + b"XASS" + int(sequence).to_bytes(4, "little") + b"\x00" * 4 + bytes((len(chunks),)))
            return header + table + b"".join(chunks)

        first, second = packet[:255], packet[255:]
        blob = page(0, 0, [first]) + page(1, 1, [second])
        self.assertEqual(mp._ogg_notes(blob)[0], lyrics)

    def test_flac_comment_and_rejected_picture_url_do_not_break_playback(self):
        comment = "LYRICS=Только текст".encode()
        vendor = b"xass"
        block = len(vendor).to_bytes(4, "little") + vendor + (1).to_bytes(4, "little") + len(comment).to_bytes(4, "little") + comment
        path = Path(self.directory.name) / "voice.flac"
        path.write_bytes(b"fLaC" + bytes([0x84]) + len(block).to_bytes(3, "big") + block)
        self.assertEqual(self.player.play_local(path)["title"], "voice")
        self.assertEqual(self.player.presentation()["lyrics"], "Только текст")
        self.assertEqual(self.player.presentation()["artwork"], b"")
        self.assertEqual(mp._apic_bytes(b"\x00-->\x00\x03\x00http://evil.example/a.jpg"), b"")

    def test_local_file_plays_without_network_and_stop_keeps_it(self):
        path = Path(self.directory.name) / "night.wav"
        path.write_bytes(silent_wav())
        snapshot = self.player.play_local(path, title="Ночь", artist="red!")
        self.assertEqual(snapshot["state"], "playing")
        self.assertEqual(snapshot["title"], "Ночь")
        self.assertEqual(snapshot["artist"], "red!")
        self.assertEqual(snapshot["media_transport"], "local_file")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.player.command("music_pause", {}, CONFIG)["state"], "paused")
        self.assertEqual(self.player.command("music_stop", {}, CONFIG)["state"], "stopped")
        self.assertTrue(path.is_file())
        with self.assertRaises(mp.MusicError):
            self.player.play_local(path.with_suffix(".txt"))

    def test_pause_on_idle_stopped_or_error_confirms_silence_without_raising(self):
        for state in ("idle", "stopped", "error"):
            with self.subTest(state=state):
                self.player._state = state
                self.player._error = "old"
                paused = self.command("music_pause")
                self.assertEqual(paused["state"], "stopped")
                self.assertEqual(paused["error"], "")
        with self.assertRaises(mp.MusicError):
            self.command("music_resume")
        with self.assertRaises(mp.MusicError):
            self.command("music_seek", position_sec=1)

    def test_pause_during_download_cancels_worker_and_preserves_handoff_position(self):
        downloading = threading.Event()
        def slow_response(request):
            downloading.set()
            self.release.wait(timeout=2)
            return httpx.Response(200, content=silent_wav())
        self.handler = slow_response
        self.assertEqual(self.play(position_sec=27)["state"], "loading")
        self.assertTrue(downloading.wait(timeout=1))
        paused = self.command("music_pause")
        self.assertEqual(paused["state"], "stopped")
        self.assertEqual(paused["position_sec"], 27)
        self.release.set()
        self.player.close()
        self.player._worker.join(timeout=2)
        self.assertEqual(self.audio.devices, [], "cancelled download must not start audio")

    def test_local_validation_reads_only_header_and_rejects_oversize_before_read(self):
        path = Path(self.directory.name) / "local.wav"
        path.write_bytes(silent_wav())
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            self.assertEqual(mp._local_audio_file(path), path.resolve())
        with patch.object(mp, "MAX_DOWNLOAD_BYTES", 8), patch.object(Path, "open") as opened:
            with self.assertRaises(mp.MusicError):
                mp._local_audio_file(path)
            opened.assert_not_called()

    def test_stop_during_local_decode_never_starts_late_audio(self):
        path = Path(self.directory.name) / "local.wav"
        path.write_bytes(silent_wav())
        started, release = threading.Event(), threading.Event()
        def duration(_path):
            started.set()
            release.wait(2)
            return 100
        self.audio.duration = duration
        worker = threading.Thread(target=lambda: self.player.play_local(path))
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            self.command("music_stop")
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.audio.devices, [])
        self.assertTrue(path.is_file())

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


    def test_private_lan_download_does_not_touch_the_server_or_send_the_api_key(self):
        self.play(lan_url=f"http://192.168.1.20:8765/xass-lan/7?token={'a' * 43}",
                  sha256=hashlib.sha256(silent_wav()).hexdigest())
        self.wait_state("playing")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.host, "192.168.1.20")
        self.assertEqual(self.requests[0].url.path, "/xass-lan/7")
        self.assertIsNone(self.requests[0].headers.get("X-Api-Key"))
        self.assertEqual(self.player.snapshot()["media_transport"], "lan")

    def test_lan_failure_falls_back_to_the_paired_server(self):
        def handler(request):
            if request.url.host == "192.168.1.20":
                return httpx.Response(503)
            return httpx.Response(200, content=silent_wav())
        self.handler = handler
        self.play(lan_url=f"http://192.168.1.20:8765/xass-lan/7?token={'a' * 43}",
                  sha256=hashlib.sha256(silent_wav()).hexdigest())
        self.wait_state("playing")
        self.assertEqual([item.url.host for item in self.requests], ["192.168.1.20", "music.example"])
        self.assertEqual(self.requests[1].headers.get("X-Api-Key"), CONFIG["api_key"])
        self.assertIsNone(self.requests[0].headers.get("X-Api-Key"))
        self.assertEqual(self.player.snapshot()["media_transport"], "server_fallback")

    def test_cancelled_lan_download_does_not_fall_back(self):
        entered = threading.Event()
        def blocked(request):
            entered.set()
            self.release.wait(2)
            return httpx.Response(200, content=silent_wav())
        self.handler = blocked
        self.play(lan_url=f"http://192.168.1.20:8765/xass-lan/7?token={'a' * 43}",
                  sha256=hashlib.sha256(silent_wav()).hexdigest())
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.command("music_stop")["state"], "stopped")
        self.release.set()
        time.sleep(0.05)
        self.assertEqual(self.player.snapshot()["state"], "stopped")
        self.assertEqual({request.url.host for request in self.requests}, {"192.168.1.20"})
        self.assertEqual(len(self.audio.devices), 0)

    def test_lan_checksum_mismatch_falls_back_to_verified_server_bytes(self):
        trusted = silent_wav()
        substituted = bytearray(trusted)
        substituted[-1] = 1

        def handler(request):
            content = bytes(substituted) if request.url.host == "192.168.1.20" else trusted
            return httpx.Response(200, content=content)

        self.handler = handler
        self.play(lan_url=f"http://192.168.1.20:8765/xass-lan/7?token={'a' * 43}",
                  sha256=hashlib.sha256(trusted).hexdigest())
        self.wait_state("playing")
        self.assertEqual([item.url.host for item in self.requests], ["192.168.1.20", "music.example"])
        self.assertIsNone(self.requests[0].headers.get("X-Api-Key"))
        self.assertEqual(self.requests[1].headers.get("X-Api-Key"), CONFIG["api_key"])
        self.assertEqual(self.player.snapshot()["media_transport"], "server_fallback")

    def test_server_checksum_mismatch_is_terminal_and_never_starts_audio(self):
        self.play(sha256="0" * 64)
        result = self.wait_state("error")
        self.assertIn("целостности", result["error"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.audio.devices, [])

    def test_lan_without_trusted_checksum_is_ignored_for_legacy_server_playback(self):
        self.play(lan_url=f"http://192.168.1.20:8765/xass-lan/7?token={'a' * 43}")
        self.wait_state("playing")
        self.assertEqual([item.url.host for item in self.requests], ["music.example"])
        self.assertEqual(self.player.snapshot()["media_transport"], "server")

    def test_malformed_checksum_is_rejected_before_network_or_playback(self):
        with self.assertRaises(mp.MusicError):
            self.play(sha256="not-a-sha", lan_url=f"http://192.168.1.20:8765/xass-lan/7?token={'a' * 43}")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.audio.devices, [])

    def test_public_and_rewritten_lan_urls_are_rejected_before_any_request(self):
        lan = f"http://192.168.1.20:8765/xass-lan/7?token={'a' * 43}"
        bad = [
            lan.replace("http://", "https://"),
            lan.replace("192.168.1.20", "8.8.8.8"),
            lan.replace("192.168.1.20", "127.0.0.1"),
            lan.replace("192.168.1.20", "169.254.1.1"),
            lan.replace(":8765", ":80"),
            lan.replace("http://", "http://user@"),
            lan.replace("/xass-lan/7", "/other/7"),
            lan.replace("/xass-lan/7", "/xass-lan/8"),
            lan + "&extra=1",
            lan + "#fragment",
            "http://192.168.1.20:8765/xass-lan/7",
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(mp.MusicError):
                self.play(lan_url=value)
        self.assertEqual(self.requests, [])


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
