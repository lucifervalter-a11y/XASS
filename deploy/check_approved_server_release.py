"""Relevant combined regression suite plus backup/deployment safety tests."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PATTERNS = [
    "test_runtime_regressions.py", "test_agent_archive.py", "test_bot_identity.py",
    "test_bot_media.py", "test_notifications.py", "test_rules_store.py",
    "test_scheduler_alerts.py", "test_conversation_avatar_cache.py",
    "test_telegram_music_ingest.py", "test_music_restore_recovery.py",
    "test_music_storage.py", "test_pc_music_storage.py", "test_music_restore_ephemeral.py",
    "test_music_play_without_pc.py", "test_music_transfers.py", "test_music_transfer_diagnostics.py",
    "test_music_archive_uploads.py", "test_music_api.py", "test_predeploy_backup.py",
    "test_approved_server_release.py",
]

if __name__ == "__main__":
    suite = unittest.TestSuite()
    for pattern in PATTERNS:
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=pattern))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
