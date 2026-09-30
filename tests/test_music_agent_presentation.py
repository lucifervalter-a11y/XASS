import unittest
from types import SimpleNamespace

from app.services.music_agent_presentation import render_lyrics, select_stored_lyrics


class StoredAgentPresentationTests(unittest.TestCase):
    def record(self, *, owner=None, catalog=None, dismissed=False):
        return SimpleNamespace(owner_lyrics=owner or {}, result={"lyrics": catalog or {}}, dismissed=dismissed)

    def test_timed_owner_rows_render_as_lrc_for_windows_lyrics_pane(self):
        value = {"synced": True, "lines": [
            {"time": 1.25, "text": " Первая   строка "},
            {"start": 65.5, "text": "Вторая"},
        ]}
        self.assertEqual(render_lyrics(value), "[00:01.25]Первая строка\n[01:05.50]Вторая")

    def test_pc_transcript_beats_plain_catalog_but_not_synced_owner(self):
        transcript = {"lines": [{"start": 2.0, "end": 3.0, "text": "PC line"}]}
        plain = self.record(catalog={"synced": False, "text": "catalog filler"})
        self.assertEqual(select_stored_lyrics(plain, transcript, 30), "[00:02.00]PC line")
        owner = self.record(owner={"synced": True, "text": "Owner", "lines": [{"time": 1, "text": "Owner"}]},
                            catalog={"synced": True, "text": "Catalog", "lines": [{"time": 1, "text": "Catalog"}]})
        self.assertEqual(select_stored_lyrics(owner, transcript, 30), "[00:01.00]Owner")

    def test_disabled_or_dismissed_rows_never_escape_and_output_is_bounded(self):
        record = self.record(owner={"disabled": True, "text": "private draft"},
                             catalog={"synced": False, "text": "catalog"}, dismissed=True)
        self.assertEqual(select_stored_lyrics(record, None, 30), "")
        self.assertLessEqual(len(render_lyrics({"text": "x" * 20_000, "synced": False})), 8000)


if __name__ == "__main__":
    unittest.main()
