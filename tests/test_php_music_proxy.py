"""Real PHP streaming contract against local fixtures, never a deployed server.

Set XASS_TEST_PHP to a portable php executable when PHP is not on PATH.
The PHP files are copied unchanged except the fixture-only backend address.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx


ROOT = Path(__file__).resolve().parents[1]
AUDIO = bytes(range(256)) * 1024
TICKET = "fixture_ticket-Abc123.signed987"
ETAG = '"fixture-audio-v1"'
MODIFIED = "Mon, 07 Sep 2026 12:00:00 GMT"
OWNER_COOKIE = "xass_pwa=fixture-owner-session; Path=/; Max-Age=86400; Secure; HttpOnly; SameSite=Lax"


class AudioBackend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.respond()

    def do_HEAD(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        request_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.put({"method": self.command, "path": self.path,
                                  "headers": {name.lower(): value for name, value in self.headers.items()},
                                  "body": request_body})
        parsed = urlsplit(self.path)
        if parsed.path in {"/api/native/enrollment/verify", "/api/pwa/logout"}:
            body = json.dumps({"ok": True, "native": parsed.path.endswith("verify")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "backend_admin=fixture-never-forward; Path=/; HttpOnly")
            self.send_header("Set-Cookie", "xass_pwa_debug=fixture-never-forward-prefix; Path=/")
            cookie = OWNER_COOKIE if parsed.path.endswith("verify") else 'xass_pwa=""; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax'
            self.send_header("set-cookie", cookie)
            if parse_qs(parsed.query).get("case_variant") == ["1"]:
                self.send_header("Set-Cookie", "XASS_PWA=fixture-different-case-cookie; Path=/; HttpOnly")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if parsed.path == "/agent/installer/download":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="XASS-Setup.exe"')
            self.send_header("Content-Length", str(len(AUDIO)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(AUDIO)
            return
        if not re.fullmatch(r"/(api|agent)/music/tracks/1/stream", parsed.path) or parse_qs(parsed.query).get("ticket") != [TICKET]:
            body = json.dumps({"detail": "ticket rejected"}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if parse_qs(parsed.query).get("slow") == ["1"]:
            # The fixture pauses mid-response. A buffering proxy cannot pass
            # the first bytes to the client until this entire response ends.
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(AUDIO) * 12))
            self.end_headers()
            self.wfile.write(AUDIO * 4)
            self.wfile.flush()
            self.server.release_stream.wait(timeout=8)
            self.wfile.write(AUDIO * 8)
            self.wfile.flush()
            self.server.stream_finished.set()
            return
        start, end, status = 0, len(AUDIO) - 1, 200
        requested = self.headers.get("Range")
        if requested and self.headers.get("If-Range", ETAG) in (ETAG, MODIFIED):
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
            if match:
                start = int(match[1])
                end = min(int(match[2]) if match[2] else end, end)
            if not match or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(AUDIO)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Disposition", 'inline; filename="fixture.wav"')
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", ETAG)
        self.send_header("Last-Modified", MODIFIED)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(AUDIO)}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(AUDIO[start:end + 1])


class PhpMusicProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        php = os.environ.get("XASS_TEST_PHP") or shutil.which("php")
        if not php:
            raise unittest.SkipTest("PHP CLI is unavailable; set XASS_TEST_PHP or install php-cli in CI")
        version = subprocess.run([php, "-n", "-v"], capture_output=True, text=True, timeout=10)
        if version.returncode:
            raise RuntimeError("Configured PHP executable cannot run: " + version.stderr[:300])
        cls.temp = tempfile.TemporaryDirectory(prefix="xass-php-proxy-test-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.fixture_root = Path(cls.temp.name)
        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), AudioBackend)
        cls.backend.daemon_threads = True
        cls.backend.requests = queue.Queue()
        cls.backend.release_stream = threading.Event()
        cls.backend.stream_finished = threading.Event()
        backend_thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        backend_thread.start()

        def stop_backend():
            cls.backend.shutdown()
            cls.backend.server_close()
            backend_thread.join(timeout=3)

        cls.addClassCleanup(stop_backend)
        backend_url = f"http://127.0.0.1:{cls.backend.server_address[1]}"
        source = (ROOT / "proxy.php").read_text(encoding="utf-8")
        source, count = re.subn(r"(?m)^\$BACKEND = '[^']+';", f"$BACKEND = '{backend_url}';", source)
        if count != 1:
            raise AssertionError("Expected exactly one fixture backend substitution")
        (cls.fixture_root / "proxy.php").write_text(source, encoding="utf-8")
        shutil.copyfile(ROOT / "index.php", cls.fixture_root / "index.php")
        (cls.fixture_root / "profile.php").write_text("<?php echo 'fixture profile';", encoding="utf-8")
        (cls.fixture_root / "router.php").write_text("<?php\n"
            "$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);\n"
            "if ($path === '/proxy.php') { return false; }\n"
            "require __DIR__ . '/index.php';\n", encoding="utf-8")
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        cls.log = (cls.fixture_root / "php.log").open("wb")
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen([php, "-n", "-d", "allow_url_fopen=1", "-d", "display_errors=0",
            "-S", f"127.0.0.1:{port}", "-t", str(cls.fixture_root), str(cls.fixture_root / "router.php")],
            stdin=subprocess.DEVNULL, stdout=cls.log, stderr=cls.log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)

        def stop_php():
            if cls.process.poll() is None:
                cls.process.terminate()
                try:
                    cls.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cls.process.kill()
                    cls.process.wait(timeout=5)

        cls.addClassCleanup(stop_php)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise RuntimeError("Fixture PHP server exited: " + (cls.fixture_root / "php.log").read_text(errors="replace")[-1000:])
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    break
            except OSError:
                time.sleep(.03)
        else:
            raise RuntimeError("Fixture PHP server did not become ready")
        cls.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False)
        cls.addClassCleanup(cls.client.close)

    def setUp(self):
        while not self.backend.requests.empty():
            self.backend.requests.get_nowait()

    def media_url(self, namespace="api", *, direct=False, ticket=TICKET):
        path = f"/{namespace}/music/tracks/1/stream?" + urlencode({"ticket": ticket})
        return path if direct else "/proxy.php?" + urlencode({"_binary": "1", "_media": "1", "_p": path})

    def test_media_range_and_if_range_headers_status_and_bytes_pass_through(self):
        response = self.client.get(self.media_url(), headers={"Range": "bytes=16-47", "If-Range": ETAG})
        self.assertEqual(response.status_code, 206, response.text)
        self.assertEqual(response.content, AUDIO[16:48])
        self.assertEqual(response.headers["content-length"], "32")
        self.assertEqual(response.headers["content-range"], f"bytes 16-47/{len(AUDIO)}")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["etag"], ETAG)
        self.assertEqual(response.headers["last-modified"], MODIFIED)
        self.assertEqual(response.headers["x-xass-status"], "206")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        request = self.backend.requests.get_nowait()
        self.assertEqual(request["headers"]["range"], "bytes=16-47")
        self.assertEqual(request["headers"]["if-range"], ETAG)
        self.assertEqual(parse_qs(urlsplit(request["path"]).query)["ticket"], [TICKET])
        fallback = self.client.get(self.media_url(), headers={"Range": "bytes=16-47", "If-Range": '"obsolete"'})
        self.assertEqual(fallback.status_code, 200)
        self.assertEqual(fallback.content, AUDIO)

    def test_unsatisfiable_range_and_expired_ticket_keep_real_error_status(self):
        response = self.client.get(self.media_url(), headers={"Range": "bytes=999999999-"})
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], f"bytes */{len(AUDIO)}")
        self.assertEqual(response.content, b"")
        response = self.client.get(self.media_url(ticket="expired"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "ticket rejected"})

    def test_first_bytes_arrive_before_backend_finishes_large_media(self):
        self.backend.release_stream.clear()
        self.backend.stream_finished.clear()
        path = "/api/music/tracks/1/stream?" + urlencode({"ticket": TICKET, "slow": "1"})
        url = "/proxy.php?" + urlencode({"_binary": "1", "_media": "1", "_p": path})
        try:
            with self.client.stream("GET", url) as response:
                self.assertEqual(response.status_code, 200)
                chunks = response.iter_bytes(chunk_size=16384)
                first = next(chunks)
                self.assertEqual(first, AUDIO[:16384])
                self.assertFalse(self.backend.stream_finished.is_set(), "PHP buffered the entire media response")
                self.backend.release_stream.set()
                body = first + b"".join(chunks)
                self.assertEqual(body, AUDIO * 12)
        finally:
            self.backend.release_stream.set()

    def test_head_keeps_method_length_range_and_no_body(self):
        for direct, namespace in ((False, "api"), (True, "agent")):
            with self.subTest(direct=direct):
                response = self.client.head(self.media_url(namespace, direct=direct))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(int(response.headers["content-length"]), len(AUDIO))
                self.assertEqual(response.content, b"")
                self.assertEqual(self.backend.requests.get_nowait()["method"], "HEAD")
                response = self.client.head(self.media_url(namespace, direct=direct), headers={"Range": "bytes=20-29"})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.headers["content-length"], "10")
                self.assertEqual(response.content, b"")
                self.assertEqual(self.backend.requests.get_nowait()["method"], "HEAD")

    def test_index_front_controller_preserves_only_explicit_agent_media_ticket(self):
        response = self.client.get(self.media_url("agent", direct=True) + "&ignored=never-forward", headers={"Range": "bytes=3-7"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, AUDIO[3:8])
        request = self.backend.requests.get_nowait()
        self.assertEqual(urlsplit(request["path"]).path, "/agent/music/tracks/1/stream")
        self.assertEqual(parse_qs(urlsplit(request["path"]).query), {"ticket": [TICKET]})

    def test_media_mode_cannot_bypass_path_allowlist(self):
        for path in ("/docs", "/api/config", "/agent/installer/download", "/api/music/tracks/1/stream/extra",
                     "/api/../docs", "/api/music/tracks/0/stream", "/api/music/tracks/1/stream\x00"):
            with self.subTest(path=path):
                response = self.client.get("/proxy.php", params={"_binary": "1", "_media": "1", "_p": path})
                self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(self.backend.requests.empty())

    def test_existing_installer_and_json_envelope_contracts_do_not_change(self):
        installer = self.client.get("/agent/installer/download", headers={"Range": "bytes=0-15"})
        self.assertEqual(installer.status_code, 200)
        self.assertEqual(installer.content, AUDIO)
        self.assertIn("XASS-Setup.exe", installer.headers["content-disposition"])
        self.assertNotIn("range", self.backend.requests.get_nowait()["headers"])
        error = self.client.get("/proxy.php", params={"_p": "/api/private"})
        self.assertEqual(error.status_code, 200)
        self.assertEqual(error.json()["_s"], 401)
        self.assertEqual(json.loads(error.json()["_b"]), {"detail": "ticket rejected"})

    def test_native_enrollment_cookie_is_forwarded_as_header_not_json(self):
        payload = {"challenge_id": "fixture-enrollment", "proof": "fixture-signed-proof"}
        response = self.client.post("/proxy.php", params={"_p": "/api/native/enrollment/verify"}, json=payload,
                                    headers={"X-Telegram-Init-Data": "fixture-owner-init-data",
                                             "Cookie": "xass_pwa=fixture-old-session"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get_list("set-cookie"), [OWNER_COOKIE])
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.json()["_s"], 200)
        self.assertEqual(json.loads(response.json()["_b"]), {"ok": True, "native": True})
        self.assertNotIn("fixture-owner-session", response.text)
        self.assertNotIn("fixture-never-forward", response.text)
        self.assertNotIn("backend_admin", str(response.headers))
        self.assertNotIn("xass_pwa_debug", str(response.headers))
        request = self.backend.requests.get_nowait()
        self.assertEqual(request["method"], "POST")
        self.assertEqual(json.loads(request["body"]), payload)
        self.assertEqual(request["headers"]["x-telegram-init-data"], "fixture-owner-init-data")
        self.assertEqual(request["headers"]["cookie"], "xass_pwa=fixture-old-session")

    def test_native_envelope_forwards_exact_cookie_name_not_case_variant(self):
        response = self.client.post("/proxy.php", params={"_p": "/api/native/enrollment/verify?case_variant=1"}, json={})
        self.assertEqual(response.headers.get_list("set-cookie"), [OWNER_COOKIE])
        self.assertNotIn("fixture-different-case-cookie", response.text)

    def test_logout_cookie_expiry_survives_json_envelope(self):
        response = self.client.post("/proxy.php", params={"_p": "/api/pwa/logout"}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get_list("set-cookie"),
                         ['xass_pwa=""; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax'])
        self.assertEqual(response.json()["_s"], 200)
        self.assertNotIn("xass_pwa", response.json()["_b"])


if __name__ == "__main__":
    unittest.main()
