from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import httpx

CLIENT_ROOT = Path(__file__).resolve().parents[1] / "pc_client"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import music_bridge
from music_player import MusicError


class FakePlayer:
    def __init__(self):
        self.calls = []
        self.reveal = 0
        self.listener = None
        self.state = {"state": "playing", "track_id": 7, "output_id": "default", "position_sec": 3,
                      "duration_sec": 90, "volume": 40, "title": "Песня", "artist": "Телефон", "error": "",
                      "downloaded_bytes": 10, "total_bytes": 20, "url": "http://secret.example/track"}

    def snapshot(self):
        return dict(self.state)

    def reveal_count(self):
        return self.reveal

    def set_reveal_listener(self, listener):
        self.listener = listener

    def command(self, command, payload, config):
        self.calls.append((command, payload, config))
        if command == "music_pause" and self.state["state"] == "idle":
            raise MusicError("Сначала запустите трек и дождитесь загрузки")
        self.state["state"] = "paused" if command == "music_pause" else self.state["state"]
        return self.snapshot()


class MusicBridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xass-bridge-test-")
        self.root = Path(self.directory.name)
        self.player = FakePlayer()
        self.assertTrue(music_bridge.start_music_bridge(self.player, self.root))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        music_bridge.stop_music_bridge()
        self.directory.cleanup()

    def token_and_port(self):
        payload = json.loads((self.root / music_bridge.BRIDGE_NAME).read_text(encoding="utf-8"))
        return payload["token"], int(payload["port"])

    def test_status_file_has_the_track_and_no_media_url(self):
        music_bridge.stop_music_bridge()
        self.player.reveal = 4
        music_bridge.write_playback(self.player, self.root)
        status = json.loads((self.root / music_bridge.STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["reveal"], 4)
        self.assertEqual(status["title"], "Песня")
        self.assertEqual(status["track_id"], 7)
        self.assertNotIn("url", status)
        self.assertNotIn("token", json.dumps(status))

    def test_loopback_pause_reaches_the_agent_player(self):
        token, port = self.token_and_port()
        response = httpx.post(
            f"http://127.0.0.1:{port}/command",
            headers={music_bridge.HEADER: token, "Content-Type": "application/json"},
            content=b'{"command":"music_pause","url":"http://evil.example"}',
            trust_env=False, follow_redirects=False, timeout=2,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.player.calls, [("music_pause", {}, {})])
        self.assertTrue(response.json()["ok"])

    def test_missing_token_play_command_and_oversize_body_are_refused(self):
        token, port = self.token_and_port()
        url = f"http://127.0.0.1:{port}/command"
        missing = httpx.post(url, json={"command": "music_pause"}, trust_env=False, follow_redirects=False, timeout=2)
        self.assertEqual(missing.status_code, 404)
        play = httpx.post(url, headers={music_bridge.HEADER: token}, json={"command": "music_play", "url": "http://evil.example"},
                          trust_env=False, follow_redirects=False, timeout=2)
        self.assertEqual(play.status_code, 404)
        huge = httpx.post(url, headers={music_bridge.HEADER: token}, content=b"{" * 5000,
                          trust_env=False, follow_redirects=False, timeout=2)
        self.assertEqual(huge.status_code, 413)
        self.assertEqual(self.player.calls, [])

    def test_gui_helper_posts_only_to_loopback(self):
        result = music_bridge.send_command("music_volume", {"volume": 15, "lan_url": "http://evil.example"}, root=self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(self.player.calls[-1][0], "music_volume")
        self.assertEqual(self.player.calls[-1][1], {"volume": 15})
        with self.assertRaises(MusicError):
            music_bridge.send_command("music_play", root=self.root)

    def test_lyrics_travel_in_status_and_cover_stays_a_local_jpeg(self):
        import io
        from PIL import Image
        token, port = self.token_and_port()
        response = httpx.post(
            f"http://127.0.0.1:{port}/command",
            headers={music_bridge.HEADER: token, "Content-Type": "application/json"},
            content=b'{"command":"music_status"}',
            trust_env=False, follow_redirects=False, timeout=2,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("lyrics", response.json()["player"])
        music_bridge.stop_music_bridge()
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "#c495f4").save(buffer, format="JPEG")
        picture = buffer.getvalue()
        self.player.presentation = lambda: {"lyrics": "Куплет\nПрипев", "artwork": picture, "url": "http://secret.example/art"}
        music_bridge.write_playback(self.player, self.root)
        status = json.loads((self.root / music_bridge.STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["lyrics"], "Куплет\nПрипев")
        self.assertEqual(status["cover_sha256"], music_bridge.hashlib.sha256(picture).hexdigest())
        self.assertNotIn("artwork", status)
        self.assertNotIn("url", status)
        self.assertNotIn("token", json.dumps(status))
        self.assertEqual(music_bridge.read_cover(self.root), picture)
        self.player.presentation = lambda: {"lyrics": "е" * 9000, "artwork": b"GIF89a-not-a-jpeg"}
        music_bridge.write_playback(self.player, self.root)
        status = json.loads((self.root / music_bridge.STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(len(status["lyrics"]), 8000)
        self.assertEqual(status["cover_sha256"], "")
        self.assertEqual(music_bridge.read_cover(self.root), b"")

    def test_cover_is_published_before_status_and_only_matching_digest_is_read(self):
        import io
        from PIL import Image

        def jpeg(color: str) -> bytes:
            buffer = io.BytesIO()
            Image.new("RGB", (9, 7), color).save(buffer, format="JPEG", quality=91)
            return buffer.getvalue()

        first, second = jpeg("#829cff"), jpeg("#c495f4")
        self.player.presentation = lambda: {"lyrics": "", "artwork": first}
        events = []
        real_cover = music_bridge._write_cover
        real_status = music_bridge.atomic_write_json

        def write_cover(*args, **kwargs):
            events.append("cover")
            return real_cover(*args, **kwargs)

        def write_status(*args, **kwargs):
            events.append("status")
            return real_status(*args, **kwargs)

        with patch.object(music_bridge, "_write_cover", side_effect=write_cover), patch.object(
            music_bridge, "atomic_write_json", side_effect=write_status
        ):
            music_bridge.write_playback(self.player, self.root)
        self.assertEqual(events, ["cover", "status"])

        old_status = music_bridge.read_playback(self.root)
        old_digest = old_status["cover_sha256"]
        self.assertEqual(music_bridge.read_cover(self.root, expected_sha256=old_digest), first)

        # This is the short publish window between the new cover and new status.
        second_digest = music_bridge.hashlib.sha256(second).hexdigest()
        music_bridge._write_cover(self.root, second, second_digest)
        self.assertEqual(music_bridge.read_cover(self.root, expected_sha256=old_digest), b"")
        self.assertEqual(music_bridge.read_cover(self.root, expected_sha256=second_digest), second)
        self.assertEqual(music_bridge.read_cover(self.root, expected_sha256="not-a-digest"), b"")

    def test_cover_writer_does_not_reuse_the_old_predictable_temporary_name(self):
        import io
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "#829cff").save(buffer, format="JPEG")
        picture = buffer.getvalue()
        legacy = self.root / (
            f".{music_bridge.COVER_NAME}.{music_bridge.os.getpid()}."
            f"{music_bridge.threading.get_ident()}.tmp"
        )
        legacy.write_bytes(b"must-not-be-opened-or-replaced")

        digest = music_bridge.hashlib.sha256(picture).hexdigest()
        music_bridge._write_cover(self.root, picture, digest)

        self.assertEqual(legacy.read_bytes(), b"must-not-be-opened-or-replaced")
        self.assertEqual(music_bridge.read_cover(self.root, expected_sha256=digest), picture)


if __name__ == "__main__":
    unittest.main()
