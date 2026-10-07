from contextlib import closing
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/approved_server_release.py"
spec = importlib.util.spec_from_file_location("approved_release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ApprovedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        (self.root / "service.py").write_bytes(b"old code\n")
        (self.root / "requirements.txt").write_bytes(b"unchanged\n")
        self.base = self.commit("baseline")
        (self.root / "service.py").write_bytes(b"fixed code\n")
        (self.root / "regression.py").write_bytes(b"regression coverage\n")
        self.target = self.commit("fix")
        self.manifest = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                         for name in ("service.py", "regression.py")}
        self.git("checkout", "-q", self.base)
        (self.root / ".env").write_bytes(b"PRIVATE_SENTINEL=yes\n")
        (self.root / "media").mkdir()
        (self.root / "media/song.flac").write_bytes(b"live media")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              check=True, capture_output=True).stdout.decode().strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def validate(self, **overrides):
        arguments = dict(root=self.root, base=self.base, release=self.target, files=self.manifest)
        arguments.update(overrides)
        release.validate_release(**arguments)

    def test_reviewed_release_is_accepted(self):
        self.validate()

    def test_wrong_parent_is_rejected(self):
        with self.assertRaisesRegex(release.Stop, "single parent"):
            self.validate(base="0" * 40)

    def test_extra_changed_file_is_rejected(self):
        with self.assertRaisesRegex(release.Stop, "outside the approved manifest"):
            self.validate(files={"service.py": self.manifest["service.py"]})

    def test_content_mismatch_is_rejected(self):
        with self.assertRaisesRegex(release.Stop, "content differs"):
            self.validate(files={**self.manifest, "service.py": "0" * 64})

    def test_untracked_collision_is_rejected_and_preserved(self):
        collision = self.root / "regression.py"
        collision.write_bytes(b"user work")
        with self.assertRaisesRegex(release.Stop, "collides"):
            self.validate()
        self.assertEqual(collision.read_bytes(), b"user work")

    def test_rollback_restores_code_and_preserves_new_runtime_data(self):
        self.git("merge", "--ff-only", self.target)
        (self.root / "media/new-message").write_bytes(b"arrived after backup")
        release.rollback_code(self.root, self.base, self.target)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual((self.root / "service.py").read_bytes(), b"old code\n")
        self.assertFalse((self.root / "regression.py").exists())
        self.assertEqual((self.root / "media/new-message").read_bytes(), b"arrived after backup")
        self.assertEqual((self.root / ".env").read_bytes(), b"PRIVATE_SENTINEL=yes\n")
        release.rollback_code(self.root, self.base, self.target)  # Idempotent retry.

    def test_rollback_rejects_local_code_edits(self):
        self.git("merge", "--ff-only", self.target)
        (self.root / "service.py").write_bytes(b"concurrent operator edit")
        with self.assertRaisesRegex(release.Stop, "Tracked checkout changes"):
            release.rollback_code(self.root, self.base, self.target)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.target)
        self.assertEqual((self.root / "service.py").read_bytes(), b"concurrent operator edit")

    def test_rollback_rejects_unrelated_revision(self):
        # Keep fixture secrets and runtime data untracked.
        self.git("merge", "--ff-only", self.target)
        (self.root / "service.py").write_bytes(b"another release")
        self.git("add", "service.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "later release")
        newer = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(release.Stop, "another revision"):
            release.rollback_code(self.root, self.base, self.target)
        self.assertEqual(self.git("rev-parse", "HEAD"), newer)

    def backup(self):
        destination = Path(self.temp.name) / "backup"
        destination.mkdir(mode=0o700)
        (destination / ".env").write_bytes((self.root / ".env").read_bytes())
        (destination / "COMPLETE.json").write_text(json.dumps({"revision": self.base, "database": "sqlite"}))
        self.git("archive", "--format=tar.gz", "-o", str(destination / "code.tar.gz"), self.base)
        database = destination / "database"
        database.mkdir(mode=0o700)
        with closing(sqlite3.connect(database / "sqlite.db")) as connection:
            connection.execute("CREATE TABLE messages(value TEXT)")
            connection.execute("INSERT INTO messages VALUES ('backed up')")
            connection.commit()
        for path in destination.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        return destination

    def test_backup_verifies_code_config_and_database(self):
        checked = release.verify_backup(self.backup(), self.root, self.base)
        self.assertEqual(checked["tracked_blobs_verified"], 2)
        self.assertTrue(checked["database_integrity"])

    def test_backup_from_other_revision_is_rejected(self):
        with self.assertRaisesRegex(release.Stop, "Backup revision differs"):
            release.verify_backup(self.backup(), self.root, self.target)

    def test_changed_configuration_copy_is_rejected(self):
        destination = self.backup()
        (destination / ".env").write_bytes(b"different private settings")
        with self.assertRaisesRegex(release.Stop, "Configuration backup differs"):
            release.verify_backup(destination, self.root, self.base)

    def test_wrong_code_archive_is_rejected(self):
        destination = self.backup()
        self.git("archive", "--format=tar.gz", "-o", str(destination / "code.tar.gz"), self.target)
        with self.assertRaises(release.Stop):
            release.verify_backup(destination, self.root, self.base)

    def test_corrupt_database_is_rejected(self):
        destination = self.backup()
        (destination / "database/sqlite.db").write_bytes(b"corrupt database")
        with self.assertRaises(sqlite3.DatabaseError):
            release.verify_backup(destination, self.root, self.base)


class PublicApiChecksTests(unittest.TestCase):
    def response(self, request, *, broken_auth=False, invalid_json=False, **_kwargs):
        url = urlparse(request.full_url)
        public = url.hostname == "redvps.site"
        if public and url.path != "/health":
            self.assertEqual(url.path, "/proxy.php")
            path = parse_qs(url.query)["_p"][0]
        else:
            path = url.path
        status = 401 if path in {"/api/mini/music/storage", "/api/mini/weather"} else 200
        if public and broken_auth and status == 401:
            status = 200
        data = {"status": "ok"} if path == "/health" else {"ok": status == 200}
        payload = json.dumps(data)
        if public and url.path == "/proxy.php":
            payload = "<html>profile</html>" if invalid_json else json.dumps({"_s": status, "_b": payload})
            status = 200
        if status >= 400:
            raise release.urllib.error.HTTPError(request.full_url, status, "Unauthorized", {}, None)
        response = io.BytesIO(payload.encode())
        response.status = status
        response.url = request.full_url
        return response

    def test_public_api_uses_frontend_proxy_and_checks_inner_status(self):
        with patch.object(release.urllib.request, "urlopen", side_effect=self.response):
            checked = release.api_checks(8000)
        self.assertEqual(len(checked), 8)
        self.assertEqual(checked["public_ping"], 200)
        self.assertEqual(checked["public_music_auth"], 401)
        self.assertEqual(checked["public_weather_auth"], 401)

    def test_proxy_http_200_does_not_hide_broken_authentication(self):
        with patch.object(release.urllib.request, "urlopen", side_effect=lambda request, **kw: self.response(request, broken_auth=True)):
            with self.assertRaisesRegex(release.Stop, "not rejecting unauthenticated"):
                release.api_checks(8000)

    def test_public_html_is_rejected(self):
        with patch.object(release.urllib.request, "urlopen", side_effect=lambda request, **kw: self.response(request, invalid_json=True)):
            with self.assertRaisesRegex(release.Stop, "invalid JSON"):
                release.api_checks(8000)


if __name__ == "__main__":
    unittest.main()
