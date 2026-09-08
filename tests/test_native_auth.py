from __future__ import annotations
import base64
import asyncio
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException, Request
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base, get_session
from app.native_api import b64, build_router, consume_native_proof, digest, unb64
from app.native_models import NativeChallenge, NativeDevice, NativeProof
from app.models import PwaPairToken
from app.services.pwa_pairing import issue_pwa_pair_token


class NativeAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = SimpleNamespace(owner_user_id=42, bot_token="test:fixture", setup_api_key="fixture",
            pwa_cookie_secure=True, pwa_session_generation_path=str(self.root / "generation"))
        self.engine = create_async_engine("sqlite+aiosqlite:///" + (self.root / "test.db").as_posix())
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async def dependency():
            async with self.sessions() as session:
                yield session
        async def owner(request: Request):
            if request.headers.get("x-fixture-owner") != "42":
                raise HTTPException(401)
            return SimpleNamespace(user_id=42)
        app = FastAPI()
        app.dependency_overrides[get_session] = dependency
        app.include_router(build_router(self.settings, owner))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid")
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pub = b64(self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        self.headers = {"x-fixture-owner": "42"}
        self.purpose = "agent:detach:7:fixture-PC"
        self.binding = {"source_id": 7, "confirm_name": "fixture-PC"}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()
        self.temp.cleanup()

    async def pair(self, owner=42):
        async with self.sessions() as session:
            return (await issue_pwa_pair_token(session, actor_user_id=owner)).token

    def sign(self, message, key=None):
        return b64((key or self.key).sign(unb64(message), ec.ECDSA(hashes.SHA256())))

    async def enrollment(self):
        token = await self.pair()
        body = {"public_key": self.pub, "pair_token": token}
        options = await self.client.post("/api/native/enrollment/options", json=body)
        self.assertEqual(options.status_code, 200, options.text)
        data = options.json()
        body.update(challenge_id=data["challenge_id"], signature=self.sign(data["message"]), device_name="Fixture iPhone")
        response = await self.client.post("/api/native/enrollment/verify", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response, body

    async def proof(self, device, binding=None):
        options = await self.client.post("/api/native/actions/options", headers=self.headers,
            json={"device_id": device, "purpose": self.purpose, "binding": self.binding if binding is None else binding})
        self.assertEqual(options.status_code, 200, options.text)
        data = options.json()
        body = {"device_id": device, "challenge_id": data["challenge_id"], "signature": self.sign(data["message"])}
        response = await self.client.post("/api/native/actions/verify", headers=self.headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["action_proof"], body

    async def consume(self, proof, purpose=None, binding=None):
        async with self.sessions() as session:
            accepted = await consume_native_proof(session, proof, 42, purpose or self.purpose,
                self.binding if binding is None else binding, self.settings)
            await session.commit()
            return accepted

    async def test_enrollment_sets_secure_cookie_and_consumes_pair(self):
        response, body = await self.enrollment()
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertEqual(response.json()["user"]["id"], 42)
        repeated = await self.client.post("/api/native/enrollment/verify", json=body)
        self.assertEqual(repeated.status_code, 401)

    async def test_cookie_alone_cannot_enroll_without_fresh_owner_link(self):
        await self.enrollment()
        response = await self.client.post("/api/native/enrollment/options", headers=self.headers,
            json={"public_key": self.pub, "pair_token": "xpw_" + "invalid" * 8})
        self.assertEqual(response.status_code, 401)

    async def test_other_owner_link_is_rejected(self):
        response = await self.client.post("/api/native/enrollment/options",
            json={"public_key": self.pub, "pair_token": await self.pair(99)})
        self.assertEqual(response.status_code, 401)

    async def test_wrong_signature_does_not_consume_link(self):
        token = await self.pair()
        body = {"public_key": self.pub, "pair_token": token}
        options = (await self.client.post("/api/native/enrollment/options", json=body)).json()
        body.update(challenge_id=options["challenge_id"], signature=self.sign(options["message"], ec.generate_private_key(ec.SECP256R1())))
        self.assertEqual((await self.client.post("/api/native/enrollment/verify", json=body)).status_code, 403)
        body["signature"] = self.sign(options["message"])
        self.assertEqual((await self.client.post("/api/native/enrollment/verify", json=body)).status_code, 200)

    async def test_challenge_and_proof_are_single_use_and_binding_specific(self):
        device = (await self.enrollment())[0].json()["device_id"]
        proof, body = await self.proof(device)
        self.assertEqual((await self.client.post("/api/native/actions/verify", headers=self.headers, json=body)).status_code, 409)
        self.assertFalse(await self.consume(proof, binding={"source_id": 8, "confirm_name": "fixture-PC"}))
        self.assertFalse(await self.consume(proof, purpose="agent:shutdown:fixture-PC"))
        self.assertTrue(await self.consume(proof))
        self.assertFalse(await self.consume(proof))

    async def test_revoked_device_proof_cannot_authorize(self):
        device = (await self.enrollment())[0].json()["device_id"]
        proof, _ = await self.proof(device)
        async with self.sessions() as session:
            await session.execute(update(NativeDevice).where(NativeDevice.id == device).values(revoked=True))
            await session.commit()
        self.assertFalse(await self.consume(proof))

    async def test_expired_challenge_and_proof_rejected(self):
        device = (await self.enrollment())[0].json()["device_id"]
        proof, _ = await self.proof(device)
        async with self.sessions() as session:
            await session.execute(update(NativeProof).values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
            await session.commit()
        self.assertFalse(await self.consume(proof))
        options = (await self.client.post("/api/native/actions/options", headers=self.headers,
            json={"device_id": device, "purpose": self.purpose, "binding": self.binding})).json()
        async with self.sessions() as session:
            await session.execute(update(NativeChallenge).values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
            await session.commit()
        response = await self.client.post("/api/native/actions/verify", headers=self.headers,
            json={"device_id": device, "challenge_id": options["challenge_id"], "signature": self.sign(options["message"])})
        self.assertEqual(response.status_code, 409)

    async def test_unsigned_and_unauthenticated_actions_never_get_proof(self):
        device = (await self.enrollment())[0].json()["device_id"]
        response = await self.client.post("/api/native/actions/options", json={"device_id": device, "purpose": self.purpose})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(await self.consume("xna_" + "forged" * 7))

    async def test_same_proof_is_consumed_once_across_concurrent_transactions(self):
        device = (await self.enrollment())[0].json()["device_id"]
        proof, _ = await self.proof(device)
        accepted = await asyncio.gather(self.consume(proof), self.consume(proof))
        self.assertEqual(sorted(accepted), [False, True])

    async def test_failed_action_rollback_preserves_proof_for_retry(self):
        device = (await self.enrollment())[0].json()["device_id"]
        proof, _ = await self.proof(device)
        async with self.sessions() as session:
            self.assertTrue(await consume_native_proof(session, proof, 42, self.purpose, self.binding, self.settings))
            await session.rollback()  # The guarded action failed before committing.
        self.assertTrue(await self.consume(proof))
        self.assertFalse(await self.consume(proof))

    async def test_generation_change_invalidates_device_challenge_and_proof(self):
        from app.services.pwa_auth import rotate_session_generation
        device = (await self.enrollment())[0].json()["device_id"]
        proof, _ = await self.proof(device)
        options = (await self.client.post("/api/native/actions/options", headers=self.headers,
            json={"device_id": device, "purpose": self.purpose, "binding": self.binding})).json()
        rotate_session_generation(self.settings)
        self.assertFalse(await self.consume(proof))
        response = await self.client.post("/api/native/actions/verify", headers=self.headers, json={
            "device_id": device, "challenge_id": options["challenge_id"], "signature": self.sign(options["message"])})
        self.assertEqual(response.status_code, 428)
        response = await self.client.post("/api/native/actions/options", headers=self.headers,
            json={"device_id": device, "purpose": self.purpose, "binding": self.binding})
        self.assertEqual(response.status_code, 428)

    async def test_concurrent_enrollment_consumes_one_pair_only_once(self):
        token = await self.pair()
        initial = {"public_key": self.pub, "pair_token": token}
        options = [(await self.client.post("/api/native/enrollment/options", json=initial)).json() for _ in range(2)]
        results = await asyncio.gather(*[
            self.client.post("/api/native/enrollment/verify", json={**initial,
                "challenge_id": item["challenge_id"], "signature": self.sign(item["message"])})
            for item in options])
        self.assertEqual(sum(response.status_code == 200 for response in results), 1, [r.text for r in results])
        self.assertTrue(all(response.status_code in {200, 401, 409} for response in results), [r.text for r in results])
        async with self.sessions() as session:
            self.assertEqual(len(list(await session.scalars(select(NativeDevice)))), 1)

    async def test_challenge_limit_and_expiry_bound_pending_state(self):
        device = (await self.enrollment())[0].json()["device_id"]
        body = {"device_id": device, "purpose": self.purpose, "binding": self.binding}
        for _ in range(30):
            response = await self.client.post("/api/native/actions/options", headers=self.headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
        response = await self.client.post("/api/native/actions/options", headers=self.headers, json=body)
        self.assertEqual(response.status_code, 429)
        async with self.sessions() as session:
            await session.execute(update(NativeChallenge).values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
            await session.commit()
        self.assertEqual((await self.client.post("/api/native/actions/options", headers=self.headers, json=body)).status_code, 200)
        oversized = await self.client.post("/api/native/actions/options", headers=self.headers,
            json={**body, "binding": {"payload": "x" * 8192}})
        self.assertEqual(oversized.status_code, 400)

    async def test_revocation_committed_before_proof_update_cannot_use_stale_device_read(self):
        device = (await self.enrollment())[0].json()["device_id"]
        proof, _ = await self.proof(device)
        async with self.sessions() as session:
            execute = session.execute
            revoked = False

            async def revoke_before_conditional_update(statement, *args, **kwargs):
                nonlocal revoked
                if getattr(statement, "is_update", False) and getattr(statement.table, "name", "") == "native_action_proofs":
                    if not revoked:
                        async with self.sessions() as concurrent:
                            await concurrent.execute(update(NativeDevice).where(NativeDevice.id == device).values(revoked=True))
                            await concurrent.commit()
                        revoked = True
                return await execute(statement, *args, **kwargs)

            with patch.object(session, "execute", side_effect=revoke_before_conditional_update):
                accepted = await consume_native_proof(session, proof, 42, self.purpose, self.binding, self.settings)
                await session.commit()
            self.assertTrue(revoked, "fixture must actually commit revocation before the proof mutation")
            self.assertFalse(accepted, "do not trust an ORM device value read before revocation committed")

    async def test_revocation_before_action_challenge_mutation_cannot_issue_proof(self):
        device = (await self.enrollment())[0].json()["device_id"]
        options = (await self.client.post("/api/native/actions/options", headers=self.headers,
            json={"device_id": device, "purpose": self.purpose, "binding": self.binding})).json()
        execute = AsyncSession.execute
        revoked = False

        async def revoke_before_mutation(session, statement, *args, **kwargs):
            nonlocal revoked
            if (not revoked and getattr(statement, "is_update", False)
                    and getattr(statement.table, "name", "") == "native_challenges"):
                async with self.sessions() as concurrent:
                    await concurrent.execute(update(NativeDevice).where(NativeDevice.id == device).values(revoked=True))
                    await concurrent.commit()
                revoked = True
            return await execute(session, statement, *args, **kwargs)

        with patch.object(AsyncSession, "execute", new=revoke_before_mutation):
            response = await self.client.post("/api/native/actions/verify", headers=self.headers, json={
                "device_id": device, "challenge_id": options["challenge_id"], "signature": self.sign(options["message"])})
        self.assertTrue(revoked)
        self.assertIn(response.status_code, {409, 428}, response.text)
        self.assertNotIn("action_proof", response.json())
        async with self.sessions() as session:
            self.assertIsNone(await session.scalar(select(NativeProof)))


if __name__ == "__main__":
    unittest.main()
