from __future__ import annotations

import unittest

from app.services.message_logging import merge_delete_event


class DeleteEventMergeTests(unittest.TestCase):
    def test_sparse_delete_keeps_original_text_and_media(self) -> None:
        original = {
            "message_id": 10,
            "text": "сохранённый текст",
            "document": {"file_id": "doc", "file_name": "note.pdf"},
        }
        payload = {
            "chat": {"id": 5, "type": "private"},
            "message_ids": [10],
            "business_connection_id": "bc-1",
        }

        result = merge_delete_event(original, payload)

        self.assertEqual(result["text"], "сохранённый текст")
        self.assertEqual(result["document"]["file_name"], "note.pdf")
        self.assertEqual(result["_xass_delete"], payload)
        self.assertNotIn("_xass_delete", original)

    def test_repeated_delete_replaces_only_delete_metadata(self) -> None:
        original = {
            "message_id": 10,
            "caption": "подпись",
            "_xass_delete": {"message_ids": [9]},
        }
        payload = {"chat": {"id": 5}, "message_ids": [10], "business_connection_id": "bc-2"}

        result = merge_delete_event(original, payload)

        self.assertEqual(result["caption"], "подпись")
        self.assertEqual(result["_xass_delete"], payload)

    def test_tombstone_without_original_message_keeps_delete_payload(self) -> None:
        payload = {"chat": {"id": 5}, "message_ids": [10], "business_connection_id": "bc-3"}

        self.assertEqual(merge_delete_event({}, payload), payload)
        self.assertEqual(merge_delete_event(None, payload), payload)


if __name__ == "__main__":
    unittest.main()
