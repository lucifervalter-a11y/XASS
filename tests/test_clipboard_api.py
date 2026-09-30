"""The owner API preserves E2E clipboard bytes and accepts legacy plain text."""
import base64
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, patch

import httpx
from fastapi import FastAPI

from app import main
from pc_client.e2e_crypto import generate_keypair, seal_text, unseal_text


class ClipboardCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = SimpleNamespace(id=1, source_name="PC")
        self.session = SimpleNamespace(scalar=AsyncMock(return_value=source))
        app = FastAPI()
        app.add_api_route("/api/mini/agents/{source_name}/commands", main.mini_agent_command, methods=["POST"])
        app.dependency_overrides[main.require_mini_owner] = lambda: SimpleNamespace(user_id=42)
        app.dependency_overrides[main.get_session] = lambda: self.session
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid")
        self.addAsyncCleanup(self.client.aclose)
        command = SimpleNamespace(id=1, source_name="PC", command="clipboard_set", status="pending",
                                  created_at=datetime.now(timezone.utc))
        self.enqueue = AsyncMock(return_value=command)
        self.audit = AsyncMock()
        self.proof = AsyncMock()
        for name, value in (("enqueue_agent_command", self.enqueue), ("log_admin_action", self.audit), ("_require_pwa_action_proof", self.proof)):
            context = patch.object(main, name, value)
            context.start()
            self.addCleanup(context.stop)

    async def send(self, payload):
        return await self.client.post("/api/mini/agents/PC/commands",
            json={"command": "clipboard_set", "payload": payload})

    async def test_encrypted_clipboard_survives_owner_api_and_decrypts_on_pc(self):
        owner_private, owner_public = generate_keypair()
        agent_private, agent_public = generate_keypair()
        envelope = seal_text("Текст с iPhone 🔒", private_jwk=owner_private, peer_public_jwk=agent_public, aad="clipboard")
        response = await self.send({**envelope, "text": "discard this plaintext"})
        self.assertEqual(response.status_code, 200, response.text)
        queued = self.enqueue.await_args.kwargs["payload"]
        self.assertEqual(queued, envelope)
        self.assertEqual(unseal_text(queued, private_jwk=agent_private, peer_public_jwk=owner_public, aad="clipboard"), "Текст с iPhone 🔒")
        self.assertNotIn("blob", self.audit.await_args.args[3])
        self.assertNotIn("text", self.audit.await_args.args[3])

    async def test_empty_and_maximum_utf8_envelopes_keep_ciphertext_unchanged(self):
        owner_private, owner_public = generate_keypair()
        agent_private, agent_public = generate_keypair()
        for text in ("", "🔒" * (64 * 1024)):
            with self.subTest(length=len(text)):
                envelope = seal_text(text, private_jwk=owner_private, peer_public_jwk=agent_public, aad="clipboard")
                response = await self.send(envelope)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.enqueue.await_args.kwargs["payload"], envelope)
                self.assertEqual(unseal_text(envelope, private_jwk=agent_private, peer_public_jwk=owner_public, aad="clipboard"), text)

    async def test_legacy_plain_text_and_empty_clipboard_remain_supported(self):
        for text in ("", "Буфер старого ПК", "я" * (64 * 1024)):
            with self.subTest(length=len(text)):
                response = await self.send({"text": text, "unused": "drop"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.enqueue.await_args.kwargs["payload"], {"text": text})
                self.assertNotIn("text", self.audit.await_args.args[3])
        response = await self.send({"text": "x" * (64 * 1024 + 1)})
        self.assertEqual(response.status_code, 400)

    async def test_malformed_envelopes_never_enqueue_or_fall_back_to_plaintext(self):
        encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
        valid = encode(b"XASS\x01" + b"\0" * 29)
        cases = [
            {"sealed": True}, {"sealed": True, "blob": None}, {"sealed": True, "blob": 1},
            {"sealed": "true", "blob": valid}, {"sealed": 1, "blob": valid},
            {"sealed": False, "blob": valid}, {"blob": valid},
            *({"sealed": True, "blob": value} for value in (
                "", valid + "=", valid + "\n", "!" + valid[1:], valid[:-1] + "B",
                encode(b"NOPE\x01" + b"\0" * 28), encode(b"XASS\x02" + b"\0" * 28),
                encode(b"XASS\x01" + b"\0" * 27), encode(b"XASS\x01" + b"\0" * (262177 - 4)),
            )),
        ]
        for payload in cases:
            with self.subTest(payload_type=type(payload.get("blob")).__name__, size=len(str(payload.get("blob", "")))):
                response = await self.send({**payload, "text": "never enqueue on malformed envelope"})
                self.assertEqual(response.status_code, 400)
                self.enqueue.assert_not_awaited()
                self.audit.assert_not_awaited()

    async def test_clipboard_read_requires_fresh_proof_bound_to_agent_and_payload(self):
        command = SimpleNamespace(id=2, source_name="PC", command="clipboard_get", status="pending",
                                  created_at=datetime.now(timezone.utc))
        self.enqueue.return_value = command
        response = await self.client.post("/api/mini/agents/PC/commands",
            json={"command": "clipboard_get", "payload": {}, "action_proof": "xna_clipboard"})
        self.assertEqual(response.status_code, 200, response.text)
        self.proof.assert_awaited_once_with(
            session=self.session, user=ANY, telegram_init_data="", action_proof="xna_clipboard",
            purpose="agent:clipboard_get:PC",
            binding={"source_id": 1, "command": "clipboard_get", "payload": {}},
        )

    async def test_clipboard_write_does_not_prompt_for_read_access(self):
        response = await self.send({"text": "owner chose to send this"})
        self.assertEqual(response.status_code, 200, response.text)
        self.proof.assert_not_awaited()
