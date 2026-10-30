"""Exercise real authenticated API + SQLite state, without touching a real PC."""
from datetime import datetime, timedelta, timezone
import asyncio
from unittest.mock import AsyncMock, patch
import unittest

from sqlalchemy import select

from app.models import AgentCommand, HeartbeatSource
from app.music_models import MusicSession
from app.music_playback_models import MusicPlaybackState, MusicTransfer
from app.services.agent_commands import acknowledge_agent_commands
from app.services.agent_lifecycle import AgentDetachedError, detach_agent
from app import transcription_models  # Register presentation tables before the isolated fixture creates its schema.
import test_music_transfers as fixtures


class ReliableHandoffTests(unittest.IsolatedAsyncioTestCase):
    _base = fixtures.MusicTransferTests
    asyncSetUp = _base.asyncSetUp
    asyncTearDown = _base.asyncTearDown
    start, chunk, upload = _base.start, _base.chunk, _base.upload
    request, music_track, playing = _base.request, _base.music_track, _base.playing
    commands, poll, old_key, new_key = _base.commands, _base.poll, _base.old_key, _base.new_key

    async def agent(self, name, track_id=None, state="idle"):
        await fixtures.MusicTransferTests.agent(self, name, track_id=track_id, state=state)
        async with self.sessions() as session:
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == name))
            source.last_payload = {"music_player": {**source.last_payload["music_player"],
                "handoff_protocol": 2, "supported_codecs": ["pcm_s16le", "flac"]}}
            await session.commit()

    async def transfer(self, track=None, device="local"):
        with patch("app.services.music_compatibility.prepare_audio", AsyncMock(return_value={"sha256": "a" * 64, "variant": ""})):
            response = await self.request("POST", "/api/mini/music/transfers", json={
                "protocol": 2, "session_key": self.new_key, "client_id": "new", "device": device, "track_id": track})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def heartbeat(self, name, attempt, state, track, *, revision=None):
        async with self.sessions() as session:
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == name))
            source.last_payload = {"music_player": {**source.last_payload["music_player"], "state": state,
                "track_id": track, "handoff_id": attempt["transfer_id"],
                "audio_started": state == "playing",
                "handoff_revision": attempt["revision"] if revision is None else revision}}
            source.last_seen_at = datetime.now(timezone.utc)
            await session.commit()

    async def ack_source(self, attempt, **extra):
        return await self.request("POST", f"/api/mini/music/transfers/{attempt['transfer_id']}/ack", json={
            "session_key": self.old_key, "position": 37.25, "revision": attempt["revision"], **extra})

    async def ack_target(self, attempt, state, **extra):
        return await self.request("POST", f"/api/mini/music/transfers/{attempt['transfer_id']}/target-ack", json={
            "session_key": self.new_key, "revision": attempt["revision"], "state": state, **extra})

    async def age_phase(self, attempt):
        async with self.sessions() as session:
            transfer = await session.get(MusicTransfer, attempt["transfer_id"])
            transfer.target = {**transfer.target, "phase_started": 1}
            await session.commit()

    async def complete_source(self, attempt, *, position=27.375, revision=None):
        async with self.sessions() as session:
            transfer = await session.get(MusicTransfer, attempt["transfer_id"])
            command = await session.get(AgentCommand, transfer.stop_command_id)
            command.status = "completed"
            command.result = {"ok": True, "details": {"state": "paused", "position_sec": position,
                "track_id": transfer.target["track_id"], "handoff_id": transfer.id,
                "handoff_revision": attempt["revision"] if revision is None else revision}}
            await session.commit()

    async def test_prepare_precedes_source_stop_and_playing_receipt(self):
        track = await self.music_track()
        await self.agent("PC")
        await self.playing(track)
        attempt = await self.transfer(track, "agent:PC")
        self.assertEqual(attempt["phase"], "preparing")
        self.assertEqual([c.command for c in await self.commands()], ["music_prepare"])
        self.assertEqual((await self.ack_source(attempt)).status_code, 409)
        # A wrong-attempt ready heartbeat cannot stop the source.
        await self.heartbeat("PC", attempt, "prepared", track, revision=attempt["revision"] - 1)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "preparing")
        await self.heartbeat("PC", attempt, "prepared", track)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "waiting")
        self.assertEqual((await self.ack_source(attempt)).status_code, 200)
        self.assertEqual((await self.ack_source(attempt)).status_code, 200)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        commands = await self.commands()
        self.assertEqual([c.command for c in commands], ["music_prepare", "music_start_prepared"])
        self.assertEqual(commands[-1].payload["position_sec"], 37.25)
        # Even command acceptance with 'playing' is not a current heartbeat.
        async with self.sessions() as session:
            command = await session.get(AgentCommand, commands[-1].id)
            command.status, command.result = "completed", {"ok": True, "details": {"state": "playing"}}
            await session.commit()
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        await self.heartbeat("PC", attempt, "playing", track)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["status"], "ready")
        self.assertEqual(len(await self.commands()), 2)

    async def test_local_target_requires_preroll_and_actual_play_ack(self):
        track = await self.music_track()
        await self.playing(track)
        attempt = await self.transfer(track)
        self.assertEqual((await self.ack_target(attempt, "playing")).status_code, 409)
        prepared = await self.ack_target(attempt, "prepared")
        self.assertEqual(prepared.json()["phase"], "waiting")
        self.assertEqual((await self.ack_source(attempt, revision=0)).status_code, 409)
        self.assertEqual((await self.ack_source(attempt)).status_code, 200)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        for _ in range(2):
            self.assertEqual((await self.ack_target(attempt, "playing")).json()["status"], "ready")

    async def test_wrong_track_readiness_does_not_pause_the_phone(self):
        track = await self.music_track()
        await self.agent("PC")
        await self.playing(track)
        attempt = await self.transfer(track, "agent:PC")
        await self.heartbeat("PC", attempt, "prepared", track + 1)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "preparing")
        self.assertEqual((await self.ack_source(attempt)).status_code, 409)
        self.assertEqual([c.command for c in await self.commands()], ["music_prepare"])

    async def test_disconnected_ready_heartbeat_cannot_stop_source(self):
        track = await self.music_track()
        await self.agent("PC")
        await self.playing(track)
        attempt = await self.transfer(track, "agent:PC")
        await self.heartbeat("PC", attempt, "prepared", track)
        async with self.sessions() as session:
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "PC"))
            source.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=16)
            await session.commit()
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "preparing")
        self.assertEqual((await self.ack_source(attempt)).status_code, 409)

    async def test_explicit_offline_flag_blocks_fresh_ready_and_playing_receipts(self):
        track = await self.music_track()
        await self.agent("PC")
        await self.playing(track)
        attempt = await self.transfer(track, "agent:PC")
        await self.heartbeat("PC", attempt, "prepared", track)

        async def online(value):
            async with self.sessions() as session:
                source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "PC"))
                source.is_online = value
                await session.commit()

        await online(False)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "preparing")
        self.assertEqual((await self.ack_source(attempt)).status_code, 409)
        await online(True)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "waiting")
        await self.ack_source(attempt)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        await self.heartbeat("PC", attempt, "playing", track)
        await online(False)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        await online(True)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["status"], "ready")

    async def test_playing_heartbeat_must_postdate_start_command(self):
        track = await self.music_track()
        await self.agent("PC")
        attempt = await self.transfer(track, "agent:PC")
        await self.heartbeat("PC", attempt, "prepared", track)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        await self.heartbeat("PC", attempt, "playing", track)
        async with self.sessions() as session:
            transfer = await session.get(MusicTransfer, attempt["transfer_id"])
            command = await session.get(AgentCommand, transfer.start_command_id)
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "PC"))
            source.last_seen_at = command.created_at - timedelta(seconds=1)
            await session.commit()
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        await self.heartbeat("PC", attempt, "playing", track)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["status"], "ready")

    async def test_pc_to_phone_waits_for_preroll_then_exact_stop_then_playing(self):
        track = await self.music_track()
        await self.agent("PC", track, "playing")
        await self.playing(track, device="agent:PC")
        attempt = await self.transfer(track)
        self.assertEqual(await self.commands(), [])
        prepared = await self.ack_target(attempt, "prepared")
        self.assertEqual(prepared.json()["phase"], "waiting")
        self.assertEqual([c.command for c in await self.commands()], ["music_pause"])
        self.assertEqual((await self.ack_target(attempt, "playing")).status_code, 409)
        await self.complete_source(attempt)
        starting = await self.poll(attempt["transfer_id"])
        self.assertEqual(starting["phase"], "starting")
        self.assertEqual(starting["session"]["position"], 27.375)
        self.assertEqual(starting["session"]["state"], "loading")
        self.assertEqual((await self.ack_target(attempt, "playing")).json()["status"], "ready")
        self.assertEqual([c.command for c in await self.commands()], ["music_pause"])

    async def test_pc_stop_position_is_bounded_by_target_duration(self):
        track = await self.music_track()
        await self.agent("PC", track, "playing")
        await self.playing(track, device="agent:PC")
        attempt = await self.transfer(track)
        await self.ack_target(attempt, "prepared")
        await self.complete_source(attempt, position=601)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["session"]["position"], 600)

    async def test_old_pc_stop_ack_never_starts_phone_or_auto_resumes_pc(self):
        track = await self.music_track()
        await self.agent("PC", track, "playing")
        await self.playing(track, device="agent:PC")
        attempt = await self.transfer(track)
        await self.ack_target(attempt, "prepared")
        await self.complete_source(attempt, revision=attempt["revision"] - 1)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["status"], "failed")
        self.assertEqual((await self.ack_target(attempt, "playing")).status_code, 409)
        self.assertEqual([c.command for c in await self.commands()], ["music_pause"])

    async def test_timeout_after_pc_stop_never_auto_resumes_either_device(self):
        track = await self.music_track()
        await self.agent("PC", track, "playing")
        await self.playing(track, device="agent:PC")
        attempt = await self.transfer(track)
        await self.ack_target(attempt, "prepared")
        await self.complete_source(attempt)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        await self.age_phase(attempt)
        result = await self.poll(attempt["transfer_id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotEqual(result["session"]["state"], "playing")
        self.assertEqual((await self.ack_target(attempt, "playing")).status_code, 409)
        self.assertEqual([c.command for c in await self.commands()], ["music_pause"])

    async def test_source_timeout_keeps_source_and_never_starts_target(self):
        track = await self.music_track()
        await self.playing(track)
        attempt = await self.transfer(track)
        await self.ack_target(attempt, "prepared")
        await self.age_phase(attempt)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["status"], "failed")
        self.assertEqual((await self.ack_source(attempt)).status_code, 409)
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            self.assertEqual((item.session_key, item.state), (self.old_key, "playing"))
        self.assertEqual(await self.commands(), [])

    async def test_cancel_and_late_target_receipt_cannot_replace_next_lease(self):
        track = await self.music_track()
        attempt = await self.transfer(track)
        await self.ack_target(attempt, "prepared")
        cancel = await self.request("POST", f"/api/mini/music/transfers/{attempt['transfer_id']}/cancel")
        self.assertEqual(cancel.json()["status"], "failed")
        before = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertNotEqual(before["state"], "playing")
        self.assertEqual((await self.ack_target(attempt, "playing")).status_code, 409)
        after = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertEqual(after["revision"], before["revision"])

    async def test_start_timeout_keeps_phone_paused_and_rejects_late_play(self):
        track = await self.music_track()
        await self.playing(track)
        attempt = await self.transfer(track)
        await self.ack_target(attempt, "prepared")
        await self.ack_source(attempt)
        await self.poll(attempt["transfer_id"])
        await self.age_phase(attempt)
        failed = await self.poll(attempt["transfer_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotEqual(failed["session"]["state"], "playing")
        self.assertEqual((await self.ack_target(attempt, "playing")).status_code, 409)

    async def test_old_agent_is_rejected_before_source_is_paused(self):
        track = await self.music_track()
        await fixtures.MusicTransferTests.agent(self, "Old")
        await self.playing(track)
        response = await self.request("POST", "/api/mini/music/transfers", json={"protocol": 2,
            "session_key": self.new_key, "client_id": "new", "device": "agent:Old", "track_id": track})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(await self.commands(), [])

    async def test_concurrent_poll_and_restart_retry_enqueue_one_start(self):
        track = await self.music_track()
        await self.agent("PC")
        attempt = await self.transfer(track, "agent:PC")
        await self.heartbeat("PC", attempt, "prepared", track)
        results = await asyncio.gather(*(self.poll(attempt["transfer_id"]) for _ in range(6)))
        self.assertTrue(all(result["phase"] == "starting" for result in results))
        commands = await self.commands()
        self.assertEqual([c.command for c in commands], ["music_prepare", "music_start_prepared"])
        # Simulate a crash after durable command commit but before its ID was
        # persisted in the transfer row. Poll recovery reuses that exact ID.
        async with self.sessions() as session:
            transfer = await session.get(MusicTransfer, attempt["transfer_id"])
            transfer.start_command_id = None
            await session.commit()
        await asyncio.gather(*(self.poll(attempt["transfer_id"]) for _ in range(4)))
        self.assertEqual(len(await self.commands()), 2)
        async with self.sessions() as session:
            transfer = await session.get(MusicTransfer, attempt["transfer_id"])
            self.assertEqual(transfer.start_command_id, commands[-1].id)

    async def test_timeout_recovers_and_cancels_orphaned_pending_start(self):
        track = await self.music_track()
        await self.agent("PC")
        attempt = await self.transfer(track, "agent:PC")
        await self.heartbeat("PC", attempt, "prepared", track)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        start = (await self.commands())[-1]
        async with self.sessions() as session:
            transfer = await session.get(MusicTransfer, attempt["transfer_id"])
            transfer.start_command_id = None
            transfer.target = {**transfer.target, "phase_started": 1}
            await session.commit()
        self.assertEqual((await self.poll(attempt["transfer_id"]))["status"], "failed")
        async with self.sessions() as session:
            transfer = await session.get(MusicTransfer, attempt["transfer_id"])
            recovered = await session.get(AgentCommand, start.id)
            self.assertEqual(transfer.start_command_id, start.id)
            self.assertEqual(recovered.status, "cancelled")
        self.assertEqual(len(await self.commands()), 2)

    async def test_timeout_recovers_delivered_start_and_keeps_audible_uncertainty(self):
        track = await self.music_track()
        await self.agent("PC")
        attempt = await self.transfer(track, "agent:PC")
        await self.heartbeat("PC", attempt, "prepared", track)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        start = (await self.commands())[-1]
        async with self.sessions() as session:
            transfer = await session.get(MusicTransfer, attempt["transfer_id"])
            transfer.start_command_id = None
            transfer.target = {**transfer.target, "phase_started": 1}
            command = await session.get(AgentCommand, start.id)
            command.status, command.delivered_at = "delivered", datetime.now(timezone.utc)
            await session.commit()
        failed = await self.poll(attempt["transfer_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["session"]["state"], "loading")
        self.assertEqual(failed["session"]["device"], "agent:PC")
        self.assertEqual(len(await self.commands()), 2)
        # Even a new PREPARED heartbeat cannot prove that a delivered START
        # will not run next. An explicit retry must retain the STOP barrier.
        await self.heartbeat("PC", attempt, "prepared", track)
        current = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertEqual(current["state"], "loading")
        retry = await self.transfer(track)
        self.assertEqual((await self.ack_target(retry, "prepared")).json()["phase"], "waiting")
        self.assertEqual([c.command for c in await self.commands()],
            ["music_prepare", "music_start_prepared", "music_pause"])

    async def test_detaching_target_cancels_delivered_start_and_rejects_late_ack(self):
        track = await self.music_track()
        await self.agent("PC")
        attempt = await self.transfer(track, "agent:PC")
        await self.heartbeat("PC", attempt, "prepared", track)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["phase"], "starting")
        start = (await self.commands())[-1]
        async with self.sessions() as session:
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "PC"))
            command = await session.get(AgentCommand, start.id)
            command.status, command.delivered_at = "delivered", datetime.now(timezone.utc)
            await session.commit()
            await detach_agent(session, source_name="PC", source_id=source.id)
        self.assertEqual((await self.poll(attempt["transfer_id"]))["status"], "failed")
        async with self.sessions() as session:
            with self.assertRaises(AgentDetachedError):
                await acknowledge_agent_commands(session, source_name="PC", results=[{
                    "id": start.id, "ok": True, "details": {"state": "playing",
                        "track_id": track, "handoff_id": attempt["transfer_id"],
                        "handoff_revision": attempt["revision"], "audio_started": True}}])
            await session.rollback()
            self.assertEqual((await session.get(AgentCommand, start.id)).status, "cancelled")
        self.assertEqual([c.command for c in await self.commands()], ["music_prepare", "music_start_prepared"])


if __name__ == "__main__":
    unittest.main()
