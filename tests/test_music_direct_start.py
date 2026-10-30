"""Direct PC commands must publish their canonical lease atomically."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.transcription_models  # Register optional presentation tables before fixture DB creation.
from app.models import AgentCommand, HeartbeatSource
from app.music_models import MusicSession, MusicTrack
from app.music_playback_models import MusicPlaybackState
from app.services.music_agent_queue import advance_agent_queue
import test_music_transfers as transfer_fixtures


class DirectStartTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = transfer_fixtures.MusicTransferTests.asyncSetUp
    asyncTearDown = transfer_fixtures.MusicTransferTests.asyncTearDown
    start = transfer_fixtures.MusicTransferTests.start
    chunk = transfer_fixtures.MusicTransferTests.chunk
    upload = transfer_fixtures.MusicTransferTests.upload
    request = transfer_fixtures.MusicTransferTests.request
    music_track = transfer_fixtures.MusicTransferTests.music_track
    agent = transfer_fixtures.MusicTransferTests.agent
    playing = transfer_fixtures.MusicTransferTests.playing
    claim = transfer_fixtures.MusicTransferTests.claim
    commands = transfer_fixtures.MusicTransferTests.commands
    old_key, new_key = transfer_fixtures.MusicTransferTests.old_key, transfer_fixtures.MusicTransferTests.new_key

    async def direct(self, track, source="PC", action="play"):
        return await self.request("POST", "/api/mini/music/control", json={
            "source_name": source, "action": action, "track_id": track,
            "position_sec": 17, "output_id": "speakers", "volume": 55})

    async def test_direct_start_claim_and_two_competing_pcs_are_serialized(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="paused")
        await self.agent("Other", track_id=track, state="paused")
        await self.playing(track, state="paused")
        responses = await asyncio.gather(self.direct(track), self.direct(track, "Other"), self.claim(track))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409, 409], [r.text for r in responses])
        commands = await self.commands()
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            if responses[2].status_code == 200:
                self.assertEqual(commands, [])
                self.assertEqual(item.device, "local")
            else:
                self.assertEqual(len(commands), 1)
                self.assertEqual(item.device, "agent:" + commands[0].source_name)
                self.assertEqual(item.state, "loading")
                self.assertEqual((await session.get(MusicPlaybackState, 1)).queue_command_id, commands[0].id)

    async def test_resume_reserves_paused_pc_and_rejects_stale_same_key_report(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="paused")
        await self.playing(track, device="agent:PC", state="paused")
        result = await self.direct(track, action="resume")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual((await self.claim(track)).status_code, 409)
        stale = await self.claim(track, session_key=self.old_key, device="agent:PC", state="paused", takeover=False)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "agent_playback_authoritative")

    async def test_resume_without_matching_pc_lease_cannot_start_old_buffer(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="paused")
        await self.playing(track, state="paused")
        result = await self.direct(track, action="resume")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(await self.commands(), [])

    async def test_resume_refuses_a_different_buffer_than_canonical_track(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track + 1, state="paused")
        await self.playing(track, device="agent:PC", state="paused")
        self.assertEqual((await self.direct(track, action="resume")).status_code, 409)
        self.assertEqual(await self.commands(), [])

    async def test_unacknowledged_start_cannot_be_released_by_old_quiet_or_ended_heartbeat(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="paused")
        result = await self.direct(track)
        self.assertEqual(result.status_code, 200, result.text)
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            item.updated_at = datetime.now(timezone.utc) - timedelta(minutes=3)
            command = await session.get(AgentCommand, result.json()["command_id"])
            command.status = "delivered"
            command.delivered_at = item.updated_at
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "PC"))
            source.last_seen_at = datetime.now(timezone.utc)
            source.last_payload = {"music_player": {"track_id": track, "state": "ended", "position_sec": 600}}
            await session.commit()
            advanced = await advance_agent_queue(session, self.settings, source, "https://fixture.invalid")
            self.assertEqual(advanced["status"], "waiting")
        state = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertEqual(state["state"], "loading")
        self.assertEqual((await self.claim(track, force=True)).status_code, 409)

    async def test_fresh_post_ack_player_report_confirms_actual_start(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="paused")
        result = await self.direct(track)
        async with self.sessions() as session:
            command = await session.get(AgentCommand, result.json()["command_id"])
            command.status, command.completed_at = "completed", datetime.now(timezone.utc) - timedelta(seconds=1)
            command.result = {"ok": True, "details": {"state": "loading"}}
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "PC"))
            source.last_seen_at = datetime.now(timezone.utc)
            source.last_payload = {"music_player": {"track_id": track, "state": "playing", "position_sec": 18}}
            await session.commit()
            advanced = await advance_agent_queue(session, self.settings, source, "https://fixture.invalid")
            self.assertEqual(advanced["status"], "playing")
            self.assertIsNone((await session.get(MusicPlaybackState, 1)).queue_command_id)
        self.assertEqual((await self.claim(track)).status_code, 409)

    async def test_ack_loading_is_not_playing_and_quiet_post_ack_can_release(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="paused")
        result = await self.direct(track)
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            item.updated_at = datetime.now(timezone.utc) - timedelta(seconds=40)
            command = await session.get(AgentCommand, result.json()["command_id"])
            command.status, command.completed_at = "completed", datetime.now(timezone.utc) - timedelta(seconds=1)
            command.result = {"ok": True, "details": {"state": "loading"}}
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "PC"))
            source.last_seen_at = datetime.now(timezone.utc)
            source.last_payload = {"music_player": {"track_id": track, "state": "loading"}}
            await session.commit()
        self.assertEqual((await self.claim(track)).status_code, 409)
        self.assertEqual((await self.request("GET", "/api/mini/music/session")).json()["session"]["state"], "loading")
        async with self.sessions() as session:
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "PC"))
            source.last_seen_at = datetime.now(timezone.utc)
            source.last_payload = {"music_player": {"track_id": track, "state": "error", "position_sec": 0}}
            await session.commit()
        claimed = await self.claim(track)
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["session"]["device"], "local")

    async def test_command_insert_failure_rolls_back_reservation_and_supersession(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="paused")
        await self.playing(track, state="paused")
        original_flush = AsyncSession.flush
        async def fail_insert(session, *args, **kwargs):
            if any(isinstance(obj, AgentCommand) and (obj.payload or {}).get("direct_start") for obj in session.new):
                raise RuntimeError("injected command insert failure")
            return await original_flush(session, *args, **kwargs)
        with patch.object(AsyncSession, "flush", fail_insert):
            with self.assertRaisesRegex(RuntimeError, "injected command"):
                await self.direct(track)
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            self.assertEqual((item.device, item.state, item.session_key), ("local", "paused", self.old_key))
            self.assertIsNone((await session.get(MusicPlaybackState, 1)).queue_command_id)
        self.assertEqual(await self.commands(), [])
        self.assertEqual((await self.claim(track)).status_code, 200)

    async def test_held_insert_blocks_claim_and_heartbeat_until_atomic_publication(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="ended")
        await self.playing(track, state="paused")
        entered, release = asyncio.Event(), asyncio.Event()
        original_flush = AsyncSession.flush
        async def held_insert(session, *args, **kwargs):
            if any(isinstance(obj, AgentCommand) and (obj.payload or {}).get("direct_start") for obj in session.new):
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=5)
            return await original_flush(session, *args, **kwargs)
        async def heartbeat():
            async with self.sessions() as session:
                source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "PC"))
                return await advance_agent_queue(session, self.settings, source, "https://fixture.invalid")
        with patch.object(AsyncSession, "flush", held_insert):
            direct = asyncio.create_task(self.direct(track))
            await asyncio.wait_for(entered.wait(), timeout=5)
            claim, report = asyncio.create_task(self.claim(track)), asyncio.create_task(heartbeat())
            try:
                await asyncio.sleep(0.05)
                self.assertFalse(claim.done())
                self.assertFalse(report.done())
            finally:
                release.set()
            played, claimed, reported = await asyncio.wait_for(asyncio.gather(direct, claim, report), timeout=5)
        self.assertEqual(played.status_code, 200, played.text)
        self.assertEqual(claimed.status_code, 409, claimed.text)
        self.assertEqual(reported["status"], "waiting")
        self.assertEqual(len(await self.commands()), 1)

    async def test_commit_failure_restores_previous_pending_start_and_reservation(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="paused")
        first = await self.direct(track)
        first_id = first.json()["command_id"]
        original_commit = AsyncSession.commit
        async def fail_commit(session):
            if any(isinstance(obj, AgentCommand) and obj.id != first_id
                    and (obj.payload or {}).get("direct_start") for obj in session.identity_map.values()):
                raise RuntimeError("injected commit failure")
            return await original_commit(session)
        with patch.object(AsyncSession, "commit", fail_commit):
            with self.assertRaisesRegex(RuntimeError, "injected commit"):
                await self.direct(track)
        async with self.sessions() as session:
            self.assertEqual((await session.get(MusicPlaybackState, 1)).queue_command_id, first_id)
            self.assertEqual((await session.get(AgentCommand, first_id)).status, "pending")
        self.assertEqual(len(await self.commands()), 1)
        self.assertEqual((await self.claim(track)).status_code, 409)

    async def test_paired_catalog_also_reserves_before_phone_can_claim(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="paused")
        await self.playing(track, state="paused")
        result = await self.client.post(f"/agent/music/library/{track}/play", headers={"X-Api-Key": "PC"}, json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual((await self.claim(track)).status_code, 409)
        self.assertTrue((await self.commands())[0].payload["direct_start"])

    async def test_capable_direct_play_prepares_actual_codec_and_binds_variant_ticket(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="paused")
        async with self.sessions() as session:
            item = await session.get(MusicTrack, track)
            item.mime = "audio/mp4"
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "PC"))
            source.last_payload = {"music_player": {"state": "paused", "supported_codecs": ["flac"]}}
            await session.commit()
        prepared = AsyncMock(return_value={"sha256": "a" * 64, "variant": "flac"})
        with patch("app.services.music_compatibility.prepare_audio", prepared):
            result = await self.direct(track)
        self.assertEqual(result.status_code, 200, result.text)
        prepared.assert_awaited_once()
        command = (await self.commands())[0]
        self.assertEqual(command.payload["sha256"], "a" * 64)
        from app.services.music_library import verify_ticket
        token = command.payload["media_path"].split("ticket=", 1)[1]
        self.assertEqual(verify_ticket(self.settings, token, track, purposes=("agent",))["variant"], "flac")
