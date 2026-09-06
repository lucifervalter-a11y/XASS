from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pc_client.secret_store import cipher_label, seal_config, unseal_config


class SecretStoreTests(unittest.TestCase):
    def test_api_key_is_not_plaintext_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sealed = seal_config(
                {"server_url": "https://xass.example", "api_key": "ag_super_secret", "source_name": "PC"},
                data_dir=root,
            )
            raw = json.dumps(sealed)
            self.assertNotIn("ag_super_secret", raw)
            self.assertEqual(sealed["server_url"], "https://xass.example")
            self.assertIn(sealed["sealed"]["cipher"], {"aes-256-gcm", "dpapi"})
            opened = unseal_config(sealed, data_dir=root)
            self.assertEqual(opened["api_key"], "ag_super_secret")
            self.assertEqual(cipher_label(sealed), "AES-256-GCM" if sealed["sealed"]["cipher"] == "aes-256-gcm" else "Windows DPAPI")

    def test_plaintext_config_still_loads(self) -> None:
        opened = unseal_config({"server_url": "https://xass.example", "api_key": "plain"}, data_dir=Path("."))
        self.assertEqual(opened["api_key"], "plain")


if __name__ == "__main__":
    unittest.main()
