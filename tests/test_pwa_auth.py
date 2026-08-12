from __future__ import annotations

import hashlib
import hmac
import time
import unittest

from app.config import Settings
from app.services.pwa_auth import (
    authenticate_session,
    authenticate_telegram_login,
    issue_session,
    issue_action_proof,
    verify_action_proof,
)


def signed_login(bot_token: str, user_id: int, *, auth_date: int | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": user_id,
        "first_name": "Owner",
        "last_name": "",
        "username": "owner",
        "photo_url": "",
        "auth_date": auth_date or int(time.time()),
    }
    check = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
    secret = hashlib.sha256(bot_token.encode("utf-8")).digest()
    payload["hash"] = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return payload


class PwaAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            bot_token="123456:secret",
            owner_user_id=42,
            setup_api_key="setup-secret",
            _env_file=None,
        )

    def test_owner_login_issues_working_session(self) -> None:
        user = authenticate_telegram_login(signed_login(self.settings.bot_token, 42), self.settings)
        self.assertIsNotNone(user)
        token = issue_session(user, self.settings, now=100)
        restored = authenticate_session(token, self.settings, now=101)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.user_id, 42)
        self.assertTrue(restored.is_owner)

    def test_non_owner_is_rejected(self) -> None:
        user = authenticate_telegram_login(signed_login(self.settings.bot_token, 99), self.settings)
        self.assertIsNone(user)

    def test_optional_widget_fields_may_be_absent(self) -> None:
        payload = signed_login(self.settings.bot_token, 42)
        for key in ("last_name", "username", "photo_url"):
            payload.pop(key)
        check = "\n".join(f"{key}={payload[key]}" for key in sorted(payload) if key != "hash")
        secret = hashlib.sha256(self.settings.bot_token.encode("utf-8")).digest()
        payload["hash"] = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
        user = authenticate_telegram_login(payload, self.settings)
        self.assertIsNotNone(user)
        self.assertEqual(user.username, "")

    def test_tampered_and_expired_login_are_rejected(self) -> None:
        payload = signed_login(self.settings.bot_token, 42)
        payload["first_name"] = "Changed"
        self.assertIsNone(authenticate_telegram_login(payload, self.settings))
        old = signed_login(self.settings.bot_token, 42, auth_date=int(time.time()) - 3600)
        self.assertIsNone(authenticate_telegram_login(old, self.settings))

    def test_session_is_bound_to_owner_and_secret(self) -> None:
        user = authenticate_telegram_login(signed_login(self.settings.bot_token, 42), self.settings)
        token = issue_session(user, self.settings, now=100)
        changed_owner = Settings(
            bot_token=self.settings.bot_token,
            owner_user_id=7,
            setup_api_key=self.settings.setup_api_key,
            _env_file=None,
        )
        self.assertIsNone(authenticate_session(token, changed_owner, now=101))
        self.assertIsNone(authenticate_session(token + "x", self.settings, now=101))

    def test_action_proof_is_short_lived_and_purpose_bound(self) -> None:
        proof = issue_action_proof(42, "agent:lock:Home PC", self.settings, now=100)
        self.assertTrue(verify_action_proof(proof, 42, "agent:lock:Home PC", self.settings, now=101))
        self.assertFalse(verify_action_proof(proof, 42, "agent:restart:Home PC", self.settings, now=101))
        self.assertFalse(verify_action_proof(proof, 42, "agent:lock:Home PC", self.settings, now=400))


if __name__ == "__main__":
    unittest.main()
