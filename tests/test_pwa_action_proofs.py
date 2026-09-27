from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.db import Base
from app.models import PasskeyCredential
from app.pwa_models import PwaActionProof
from app.services import passkeys
from app.services.pwa_action_proofs import binding_hash, consume_action_proof, issue_action_proof
from app.services.pwa_auth import rotate_session_generation


class PwaActionProofTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = SimpleNamespace(owner_user_id=42, pwa_session_generation_path=str(root / "generation"))
        self.url = "sqlite+aiosqlite:///" + (root / "proofs.db").as_posix()
        self.engine = create_async_engine(self.url)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            credential = PasskeyCredential(owner_user_id=42, credential_id="fixture", public_key="fixture", name="Phone")
            session.add(credential)
            await session.commit()
            self.credential_id = credential.id
        self.purpose = "agent:file_delete:Домашний ПК"
        self.binding = {"source_id": 7, "command": "file_delete", "payload": {"root": "documents", "path": "мой.txt"}}

    async def asyncTearDown(self):
        passkeys._pending.clear()
        await self.engine.dispose()
        self.temp.cleanup()

    async def issue(self, **overrides):
        options = dict(owner_id=42, credential_id=self.credential_id, purpose=self.purpose,
                       parameters_hash=binding_hash(self.binding), generation=0, settings=self.settings)
        options.update(overrides)
        async with self.sessions() as session:
            return await issue_action_proof(session, **options)

    async def consume(self, token, *, owner=42, purpose=None, binding=None, engine=None):
        sessions = async_sessionmaker(engine or self.engine, expire_on_commit=False)
        async with sessions() as session:
            accepted = await consume_action_proof(session, token, owner, purpose or self.purpose,
                self.binding if binding is None else binding, self.settings)
            await session.commit()
            return accepted

    async def test_success_is_single_use_and_only_hash_is_stored(self):
        token = await self.issue()
        async with self.sessions() as session:
            row = await session.get(PwaActionProof, hashlib.sha256(token.encode()).hexdigest())
            self.assertEqual(row.binding_hash, binding_hash(self.binding))
            self.assertNotIn(token, str(row.__dict__))
            self.assertNotIn("мой.txt", str(row.__dict__))
        self.assertTrue(await self.consume(token))
        self.assertFalse(await self.consume(token))

    async def test_wrong_parameters_identity_or_purpose_do_not_consume(self):
        token = await self.issue()
        wrong = {**self.binding, "payload": {"root": "documents", "path": "other.txt"}}
        self.assertFalse(await self.consume(token, binding=wrong))
        self.assertFalse(await self.consume(token, binding={**self.binding, "source_id": 8}))
        self.assertFalse(await self.consume(token, binding={}))
        self.assertFalse(await self.consume(token, owner=99))
        self.assertFalse(await self.consume(token, purpose="agent:shutdown:Домашний ПК"))
        self.assertTrue(await self.consume(token))

    async def test_concurrent_claims_across_independent_engines_have_one_winner(self):
        token = await self.issue()
        other = create_async_engine(self.url)
        try:
            results = await asyncio.gather(self.consume(token), self.consume(token, engine=other))
            self.assertEqual(sorted(results), [False, True])
        finally:
            await other.dispose()

    async def test_expired_proof_is_rejected(self):
        token = await self.issue()
        async with self.sessions() as session:
            await session.execute(update(PwaActionProof).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await session.commit()
        self.assertFalse(await self.consume(token))

    async def test_logout_all_rejects_existing_proof_and_old_challenge(self):
        token = await self.issue()
        rotate_session_generation(self.settings)
        self.assertFalse(await self.consume(token))
        with self.assertRaisesRegex(ValueError, "Сеанс"):
            await self.issue()
        fresh = await self.issue(generation=1)
        self.assertTrue(await self.consume(fresh))

    async def test_deleted_passkey_rejects_proof_even_with_other_live_passkey(self):
        token = await self.issue()
        async with self.sessions() as session:
            session.add(PasskeyCredential(owner_user_id=42, credential_id="other", public_key="fixture", name="Other"))
            await session.execute(delete(PasskeyCredential).where(PasskeyCredential.id == self.credential_id))
            await session.commit()
        self.assertFalse(await self.consume(token))
        with self.assertRaisesRegex(ValueError, "Passkey"):
            await self.issue()

    async def test_legacy_and_malformed_tokens_fail_closed(self):
        for token in ["", "eyJ2IjoxfQ.signature", "xpa_" + "a" * 10000, "xna_" + "a" * 43]:
            self.assertFalse(await self.consume(token))

    async def test_helper_persists_claim_before_business_rollback(self):
        token = await self.issue()
        with patch.object(main, "settings", self.settings), patch.object(main, "miniapp_authenticate", return_value=None):
            async with self.sessions() as session:
                await main._require_pwa_action_proof(session=session, user=SimpleNamespace(user_id=42),
                    telegram_init_data="", action_proof=token, purpose=self.purpose, binding=self.binding)
                await session.rollback()  # e.g. later remote command validation failed
                with self.assertRaises(HTTPException) as error:
                    await main._require_pwa_action_proof(session=session, user=SimpleNamespace(user_id=42),
                        telegram_init_data="", action_proof=token, purpose=self.purpose, binding=self.binding)
                self.assertEqual(error.exception.status_code, 428)

    async def test_challenge_snapshots_binding_before_user_verification(self):
        original = {"payload": {"path": "first.txt"}}
        token = passkeys._transaction(b"challenge", 42, "example.test", "https://example.test",
                                      self.purpose, original, 12)
        original["payload"]["path"] = "second.txt"
        pending = passkeys._consume(token)
        self.assertEqual(pending.binding_hash, binding_hash({"payload": {"path": "first.txt"}}))
        self.assertEqual(pending.generation, 12)

    async def test_invalid_and_oversized_binding_are_rejected(self):
        for value in [{"x": "я" * 8192}, {"x": float("nan")}, {"x": object()}, []]:
            with self.assertRaises(ValueError):
                binding_hash(value)
        self.assertEqual(binding_hash({"b": 2, "a": 1}), binding_hash({"a": 1, "b": 2}))


if __name__ == "__main__":
    unittest.main()
