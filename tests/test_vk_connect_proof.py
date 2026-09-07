from __future__ import annotations

import unittest

from app.config import Settings
from app.services.pwa_auth import issue_vk_connect_proof, verify_vk_connect_proof


class VkConnectProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            bot_token="123456:secret",
            owner_user_id=42,
            setup_api_key="setup-secret-should-never-appear-in-url",
            _env_file=None,
        )

    def test_proof_roundtrip_and_chat_id(self) -> None:
        proof = issue_vk_connect_proof(self.settings, chat_id=777, now=1_700_000_000)
        self.assertNotIn(self.settings.setup_api_key, proof)
        payload = verify_vk_connect_proof(proof, self.settings, now=1_700_000_030)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["purpose"], "vk_connect")
        self.assertEqual(payload["chat_id"], 777)

    def test_expired_and_tampered_proofs_fail(self) -> None:
        proof = issue_vk_connect_proof(self.settings, now=1_700_000_000)
        self.assertIsNone(verify_vk_connect_proof(proof, self.settings, now=1_700_001_000))
        self.assertIsNone(verify_vk_connect_proof(proof + "x", self.settings, now=1_700_000_010))
        self.assertIsNone(verify_vk_connect_proof(self.settings.setup_api_key, self.settings, now=1_700_000_010))


if __name__ == "__main__":
    unittest.main()
