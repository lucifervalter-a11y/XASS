from __future__ import annotations

import unittest

from pc_client.e2e_crypto import generate_keypair, is_sealed_blob, seal_bytes, seal_text, unseal_bytes, unseal_text


class E2ECryptoTests(unittest.TestCase):
    def test_owner_and_agent_share_the_same_secret(self) -> None:
        owner_priv, owner_pub = generate_keypair()
        agent_priv, agent_pub = generate_keypair()
        photo = b"\xff\xd8fake-jpeg"
        sealed = seal_bytes(photo, private_jwk=agent_priv, peer_public_jwk=owner_pub, aad=b"screenshot")
        self.assertTrue(is_sealed_blob(sealed))
        self.assertNotIn(b"fake-jpeg", sealed)
        self.assertEqual(
            unseal_bytes(sealed, private_jwk=owner_priv, peer_public_jwk=agent_pub, aad=b"screenshot"),
            photo,
        )

    def test_clipboard_envelope_round_trip(self) -> None:
        owner_priv, owner_pub = generate_keypair()
        agent_priv, agent_pub = generate_keypair()
        envelope = seal_text("секрет из буфера", private_jwk=agent_priv, peer_public_jwk=owner_pub, aad="clipboard")
        self.assertTrue(envelope["sealed"])
        self.assertNotIn("секрет", envelope["blob"])
        self.assertEqual(
            unseal_text(envelope, private_jwk=owner_priv, peer_public_jwk=agent_pub, aad="clipboard"),
            "секрет из буфера",
        )


if __name__ == "__main__":
    unittest.main()
