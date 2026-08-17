from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


class PasskeyCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_can_delete_lost_credential(self) -> None:
        credential = SimpleNamespace(id=7, owner_user_id=42, name="Lost iPhone")
        session = SimpleNamespace(
            scalar=AsyncMock(return_value=credential),
            delete=AsyncMock(),
            commit=AsyncMock(),
        )
        deleted = await passkeys.delete_credential(session, owner_user_id=42, credential_id=7)
        self.assertIs(deleted, credential)
        session.delete.assert_awaited_once_with(credential)
        session.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
