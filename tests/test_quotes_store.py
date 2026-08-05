import tempfile
import unittest
from pathlib import Path

from app.services.quotes_store import add_quote, delete_quote, load_quotes, save_quotes, update_quote


class QuotesStoreTests(unittest.TestCase):
    def test_update_preserves_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "quotes.json"
            saved = save_quotes(path, [{"id": "a", "text": "Первая"}, {"id": "b", "text": "Вторая"}])
            self.assertEqual([item["id"] for item in saved], ["a", "b"])
            updated = update_quote(path, "a", "Новая первая")
            self.assertIsNotNone(updated)
            self.assertEqual([item["text"] for item in load_quotes(path)], ["Новая первая", "Вторая"])

    def test_add_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "quotes.json"
            save_quotes(path, [])
            added = add_quote(path, "Точная цитата")
            self.assertIsNotNone(added)
            self.assertTrue(delete_quote(path, str(added["id"])))
            self.assertEqual(load_quotes(path), [])


if __name__ == "__main__":
    unittest.main()
