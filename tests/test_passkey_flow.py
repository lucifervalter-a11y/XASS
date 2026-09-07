from __future__ import annotations

import hashlib
import json
import unittest
import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import app.main as main
from app.db import Base
from app.services import passkeys
from app.services.miniapp import MiniAppUser


class PasskeyFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = b"xass-regression-credential"
        self.rp_id = "xass.example"
        self.origin = "https://xass.example:8443"

    async def asyncTearDown(self) -> None:
        passkeys._pending.clear()
        await self.engine.dispose()

    def client_data(self, challenge: str, kind: str, origin: str | None = None) -> bytes:
        return json.dumps({
            "type": kind, "challenge": challenge,
            "origin": origin or self.origin, "crossOrigin": False,
        }).encode()

    def registration(self, challenge: str, *, origin: str | None = None) -> dict:
        numbers = self.key.public_key().public_numbers()
        public_key = cbor2.dumps({
            1: 2, 3: -7, -1: 1,
            -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big"),
        })
        authenticator_data = (
            hashlib.sha256(self.rp_id.encode()).digest() + b"\x45" + bytes(4)
            + bytes(16) + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id + public_key
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": authenticator_data})
        return {
            "id": passkeys._encode(self.credential_id),
            "rawId": passkeys._encode(self.credential_id), "type": "public-key",
            "response": {
                "clientDataJSON": passkeys._encode(self.client_data(challenge, "webauthn.create", origin)),
                "attestationObject": passkeys._encode(attestation), "transports": ["internal"],
            },
        }

    def assertion(self, challenge: str, *, signature_valid: bool = True) -> dict:
        client_data = self.client_data(challenge, "webauthn.get")
        authenticator_data = hashlib.sha256(self.rp_id.encode()).digest() + b"\x05" + (1).to_bytes(4, "big")
        signing_key = self.key if signature_valid else ec.generate_private_key(ec.SECP256R1())
        signature = signing_key.sign(
            authenticator_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()),
        )
        return {
            "id": passkeys._encode(self.credential_id),
            "rawId": passkeys._encode(self.credential_id), "type": "public-key",
            "response": {
                "clientDataJSON": passkeys._encode(client_data),
                "authenticatorData": passkeys._encode(authenticator_data),
                "signature": passkeys._encode(signature),
            },
        }

    async def register(self, session):
        options = await passkeys.registration_options(
            session, owner_user_id=42, owner_name="Owner", rp_id=self.rp_id, origin=self.origin,
        )
        return await passkeys.complete_registration(
            session, transaction=options["transaction"], name="My iPhone",
            credential=self.registration(options["options"]["challenge"]),
        )

    async def test_register_sign_in_rename_and_remove_key(self) -> None:
        async with self.sessions() as session:
            saved = await self.register(session)
            self.assertEqual(saved.owner_user_id, 42)
            self.assertEqual(saved.transports, ["internal"])
            options = await passkeys.authentication_options(
                session, owner_user_id=42, rp_id=self.rp_id, origin=self.origin, purpose="server:update",
            )
            credential = self.assertion(options["options"]["challenge"])
            stored, purpose = await passkeys.complete_authentication(
                session, transaction=options["transaction"], credential=credential,
            )
            self.assertEqual(purpose, "server:update")
            self.assertEqual(stored.sign_count, 1)
            self.assertIsNotNone(stored.last_used_at)
            with self.assertRaises(ValueError):
                await passkeys.complete_authentication(session, transaction=options["transaction"], credential=credential)
            await passkeys.rename_credential(session, owner_user_id=42, credential_id=saved.id, name="Personal phone")
            self.assertEqual((await passkeys.list_credentials(session, 42))[0].name, "Personal phone")
            self.assertIsNone(await passkeys.delete_credential(session, owner_user_id=99, credential_id=saved.id))
            await passkeys.delete_credential(session, owner_user_id=42, credential_id=saved.id)
            self.assertEqual(await passkeys.count_credentials(session, 42), 0)

    async def test_wrong_origin_returns_client_error_without_saving_key(self) -> None:
        async with self.sessions() as session:
            options = await passkeys.registration_options(
                session, owner_user_id=42, owner_name="Owner", rp_id=self.rp_id, origin=self.origin,
            )
            payload = main.PasskeyCompletePayload(
                transaction=options["transaction"], name="Phone",
                credential=self.registration(options["options"]["challenge"], origin="https://other.example"),
            )
            with self.assertRaises(HTTPException) as error:
                await main.pwa_passkey_register_verify(payload, MiniAppUser(42, "Owner", "", "", True), session)
            self.assertEqual(error.exception.status_code, 400)
            self.assertIn("Passkey", error.exception.detail)
            self.assertEqual(await passkeys.count_credentials(session, 42), 0)

    async def test_bad_signature_is_rejected_without_incrementing_counter(self) -> None:
        async with self.sessions() as session:
            saved = await self.register(session)
            options = await passkeys.authentication_options(
                session, owner_user_id=42, rp_id=self.rp_id, origin=self.origin, purpose="login",
            )
            with self.assertRaises(ValueError):
                await passkeys.complete_authentication(
                    session, transaction=options["transaction"],
                    credential=self.assertion(options["options"]["challenge"], signature_valid=False),
                )
            self.assertEqual(saved.sign_count, 0)


class PasskeyOriginTests(unittest.TestCase):
    def test_direct_and_forwarded_origins_preserve_nonstandard_ports(self) -> None:
        for host, scheme, expected, forwarded in [
            ("localhost:8000", "http", ("localhost", "http://localhost:8000"), False),
            ("XASS.example:8443", "https", ("xass.example", "https://xass.example:8443"), True),
            ("xass.example:443", "https", ("xass.example", "https://xass.example"), True),
            ("[::1]:8000", "http", ("::1", "http://[::1]:8000"), False),
        ]:
            with self.subTest(host=host):
                headers = [(b"host", host.encode())]
                if forwarded:
                    headers = [(b"host", b"127.0.0.1:8000"), (b"x-forwarded-host", host.encode()), (b"x-forwarded-proto", scheme.encode())]
                request = Request({"type": "http", "scheme": scheme, "path": "/", "query_string": b"", "headers": headers})
                self.assertEqual(main._public_origin(request), expected)


if __name__ == "__main__":
    unittest.main()
