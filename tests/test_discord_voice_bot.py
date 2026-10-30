import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.discord_voice_bot import _load_env_file, _resolve_sqlite_path, find_track, format_duration


class DiscordVoiceBotTests(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(0), "—")
        self.assertEqual(format_duration(None), "—")
        self.assertEqual(format_duration(45), "0:45")
        self.assertEqual(format_duration(125), "2:05")
        self.assertEqual(format_duration(3600), "60:00")

    def test_load_env_file(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as f:
            f.write("# Comment\nDISCORD_BOT_TOKEN=my-secret-token\nOTHER_KEY='hello'\n\n")
            path = Path(f.name)
        try:
            env = _load_env_file(path)
            self.assertEqual(env.get("DISCORD_BOT_TOKEN"), "my-secret-token")
            self.assertEqual(env.get("OTHER_KEY"), "hello")
        finally:
            path.unlink(missing_ok=True)

    def test_find_track_returns_match(self):
        tmpdir = tempfile.mkdtemp()
        tmp = Path(tmpdir)
        try:
            db_path = tmp / "test.db"
            music_root = tmp / "music"
            music_root.mkdir()

            track_file = music_root / "audio_123.mp3"
            track_file.write_bytes(b"dummy-audio-bytes")

            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE music_tracks (
                        id INTEGER PRIMARY KEY,
                        title TEXT,
                        artist TEXT,
                        album TEXT,
                        storage_name TEXT,
                        duration REAL,
                        size INTEGER,
                        mime TEXT,
                        favorite INTEGER DEFAULT 0,
                        deleted INTEGER DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO music_tracks (id, title, artist, album, storage_name, duration, size, mime, favorite, deleted)
                    VALUES (1, 'Midnight City', 'M83', 'Hurry Up', 'audio_123.mp3', 243.0, 1024, 'audio/mpeg', 1, 0)
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with patch("deploy.discord_voice_bot.DB_PATH", db_path), patch("deploy.discord_voice_bot.MUSIC_ROOT", music_root):
                found = find_track("midnight")
                self.assertIsNotNone(found)
                self.assertEqual(found["title"], "Midnight City")
                self.assertEqual(found["artist"], "M83")
                self.assertEqual(found["file_path"], str(track_file))

                # Test non-matching track
                not_found = find_track("nonexistent song")
                self.assertIsNone(not_found)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
