import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from app.services.server_migration import _prepare_sqlite


class RestoreEphemeralTests(unittest.TestCase):
    def test_restore_preserves_identities_but_never_replays_music_or_one_time_proofs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.db"
            with closing(sqlite3.connect(path)) as database, database:
                database.executescript("""
                    CREATE TABLE native_devices(id TEXT, public_key TEXT, revoked BOOLEAN);
                    INSERT INTO native_devices VALUES('device', 'public-only', false);
                    CREATE TABLE native_challenges(used BOOLEAN);
                    INSERT INTO native_challenges VALUES(false);
                    CREATE TABLE native_action_proofs(used BOOLEAN);
                    INSERT INTO native_action_proofs VALUES(false);
                    CREATE TABLE pwa_pair_tokens(is_active BOOLEAN);
                    INSERT INTO pwa_pair_tokens VALUES(true);
                    CREATE TABLE agent_pair_codes(is_active BOOLEAN);
                    INSERT INTO agent_pair_codes VALUES(true);
                    CREATE TABLE music_transfers(status TEXT, detail TEXT);
                    INSERT INTO music_transfers VALUES('waiting', '');
                    CREATE TABLE music_remote_commands(status TEXT, error TEXT);
                    INSERT INTO music_remote_commands VALUES('pending', '');
                    CREATE TABLE music_playback_state(transfer_id TEXT, revision INTEGER, queue_command_id INTEGER);
                    INSERT INTO music_playback_state VALUES('waiting', 5, 8);
                    CREATE TABLE music_sessions(state TEXT, session_key TEXT, share_site BOOLEAN, share_discord BOOLEAN);
                    INSERT INTO music_sessions VALUES('playing', 'old-key', true, false);
                    CREATE TABLE music_tracks(id INTEGER, title TEXT);
                    INSERT INTO music_tracks VALUES(1, 'Preserve music');
                    CREATE TABLE music_storage_jobs(status TEXT, error_code TEXT);
                    INSERT INTO music_storage_jobs VALUES('running', '');
                    CREATE TABLE music_import_runs(status TEXT);
                    INSERT INTO music_import_runs VALUES('pending');
                """)
            _prepare_sqlite(path, {})
            with closing(sqlite3.connect(path)) as database:
                for table in ("native_challenges", "native_action_proofs"):
                    self.assertEqual(database.execute(f"SELECT used FROM {table}").fetchone()[0], 1)
                for table in ("pwa_pair_tokens", "agent_pair_codes"):
                    self.assertEqual(database.execute(f"SELECT is_active FROM {table}").fetchone()[0], 0)
                self.assertEqual(database.execute("SELECT state, session_key, share_site FROM music_sessions").fetchone(), ("stopped", "", 0))
                self.assertEqual(database.execute("SELECT transfer_id, revision FROM music_playback_state").fetchone(), ("", 6))
                self.assertEqual(database.execute("SELECT status FROM music_remote_commands").fetchone()[0], "cancelled")
                self.assertEqual(database.execute("SELECT status FROM music_transfers").fetchone()[0], "failed")
                self.assertEqual(database.execute("SELECT title FROM music_tracks").fetchone()[0], "Preserve music")
                self.assertEqual(database.execute("SELECT public_key, revoked FROM native_devices").fetchone(), ("public-only", 0))
