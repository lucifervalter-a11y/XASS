from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pc_client.remote_tools import delete_file, list_files


class RemoteToolsTests(unittest.TestCase):
    def test_only_allowed_roots_are_listed_and_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data = Path(directory) / "data"
            documents = home / "Documents"
            documents.mkdir(parents=True)
            target = documents / "notes.txt"
            target.write_text("hello", encoding="utf-8")
            with patch.dict(os.environ, {"USERPROFILE": str(home)}):
                listing = list_files(data, "documents", "")
                self.assertEqual(listing["entries"][0]["name"], "notes.txt")
                self.assertEqual(listing["entries"][0]["size"], 5)
                removed = delete_file(data, "documents", "notes.txt")
                self.assertEqual(removed["name"], "notes.txt")
                self.assertFalse(target.exists())

    def test_path_traversal_and_root_deletion_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data = Path(directory) / "data"
            with patch.dict(os.environ, {"USERPROFILE": str(home)}):
                for path in ("../secret.txt", "folder/../../secret.txt", "C:/Windows/win.ini"):
                    with self.subTest(path=path), self.assertRaises(ValueError):
                        list_files(data, "documents", path)
                with self.assertRaises(ValueError):
                    delete_file(data, "documents", "")


if __name__ == "__main__":
    unittest.main()
