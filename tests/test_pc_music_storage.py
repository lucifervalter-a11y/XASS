from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from pc_client.music_storage import (StorageError, store_root, stored_path, transfer, selected_directory, import_directory)


class AgentMusicStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp.name)
        self.config = {"server_url": "https://xass.example", "api_key": "ag_fixture-key"}
        self.root = store_root(self.data_root, self.config)
        self.data = b"audio-fixture" * 80
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.job = {"id": "a" * 32, "operation": "replicate", "sha256": self.digest, "size": len(self.data), "offset": 0}

    def tearDown(self):
        self.temp.cleanup()

    def test_replication_verifies_bytes_and_ack_only_after_durable_file(self):
        offsets, ack = [], []
        partial = self.root / (self.job["id"] + ".part")
        partial.write_bytes(self.data[:30])
        def handler(request):
            if request.url.path.endswith("/chunk"):
                offset = int(request.url.params["offset"]); offsets.append(offset)
                return httpx.Response(200, content=self.data[offset:])
            self.assertEqual(stored_path(self.root, self.digest).read_bytes(), self.data)
            ack.append(json.loads(request.content)); return httpx.Response(200, json={"ok": True})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            transfer(client, self.config["server_url"], self.root, self.job)
            transfer(client, self.config["server_url"], self.root, self.job)
        self.assertEqual(offsets, [30])
        self.assertEqual(len(ack), 2)
        self.assertEqual(ack[0], {"sha256": self.digest, "size": len(self.data)})

    def test_bad_hash_and_oversize_never_ack_or_replace_replica(self):
        for value in (b"x" * len(self.data), b"x" * (512 * 1024 + 1)):
            posted = []
            def handler(request):
                if request.method == "POST": posted.append(request.url.path)
                return httpx.Response(200, content=value)
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(StorageError):
                    transfer(client, self.config["server_url"], self.root, self.job)
            self.assertFalse(stored_path(self.root, self.digest).exists())
            self.assertEqual(posted, [])

    def test_restore_resumes_at_server_offset_and_preserves_local_copy(self):
        path = stored_path(self.root, self.digest); path.write_bytes(self.data)
        received = bytearray(self.data[:19]); posted = []
        def handler(request):
            if request.method == "PUT":
                self.assertEqual(int(request.url.params["offset"]), len(received)); received.extend(request.content)
                return httpx.Response(200, json={"offset": len(received)})
            posted.append(request.url.path); return httpx.Response(200, json={"ok": True})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            transfer(client, self.config["server_url"], self.root, {**self.job, "operation": "restore", "offset": 19})
        self.assertEqual(bytes(received), self.data)
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(posted, ["/agent/music-storage/jobs/" + self.job["id"] + "/finish"])

    def test_missing_copy_reports_unavailable_without_fake_completion(self):
        calls = []
        with httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r.url.path) or httpx.Response(200)))) as client:
            transfer(client, self.config["server_url"], self.root, {**self.job, "operation": "restore"})
        self.assertEqual(calls, ["/agent/music-storage/jobs/" + self.job["id"] + "/unavailable"])

    def test_opaque_paths_reject_traversal_and_namespaces_separate_servers_and_credentials(self):
        for value in ("../file", "A" * 64, "a" * 63):
            with self.assertRaises(StorageError): stored_path(self.root, value)
        self.assertNotEqual(self.root, store_root(self.data_root, {**self.config, "api_key": "ag_different"}))
        self.assertNotEqual(self.root, store_root(self.data_root, {**self.config, "server_url": "https://other.example"}))

    def test_only_explicit_authorized_folder_is_scanned_and_originals_remain(self):
        folder = self.data_root / "XASS Files" / "Музыка"; folder.mkdir(parents=True)
        first = folder / "a.wav"; second = folder / "b.mp3"
        first.write_bytes(b"first"); second.write_bytes(b"second")
        (folder / "readme.txt").write_text("not music")
        outside = self.data_root / "private.wav"; outside.write_bytes(b"never scanned")
        registered, content, finish = [], {}, []
        def handler(request):
            if request.url.path.endswith("/files"):
                payload = json.loads(request.content); registered.append(payload)
                file_id = str(len(registered)).zfill(32)
                return httpx.Response(200, json={"file_id": file_id, "offset": 0, "track_id": None})
            if request.method == "PUT":
                content[request.url.path] = request.content
                return httpx.Response(200, json={"offset": len(request.content)})
            if request.url.path.endswith("/finish"):
                if request.content: finish.append(json.loads(request.content))
                return httpx.Response(200, json={"ok": True})
            self.fail("Unexpected request")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            import_directory(client, self.config["server_url"], self.data_root,
                             {"id": "a" * 32, "root": "xass_files", "path": "Музыка"})
        self.assertEqual([r["filename"] for r in registered], ["a.wav", "b.mp3"])
        self.assertEqual(set(content.values()), {b"first", b"second"})
        self.assertEqual(finish[0]["skipped"], 1)
        self.assertEqual(first.read_bytes(), b"first"); self.assertEqual(outside.read_bytes(), b"never scanned")
        for path in ("../", "/", "C:/Windows", "Music/../../Documents"):
            with self.assertRaises(StorageError): selected_directory(self.data_root, "xass_files", path)

    def test_incremental_duplicate_requires_no_audio_upload(self):
        folder = self.data_root / "XASS Files"; folder.mkdir()
        (folder / "a.mp3").write_bytes(self.data)
        calls = []
        def handler(request):
            calls.append(request.method)
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json={"file_id": "b" * 32, "offset": len(self.data), "track_id": 7})
            return httpx.Response(200, json={"ok": True})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            import_directory(client, self.config["server_url"], self.data_root,
                             {"id": "a" * 32, "root": "xass_files", "path": ""})
        self.assertNotIn("PUT", calls)

    def test_symlink_selection_and_replica_are_rejected(self):
        source = self.data_root / "source"; source.mkdir()
        source_file = source / "music.wav"; source_file.write_bytes(self.data)
        link = self.data_root / "XASS Files"
        try:
            link.symlink_to(source, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks requires Windows developer/admin capability")
        with self.assertRaises(StorageError): selected_directory(self.data_root, "xass_files", "")
        stored_path(self.root, self.digest).symlink_to(source_file)
        with self.assertRaises(StorageError): stored_path(self.root, self.digest)
