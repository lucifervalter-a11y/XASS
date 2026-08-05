from __future__ import annotations

import io
import unittest
import zipfile

from pc_client.client_update import _validate_archive, verify_manifest


class ClientUpdateTests(unittest.TestCase):
    def test_manifest_signature_rejects_tampering(self) -> None:
        manifest = {
            "version": "0.4.2",
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


if __name__ == "__main__":
    unittest.main()
