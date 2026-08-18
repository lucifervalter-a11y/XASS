from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.agent_workspace import (
    cleanup_expired_assets,
    latest_screenshot,
    load_asset,
    normalize_remote_location,
    store_asset,
)


class AgentWorkspaceTests(unittest.TestCase):
    def settings(self, root: str) -> SimpleNamespace:
        return SimpleNamespace(
            agent_workspace_dir=root,
            agent_screenshot_max_bytes=1024,
            agent_file_max_bytes=2048,
        )

    def test_remote_location_rejects_escape_and_absolute_paths(self) -> None:
        self.assertEqual(normalize_remote_location("documents", "XASS/notes.txt"), ("documents", "XASS/notes.txt"))
        for value in ("../secret.txt", "folder/../../secret.txt", "C:/Windows/win.ini", "folder/\0bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_remote_location("documents", value)
        with self.assertRaises(ValueError):
            normalize_remote_location("c_drive", "Windows")

    def test_assets_are_source_bound_expiring_and_replace_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            first = store_asset(
                settings, source_name="PC-1", kind="screenshot", filename="first.jpg",
                content_type="image/jpeg", body=b"first", ttl_seconds=60,
            )
            second = store_asset(
                settings, source_name="PC-1", kind="screenshot", filename="second.jpg",
                content_type="image/jpeg", body=b"second", ttl_seconds=60,
            )
            self.assertIsNone(load_asset(settings, first["token"], source_name="PC-1"))
            self.assertIsNone(load_asset(settings, second["token"], source_name="PC-2"))
            loaded = load_asset(settings, second["token"], source_name="PC-1")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded[1].read_bytes(), b"second")
            self.assertEqual(latest_screenshot(settings, "PC-1")["token"], second["token"])
            self.assertEqual(cleanup_expired_assets(settings, now=float(second["expires_at"]) + 1), 1)
            self.assertFalse(any(Path(directory).iterdir()))

    def test_asset_limits_and_media_type_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            with self.assertRaises(ValueError):
                store_asset(
                    settings, source_name="PC-1", kind="screenshot", filename="screen.txt",
                    content_type="text/plain", body=b"screen",
                )
            with self.assertRaises(ValueError):
                store_asset(
                    settings, source_name="PC-1", kind="file_upload", filename="large.bin",
                    content_type="application/octet-stream", body=b"x" * 2049,
                )


if __name__ == "__main__":
    unittest.main()
