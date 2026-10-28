"""Exercise the real isolated process without importing agent/network/audio code."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


class LightweightPollingTests(unittest.TestCase):
    def test_status_polling_needs_only_stdlib_and_does_not_write_data(self):
        helper = Path(__file__).resolve().parents[1] / "bridge.py"
        with tempfile.TemporaryDirectory(prefix="xass-native-poll-") as folder:
            source = Path(folder) / "pc_client"
            data = Path(folder) / "data"
            source.mkdir()
            data.mkdir()
            # Any dependency import would fail this real process. Source
            # validation still runs, but status polling must not load them.
            for module in ("secret_store.py", "network_client.py", "music_bridge.py"):
                (source / module).write_text('raise RuntimeError("status must not import this")', encoding="utf-8")
            (data / "config.json").write_text(json.dumps({"source_name": "Fixture PC", "sealed": {"data": "do-not-read"}}))
            (data / ".agent-status.json").write_text(json.dumps({"state": "online", "updated_at": time.time()}))
            (data / "music-playback.json").write_text(json.dumps({"state": "paused", "title": "Fixture track", "track_id": 7, "volume": 12}))
            before = {path.name: path.read_bytes() for path in data.iterdir()}
            result = subprocess.run([sys.executable, "-I", "-B", str(helper), "--source", str(source), "--data", str(data)],
                                    input='{"action":"snapshot"}', text=True, capture_output=True, timeout=5, check=True)
            response = json.loads(result.stdout)
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["result"]["playback"]["volume"], 12)
            self.assertEqual(response["result"]["device"]["state"], "online")
            self.assertEqual(result.stderr, "")
            self.assertNotIn("do-not-read", result.stdout)
            self.assertEqual(before, {path.name: path.read_bytes() for path in data.iterdir()})
            self.assertFalse((source / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
