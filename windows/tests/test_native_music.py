"""Native music parity tests. Fake audio; never touch a user's player/device."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from array import array
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "windows"), str(ROOT / "pc_client")]
import desktop_music_service as service
import music_player as mp
from native_music_ownership import AudioOwnership, FileLock


class FakeDevice:
    def __init__(self): self.running = False
    def start(self, stream): self.running = True
    def close(self): self.running = False


class FakeAudio:
    def __init__(self): self.devices = []; self.entered = None; self.release = None
    def duration(self, path):
        if self.entered:
            self.entered.set(); self.release.wait(3)
        return 100.0
    def outputs(self): return [{"id": "default", "name": "test"}], {"default": None}
    def device(self, output):
        device = FakeDevice(); self.devices.append(device); return device
    def stream(self, path, position):
        def samples():
            count = yield array("h")
            while True: count = yield array("h", [0, 0] * count)
        stream = samples(); next(stream); return stream


class MusicServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {"XASS_DATA_ROOT": str(self.root)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.audio = FakeAudio(); self.player = mp.MusicPlayer(audio_factory=lambda: self.audio)
        self.service = service.MusicService(ROOT / "pc_client", self.root, player=self.player)
        self.addCleanup(self.cleanup)
    def cleanup(self):
        if self.audio.release: self.audio.release.set()
        self.service.close(); self.temp.cleanup()
    def file(self, name):
        path = self.root / name; suffix = path.suffix.lower()
        path.write_bytes({".wav": b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * 40,
                          ".mp3": b"ID3" + b"\0" * 50, ".flac": b"fLaC" + b"\0" * 50,
                          ".ogg": b"OggS" + b"\0" * 50}[suffix]); return path
    def request(self, action, **extra): return self.service.request({"version": 1, "id": 1, "action": action, **extra})
    def wait_state(self, state):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            result = self.request("snapshot")
            if result["state"] == state: return result
            time.sleep(0.01)
        self.fail(f"Player did not become {state}: {result}")
    def cast(self, state="playing", **extra):
        (self.root / "music-playback.json").write_text(json.dumps({"state": state, "track_id": 7, "title": "Сервер", "position_sec": 0, "duration_sec": 100, "volume": 50, **extra}), encoding="utf-8")

    def test_offline_open_multiple_formats_history_wrap_missing_and_originals(self):
        files = [self.file(f"трек{i}.{extension}") for i, extension in enumerate(("wav", "mp3", "flac", "ogg"))]
        self.request("open", paths=list(map(str, files))); self.wait_state("playing")
        self.assertFalse((self.root / "config.json").exists())
        self.assertEqual(json.loads((self.root / "local-music.json").read_text()), list(map(str, files)))
        self.request("previous"); self.wait_state("playing"); self.assertEqual(self.service.current, 3)
        files[0].unlink(); self.request("next"); self.wait_state("playing"); self.assertEqual(self.service.current, 1)
        self.request("stop"); self.assertTrue(all(p.exists() for p in files[1:]))
        self.service.close()
        self.service = service.MusicService(ROOT / "pc_client", self.root, player=mp.MusicPlayer(audio_factory=lambda: self.audio))
        self.assertEqual(len(self.service.queue), 4)
        self.assertFalse(self.request("snapshot")["queue"][0]["exists"])

    def test_rapid_next_uses_one_decoder_and_latest_selection(self):
        self.audio.entered = threading.Event(); self.audio.release = threading.Event()
        paths = [str(self.file(f"{i}.wav")) for i in range(3)]
        self.request("open", paths=paths); self.assertTrue(self.audio.entered.wait(1))
        opener = self.service.opener
        for _ in range(25): self.request("next")
        self.assertIs(opener, self.service.opener)
        self.assertEqual(self.service.current, 1)
        self.audio.release.set(); self.assertEqual(self.wait_state("playing")["title"], "1")

    def test_oversize_file_rejected_without_queue_mutation(self):
        path = self.file("oversize.wav")
        with path.open("r+b") as stream: stream.truncate(mp.MAX_DOWNLOAD_BYTES + 1)
        with self.assertRaises(ValueError): self.request("open", paths=[str(path)])
        self.assertEqual(self.service.queue, [])

    def test_native_host_closes_during_pending_decode(self):
        self.audio.entered = threading.Event(); self.audio.release = threading.Event()
        self.request("open", paths=[str(self.file("a.wav"))]); self.assertTrue(self.audio.entered.wait(1))
        self.service.close(); self.audio.release.set(); self.service.opener.join(1)
        self.assertFalse(self.service.opener.is_alive())
        self.assertFalse(any(device.running for device in self.audio.devices))

    def test_pause_seek_resume_ended_replay_and_volume_extremes(self):
        self.request("open", paths=[str(self.file("a.wav"))]); self.wait_state("playing")
        self.request("pause"); self.request("seek", position=23); self.assertEqual(self.request("snapshot")["state"], "paused")
        self.assertEqual(self.request("snapshot")["position"], 23)
        for volume in (0, 100): self.assertEqual(self.request("volume", volume=volume)["volume"], volume)
        self.request("resume"); self.player._progress["ended"] = True
        self.assertEqual(self.request("snapshot")["state"], "ended")
        self.assertEqual(self.request("resume")["state"], "playing")
        self.assertEqual(self.request("snapshot")["position"], 0)

    def test_invalid_paths_signatures_empty_and_symlink_ancestors(self):
        path = self.file("a.wav")
        invalid = ["https://example/a.wav", "relative.wav", str(self.root / "missing.wav")]
        bad = self.root / "bad.wav"; bad.write_bytes(b"ID3" + b"\0" * 40); invalid.append(str(bad))
        empty = self.root / "empty.wav"; empty.touch(); invalid.append(str(empty))
        alias = self.root / "alias.wav"; alias.symlink_to(path); invalid.append(str(alias))
        folder = self.root / "linked"; folder.symlink_to(self.root, target_is_directory=True); invalid.append(str(folder / "a.wav"))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError): self.request("open", paths=[value])
        self.assertEqual(self.service.queue, [])

    def test_strict_schema_nonfinite_unknown_fields_and_queue_limit(self):
        for request in ({"version": 2, "id": 1, "action": "snapshot"}, {"version": True, "id": 1, "action": "snapshot"}, {"version": 1, "id": True, "action": "snapshot"},
                        {"version": 1, "id": 1, "action": "snapshot", "url": "https://evil"}):
            with self.assertRaises(ValueError): self.service.request(request)
        for bad in (float("nan"), float("inf"), -1, 101, True):
            with self.assertRaises(ValueError): self.request("volume", volume=bad)
        with self.assertRaises(ValueError): self.request("open", paths=["a"] * 41)

    def test_cancel_during_decode_cannot_start_late_audio(self):
        self.audio.entered = threading.Event(); self.audio.release = threading.Event()
        self.request("open", paths=[str(self.file("a.wav"))]); self.assertTrue(self.audio.entered.wait(1))
        self.request("stop"); self.audio.release.set(); time.sleep(0.06)
        self.assertEqual(self.request("snapshot")["state"], "stopped")
        self.assertFalse(any(device.running for device in self.audio.devices))

    def test_new_local_open_cancels_old_without_stopping_new_track(self):
        self.audio.entered = threading.Event(); self.audio.release = threading.Event()
        first, second = self.file("first.wav"), self.file("second.wav")
        self.request("open", paths=[str(first), str(second)]); self.assertTrue(self.audio.entered.wait(1))
        self.request("local_play", index=1); self.audio.release.set()
        result = self.wait_state("playing"); self.assertEqual(result["title"], "second")
        time.sleep(0.05); self.assertEqual(self.request("snapshot")["state"], "playing")

    def test_cast_priority_proves_local_silent_before_cast_lock(self):
        self.request("open", paths=[str(self.file("a.wav"))]); self.wait_state("playing")
        cast = AudioOwnership(self.root, local=False)
        try:
            cast.acquire()
            self.assertFalse(any(device.running for device in self.audio.devices))
            self.assertFalse(self.request("snapshot")["local_available"])
            with self.assertRaises(ValueError): self.request("next")
        finally: cast.release()
        self.assertTrue(self.request("snapshot")["local_available"])

    def test_cast_cancels_inflight_decode(self):
        self.audio.entered = threading.Event(); self.audio.release = threading.Event()
        self.request("open", paths=[str(self.file("a.wav"))]); self.assertTrue(self.audio.entered.wait(1))
        cast = AudioOwnership(self.root, local=False); cast.acquire()
        try:
            self.cast(); self.audio.release.set(); time.sleep(0.1)
            self.assertFalse(any(device.running for device in self.audio.devices))
            self.assertEqual(self.request("snapshot")["owner"], "agent")
        finally: cast.release()

    def test_cast_response_is_authoritative_and_error_reset_supported(self):
        self.cast("error", error="https://secret.invalid?ticket=SECRET", reveal=5)
        self.assertNotIn("SECRET", json.dumps(self.request("snapshot")))
        with patch("music_bridge.send_command", return_value={"ok": True, "player": {"state": "stopped", "track_id": 7}}) as send:
            result = self.request("reset")
        send.assert_called_once_with("music_stop", {}, root=self.root)
        self.assertTrue(result["local_available"])
        self.assertEqual(result["owner"], "local")

    def test_artwork_digest_and_bounded_timed_lyrics(self):
        from PIL import Image
        encoded = io.BytesIO(); Image.new("RGB", (20, 20), "red").save(encoded, format="JPEG"); artwork = encoded.getvalue()
        (self.root / "music-cover.jpg").write_bytes(artwork)
        self.cast(cover_sha256=hashlib.sha256(artwork).hexdigest(), lyrics="[00:01.00]Один\n[00:02.00]Два\n")
        snap = self.request("snapshot"); self.assertEqual(snap["lyric_rows"][1], {"time": 2.0, "text": "Два"})
        self.assertEqual(hashlib.sha256(base64.b64decode(snap["cover"])).hexdigest(), snap["cover_sha256"])
        self.cast(cover_sha256="0" * 64, lyrics="x" * 9000)
        snap = self.request("snapshot"); self.assertEqual(snap["cover"], ""); self.assertEqual(len(snap["lyrics"]), 8000)

    def test_stale_cast_does_not_own_local_or_reveal_private_fields(self):
        self.cast(api_key="ag_SECRET", url="https://secret", reveal=3)
        os.utime(self.root / "music-playback.json", (time.time() - 20,) * 2)
        snap = self.request("snapshot"); self.assertEqual(snap["owner"], "local"); self.assertEqual(snap["reveal"], 0)
        self.assertNotIn("SECRET", json.dumps(snap))

    def test_second_native_host_is_rejected(self):
        with self.assertRaises(ValueError): service.MusicService(ROOT / "pc_client", self.root)


class ProtocolAndOwnershipTests(unittest.TestCase):
    def test_process_eof_closes_and_requests_do_not_restart_host(self):
        with tempfile.TemporaryDirectory() as temp:
            process = subprocess.Popen([sys.executable, "-I", "-B", str(ROOT / "windows/desktop_music_service.py"), "--source", str(ROOT / "pc_client"), "--data", temp], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                for identifier in (1, 2):
                    process.stdin.write(json.dumps({"version": 1, "id": identifier, "action": "snapshot"}) + "\n"); process.stdin.flush()
                    response = json.loads(process.stdout.readline()); self.assertTrue(response["ok"]); self.assertEqual(response["id"], identifier)
                process.stdin.close(); self.assertEqual(process.wait(timeout=5), 0)
                lock = FileLock(Path(temp) / "native-music/host.lock"); self.assertTrue(lock.acquire()); lock.release()
            finally:
                if process.poll() is None: process.kill(); process.wait()
                process.stdout.close(); process.stderr.close()

    def test_crashed_process_releases_audio_and_priority_locks(self):
        with tempfile.TemporaryDirectory() as temp:
            source = "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from native_music_ownership import AudioOwnership; a=AudioOwnership(Path(sys.argv[2]),local=False); a.acquire(); print('locked',flush=True); sys.stdin.read()"
            process = subprocess.Popen([sys.executable, "-I", "-c", source, str(ROOT / "pc_client"), temp], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                local = AudioOwnership(Path(temp), local=True)
                self.assertTrue(local.cast_pending())
                with self.assertRaises(ValueError): local.acquire()
                process.kill(); process.wait(3)
                self.assertFalse(local.cast_pending()); local.acquire(); local.release()
            finally:
                if process.poll() is None: process.kill(); process.wait()
                process.stdin.close(); process.stdout.close()

    def test_native_ui_and_process_lifecycle_contracts(self):
        ui = (ROOT / "windows/Xass.Native/MainWindow.DesktopMusic.cs").read_text()
        client = (ROOT / "windows/Xass.Native/Services/MusicClient.cs").read_text()
        self.assertIn("PickMultipleFilesAsync", ui)
        self.assertIn("AddSeconds(30)", ui); self.assertIn("AddSeconds(120)", ui)
        self.assertIn("musicPendingCatalog", ui); self.assertIn("ScrollIntoView", ui)
        self.assertIn("SHA256.HashData", ui); self.assertIn("AppWindow.Show()", ui)
        self.assertIn("VirtualKey.Home", ui); self.assertIn("VirtualKey.Space", ui)
        self.assertIn('ArgumentList.Add("desktop-music")', client)
        self.assertNotIn('ArgumentList.Add("agent-bridge")', client)
        self.assertIn("2 * 1024 * 1024", client)

    def test_lyrics_offset_multiple_timestamps_and_plain(self):
        self.assertEqual(service.lyrics_rows("[offset:-500]\n[00:01.00][00:02.00]Привет"), [{"time": .5, "text": "Привет"}, {"time": 1.5, "text": "Привет"}])
        self.assertEqual(service.lyrics_rows("Простой текст"), [{"time": None, "text": "Простой текст"}])

    def test_player_cancellation_before_generation_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.wav"; path.write_bytes(b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * 40)
            player = mp.MusicPlayer(audio_factory=FakeAudio)
            try:
                self.assertEqual(player.play_local(path, cancelled=lambda: True)["state"], "idle")
                self.assertIsNone(player._audio)
            finally: player.close()


if __name__ == "__main__": unittest.main()
