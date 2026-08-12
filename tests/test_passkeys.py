from __future__ import annotations

import time
import unittest

from app.services import passkeys


class PasskeyTransactionTests(unittest.TestCase):
    def tearDown(self) -> None:
        passkeys._pending.clear()

    def test_registration_transaction_is_bound_to_owner(self) -> None:
        token = passkeys._transaction(b"challenge", 42, "xass.example", "https://xass.example", "register")
        self.assertEqual(passkeys.transaction_owner(token, "register"), 42)
        with self.assertRaises(ValueError):
            passkeys.transaction_owner(token, "login")

    def test_expired_transaction_is_rejected(self) -> None:
        token = passkeys._transaction(b"challenge", 42, "xass.example", "https://xass.example", "register")
        passkeys._pending[token].expires_at = time.time() - 1
        with self.assertRaises(ValueError):
            passkeys.transaction_owner(token, "register")


if __name__ == "__main__":
    unittest.main()
