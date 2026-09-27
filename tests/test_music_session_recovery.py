"""Explicit stale local-lease recovery never implies silence or starts audio."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from app.models import AgentCommand
from app.music_models import MusicSession, MusicTrack
from app.music_playback import current_session, stale_local_recovery_available
from app.music_playback_models import MusicPlaybackState, MusicRemoteCommand, MusicTransfer
import test_music_api as fixtures


class MusicSessionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MusicApiTests.asyncSetUp
    asyncTearDown = fixtures.MusicApiTests.asyncTearDown
    request = fixtures.MusicApiTests.request

    old_key = "old-local-session-key"
    new_key = "new-local-session-key"
    path = "/api/mini/music/session/recover"

    async def seed(self, *, age=600, state="playing", device="local", transfer_status=None):
        async with self.sessions() as session:
            session.add(MusicTrack(id=1, title="Fixture", filename="fixture.wav", storage_name="fixture.wav",
                mime="audio/wav", sha256="a" * 64, size=1, duration=1200))
            session.add(MusicSession(id=1, track_id=1, device=device, state=state, position=37,
                session_key=self.old_key, share_site=True,
                updated_at=datetime.now(timezone.utc) - timedelta(seconds=age)))
            session.add(MusicPlaybackState(id=1, revision=7, client_id="old-client", queue=[1],
                repeat_mode="all", transfer_id="b" * 32 if transfer_status else ""))
            if transfer_status and transfer_status != "missing":
                session.add(MusicTransfer(id="b" * 32, source_key=self.old_key, source_device=device,
                    status=transfer_status, target={}, created_at=datetime.now(timezone.utc)))
            await session.commit()

    async def recover(self, **changes):
        return await self.request("POST", self.path, json={"session_key": self.new_key, "client_id": "new-client",
            "expected_source_key": self.old_key, "expected_revision": 7, "confirm_stopped": True, **changes})

    async def assert_unavailable(self, response):
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "stale_session_recovery_unavailable")
        async with self.sessions() as session:
            self.assertEqual((await session.get(MusicSession, 1)).session_key, self.old_key)
            self.assertEqual((await session.get(MusicPlaybackState, 1)).revision, 7)

    async def test_authentication_and_owner_required(self):
        await self.seed()
        for headers, expected in (({}, 401), ({"x-test-owner": "guest"}, 403)):
            response = await self.request("POST", self.path, headers=headers, json={})
            self.assertEqual(response.status_code, expected)

    async def test_confirmation_and_revision_are_required(self):
        await self.seed()
        body = {"session_key": self.new_key, "client_id": "new-client", "expected_source_key": self.old_key,
            "expected_revision": 7, "confirm_stopped": True}
        for changed in ({"confirm_stopped": False}, {"expected_revision": True}, {"expected_revision": -1}):
            self.assertEqual((await self.request("POST", self.path, json={**body, **changed})).status_code, 422)
        del body["confirm_stopped"]
        self.assertEqual((await self.request("POST", self.path, json=body)).status_code, 422)

    async def test_stale_confirmed_recovery_is_paused_and_cancels_only_old_pending(self):
        await self.seed()
        async with self.sessions() as session:
            for identity, key, status in (("c", self.old_key, "pending"), ("d", self.old_key, "completed"),
                                          ("e", "unrelated-session-key", "pending")):
                session.add(MusicRemoteCommand(id=identity * 32, session_key=key, status=status, action="resume"))
            await session.commit()
        state = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertIs(state["recovery_available"], True)
        response = await self.recover()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["ok"], True)
        state = response.json()["session"]
        self.assertEqual((state["session_key"], state["client_id"], state["state"], state["position"]),
            (self.new_key, "new-client", "paused", 37))
        self.assertEqual((state["track_id"], state["revision"], state["queue"], state["repeat_mode"]), (1, 8, [1], "all"))
        self.assertIs(state["recovery_available"], False)
        async with self.sessions() as session:
            self.assertEqual((await session.get(MusicRemoteCommand, "c" * 32)).status, "cancelled")
            self.assertEqual((await session.get(MusicRemoteCommand, "d" * 32)).status, "completed")
            self.assertEqual((await session.get(MusicRemoteCommand, "e" * 32)).status, "pending")
            self.assertEqual(await session.scalar(select(func.count()).select_from(AgentCommand)), 0)
            self.assertEqual(await session.scalar(select(func.count()).select_from(MusicTransfer)), 0)
        profile = json.loads(self.root.joinpath("profile.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["now_listening_text"], "Сейчас ничего не играет")
        self.assertNotIn("ticket", response.text)

    async def test_old_report_and_late_command_ack_cannot_overwrite_recovered_lease(self):
        await self.seed()
        async with self.sessions() as session:
            session.add(MusicRemoteCommand(id="c" * 32, session_key=self.old_key, action="resume"))
            await session.commit()
        self.assertEqual((await self.recover()).status_code, 200)
        report = await self.request("POST", "/api/mini/music/session", json={"session_key": self.old_key,
            "takeover": False, "state": "playing", "position": 100})
        self.assertEqual(report.status_code, 409)
        ack = await self.request("POST", "/api/mini/music/session/commands/" + "c" * 32 + "/ack",
            json={"session_key": self.old_key, "ok": True, "state": "playing", "position": 100})
        self.assertEqual(ack.status_code, 409)
        inbox = await self.request("GET", "/api/mini/music/session/commands", params={"session_key": self.old_key})
        self.assertEqual(inbox.json()["commands"], [])
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            self.assertEqual((item.session_key, item.state, item.position), (self.new_key, "paused", 37))

    async def test_fresh_session_is_not_recoverable(self):
        await self.seed(age=290)
        self.assertIs((await self.request("GET", "/api/mini/music/session")).json()["session"]["recovery_available"], False)
        await self.assert_unavailable(await self.recover())

    async def test_agent_session_is_never_recoverable_even_when_offline(self):
        await self.seed(device="agent:offline-pc")
        self.assertIs((await self.request("GET", "/api/mini/music/session")).json()["session"]["recovery_available"], False)
        await self.assert_unavailable(await self.recover())

    async def test_non_audible_session_is_not_recoverable(self):
        await self.seed(state="paused")
        await self.assert_unavailable(await self.recover())

    async def test_changed_source_revision_or_reused_key_cannot_recover(self):
        await self.seed()
        for changed in ({"expected_revision": 6}, {"expected_source_key": "another-old-session-key"},
                        {"session_key": self.old_key}):
            await self.assert_unavailable(await self.recover(**changed))

    async def test_active_or_missing_handoff_is_never_cancelled_by_recovery(self):
        await self.seed(transfer_status="waiting")
        for status in ("waiting", "stopped", "starting", "missing"):
            async with self.sessions() as session:
                transfer = await session.get(MusicTransfer, "b" * 32)
                if status == "missing":
                    await session.delete(transfer)
                else:
                    transfer.status = status
                await session.commit()
            async with self.sessions() as session:
                self.assertIs((await current_session(session))["recovery_available"], False)
            await self.assert_unavailable(await self.recover())

    async def test_failed_handoff_does_not_block_explicit_recovery(self):
        await self.seed(transfer_status="failed")
        response = await self.recover()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["session"]["state"], "paused")
        async with self.sessions() as session:
            self.assertEqual((await session.get(MusicPlaybackState, 1)).transfer_id, "")
            self.assertEqual((await session.get(MusicTransfer, "b" * 32)).status, "failed")

    async def test_concurrent_recovery_has_exactly_one_winner(self):
        await self.seed()
        responses = await asyncio.gather(self.recover(), self.recover(session_key="other-new-session-key"))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        winner = next(response.json()["session"] for response in responses if response.status_code == 200)
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            self.assertEqual((item.session_key, item.state), (winner["session_key"], "paused"))
            self.assertEqual((await session.get(MusicPlaybackState, 1)).revision, 8)

    async def test_profile_failure_does_not_turn_committed_recovery_into_failure(self):
        await self.seed()
        with patch("app.services.music_broadcast.sync_music_profile", new=AsyncMock(side_effect=OSError("fixture"))):
            response = await self.recover()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["session"]["state"], "paused")
        async with self.sessions() as session:
            self.assertEqual((await session.get(MusicSession, 1)).session_key, self.new_key)

    def test_exact_staleness_boundary_and_loading_state(self):
        now = datetime.now(timezone.utc)
        item = SimpleNamespace(session_key=self.old_key, device="local", state="loading", updated_at=now - timedelta(seconds=300))
        self.assertIs(stale_local_recovery_available(item, None, None, now=now), True)
        item.updated_at += timedelta(microseconds=1)
        self.assertIs(stale_local_recovery_available(item, None, None, now=now), False)
        item.updated_at = now + timedelta(seconds=1)
        self.assertIs(stale_local_recovery_available(item, None, None, now=now), False)


if __name__ == "__main__":
    unittest.main()
