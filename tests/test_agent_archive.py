from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pc_client import archive_store


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, _size: int):
        yield b"image-data"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeClient:
    def __init__(self) -> None:
        self.headers = {}

    def stream(self, *_args, **_kwargs):
        self.headers = dict(_kwargs.get("headers") or {})
        return FakeResponse()


class FailedResponse(FakeResponse):
    def raise_for_status(self) -> None:
        raise RuntimeError("temporary media failure")


class FailingClient(FakeClient):
    def stream(self, *_args, **_kwargs):
        return FailedResponse()


class AgentArchiveTests(unittest.TestCase):
    def test_events_are_written_to_local_sqlite_and_media_folder(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            config = {"server_url": "https://xass.example", "source_name": "Home PC", "archive_folder": raw_root, "archive_enabled": True}
            payload = {
                "archive_enabled": True,
                "archive_events": [
                    {
                        "event_id": 10,
                        "event": "create",
                        "message_id": 4,
                        "telegram_message_id": 99,
                        "chat_id": 42,
                        "chat_type": "private",
                        "chat_title": "Alice",
                        "from_user_id": 7,
                        "from_username": "alice",
                        "direction": "incoming",
                        "text": "hello",
                        "deleted": False,
                        "message_date": "2026-08-12T10:00:00+00:00",
                        "event_date": "2026-08-12T10:00:01+00:00",
                        "media": [
                            {
                                "id": 8,
                                "type": "photo",
                                "mime_type": "image/jpeg",
                                "file_name": "photo.jpg",
                                "download_path": "/agent/archive/media/8",
                            }
                        ],
                    }
                ],
            }
            client = FakeClient()
            result = archive_store.apply_archive_events(config, payload, client=client, headers={"X-Api-Key": "secret"})
            self.assertEqual(result["saved"], 1)
            self.assertEqual(result["cursor"], 10)
            self.assertEqual(archive_store.archive_cursor(config), 10)
            rows = archive_store.conversation_rows(config)
            self.assertEqual(rows[0]["text_content"], "hello")
            self.assertEqual(rows[0]["media_count"], 1)
            self.assertEqual(client.headers["X-XASS-Source"], "Home PC")
            self.assertTrue(any((Path(raw_root) / "media").rglob("*.jpg")))

    def test_media_failure_keeps_cursor_and_publishes_retry_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            config = {"server_url": "https://xass.example", "source_name": "Home PC", "archive_folder": raw_root, "archive_enabled": True}
            payload = {
                "archive_enabled": True,
                "archive_events": [{
                    "event_id": 11,
                    "event": "create",
                    "message_id": 5,
                    "telegram_message_id": 100,
                    "chat_id": 42,
                    "chat_type": "private",
                    "chat_title": "Alice",
                    "text": "photo",
                    "media": [{"id": 9, "type": "photo", "mime_type": "image/jpeg", "file_name": "retry.jpg", "download_path": "/agent/archive/media/9"}],
                }],
            }
            result = archive_store.apply_archive_events(config, payload, client=FailingClient(), headers={})
            status = archive_store.archive_status(config)
            self.assertEqual(result["cursor"], 0)
            self.assertTrue(status["pending_retry"])
            self.assertEqual(status["errors"], 1)
            self.assertIn("temporary media failure", status["last_error"])

            recovered = archive_store.apply_archive_events(config, payload, client=FakeClient(), headers={})
            self.assertEqual(recovered["cursor"], 11)
            self.assertFalse(archive_store.archive_status(config)["pending_retry"])


if __name__ == "__main__":
    unittest.main()
