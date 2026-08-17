from __future__ import annotations

import hashlib
import hmac
import io
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pc_client.client_update as client_update
from pc_client.client_update import _cache_busted_url, _validate_archive, _write_stream_with_progress, download_update, verify_manifest


class ClientUpdateTests(unittest.TestCase):
    def test_download_progress_uses_real_content_length(self) -> None:
        class Response:
            headers = {"content-length": "10"}

            @staticmethod
            def iter_bytes():
                yield b"1234"
                yield b"567890"

        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "package.bin"
            updates: list[str] = []
            _write_stream_with_progress(Response(), destination, label="Скачивание обновления", progress=updates.append)
            self.assertEqual(destination.read_bytes(), b"1234567890")
            self.assertEqual(updates[0], "Скачивание обновления 0%")
            self.assertEqual(updates[-1], "Скачивание обновления 100%")

    def test_agent_status_round_trip_is_process_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            status_path = Path(raw_root) / ".agent-status.json"
            with patch.object(client_update, "AGENT_STATUS_PATH", status_path):
                client_update.write_agent_status(
                    "online",
                    detail="heartbeat ok",
                    server_time="2026-08-06T12:00:00Z",
                    process_id=4242,
                )
                payload = client_update.load_agent_status()
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["state"], "online")
            self.assertEqual(payload["process_id"], 4242)
            self.assertEqual(payload["server_time"], "2026-08-06T12:00:00Z")

    def test_manifest_signature_rejects_tampering(self) -> None:
        manifest = {
            "version": "0.4.3",
            "revision": "a" * 64,
            "sha256": "b" * 64,
            "url": "https://xass.example/agent/update/package",
            "signature": "0" * 64,
        }
        self.assertFalse(verify_manifest(manifest, "agent-secret"))

    def test_archive_rejects_windows_path_traversal(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("..\\outside.txt", "nope")
        payload.seek(0)
        with zipfile.ZipFile(payload, "r") as archive:
            with self.assertRaisesRegex(RuntimeError, "unsafe path"):
                _validate_archive(archive)

    def test_retry_url_preserves_signed_update_location(self) -> None:
        url = _cache_busted_url("https://xass.example/agent/update/package/rev.zip?channel=stable", "abc123")
        self.assertEqual(
            url,
            "https://xass.example/agent/update/package/rev.zip?channel=stable&download=abc123",
        )

    def test_parallel_downloads_use_isolated_staging_directories(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("version.json", '{"version":"0.4.3"}')
        package = payload.getvalue()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(package)))
                self.end_headers()
                self.wfile.write(package)

            def log_message(self, _format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            revision = "a" * 64
            sha256 = hashlib.sha256(package).hexdigest()
            url = f"http://127.0.0.1:{server.server_port}/agent/update/package/{revision}.zip"
            message = f"0.4.3\n{revision}\n{sha256}\n{url}".encode("utf-8")
            api_key = "agent-secret"
            manifest = {
                "version": "0.4.3",
                "revision": revision,
                "sha256": sha256,
                "url": url,
                "signature": hmac.new(api_key.encode("utf-8"), message, hashlib.sha256).hexdigest(),
            }
            with tempfile.TemporaryDirectory() as update_root:
                with patch.object(client_update, "UPDATE_ROOT", Path(update_root)):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        stages = list(pool.map(lambda _: download_update(manifest, api_key=api_key), range(2)))
                self.assertNotEqual(stages[0].parent, stages[1].parent)
                for stage in stages:
                    self.assertEqual((stage / "version.json").read_text(encoding="utf-8"), '{"version":"0.4.3"}')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
