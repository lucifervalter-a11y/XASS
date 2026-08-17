from __future__ import annotations

import unittest

from app.main import _backup_profile_snapshot
from app.services.backup_crypto import decrypt_backup, encrypt_backup


class EncryptedBackupTests(unittest.TestCase):
    def test_encrypted_backup_round_trip_and_wrong_password(self) -> None:
        bundle = {
            "format": "xass-config",
            "version": 1,
            "profile": {"name": "XASS"},
            "projects": [],
        }
        encrypted = encrypt_backup(bundle, "correct horse battery")
        self.assertEqual(encrypted["format"], "xass-config-encrypted")
        self.assertNotIn("XASS", encrypted["data"])
        self.assertEqual(decrypt_backup(encrypted, "correct horse battery"), bundle)
        with self.assertRaisesRegex(ValueError, "Неверный пароль"):
            decrypt_backup(encrypted, "wrong password")

    def test_profile_export_never_contains_runtime_credentials(self) -> None:
        snapshot = _backup_profile_snapshot(
            {
                "name": "Owner",
                "vk_access_token": "vk-secret",
                "iphone_hook_key": "iphone-secret",
                "avatar_url": "/data/avatars/a.jpg",
            }
        )
        self.assertEqual(snapshot["name"], "Owner")
        self.assertNotIn("vk_access_token", snapshot)
        self.assertNotIn("iphone_hook_key", snapshot)


if __name__ == "__main__":
    unittest.main()
