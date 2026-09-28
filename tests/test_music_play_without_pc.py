"""P0: the phone must play by itself when no PC is online.

The PC is only an optional output. A switched-off PC (stale heartbeat) must
never make the server refuse phone playback or keep reporting PC "playing".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from sqlalchemy import select

from app.models import AgentCommand, HeartbeatSource
from app.music_models import MusicSession
from app.music_playback import MUSIC_PC_LIVE_SECONDS, music_source_live
from app.music_playback_models import MusicPlaybackState
import test_music_transfers as transfers


class _Source:
    def __init__(self, age, online=True):
        self.is_online = online
        self.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=age)


class MusicSourceLiveTests(unittest.TestCase):
    def test_fresh_heartbeat_is_live_stale_is_not(self):
        self.assertTrue(music_source_live(_Source(1)))
        self.assertTrue(music_source_live(_Source(MUSIC_PC_LIVE_SECONDS - 1)))
        self.assertFalse(music_source_live(_Source(MUSIC_PC_LIVE_SECONDS + 5)))
        self.assertFalse(music_source_live(_Source(60)))
        self.assertFalse(music_source_live(_Source(1, online=False)))
        self.assertFalse(music_source_live(None))


class PlayWithoutPCTests(unittest.IsolatedAsyncioTestCase):
    # Borrow fixtures only; do not re-run the transfer suite here.
    _base = transfers.MusicTransferTests
    asyncSetUp = _base.asyncSetUp
    asyncTearDown = _base.asyncTearDown
    start = _base.start
    chunk = _base.chunk
    upload = _base.upload
    request = _base.request
    music_track = _base.music_track
    agent = _base.agent
    playing = _base.playing
    transfer = _base.transfer
    commands = _base.commands
    old_key = _base.old_key
    new_key = _base.new_key
    phone_key = "phone-player-session-key"

    async def age_heartbeat(self, name, seconds):
        async with self.sessions() as session:
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == name))
            source.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            source.is_online = True  # dashboard still thinks "online" (2 min window)
            await session.commit()

    async def pc_lease(self, track, name="ПК", state="playing"):
        """The PC played this track last (e.g. started from the phone)."""
        await self.agent(name, track_id=track, state=state)
        await self.playing(track, device=f"agent:{name}", state=state)

    async def phone_claim(self, track, **extra):
        return await self.request("POST", "/api/mini/music/session", json={
            "session_key": self.phone_key, "client_id": "phone", "device": "local", "track_id": track,
            "state": "loading", "position": 40, "takeover": True, **extra})

    async def test_stale_pc_heartbeat_is_unavailable_not_playing(self):
        track = await self.music_track()
        await self.pc_lease(track)
        state = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertEqual(state["state"], "playing")
        # PC switched off 30 s ago: generic online window (2 min) still says online.
        await self.age_heartbeat("ПК", 30)
        state = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertEqual(state["state"], "unavailable")

    async def test_players_expose_live_flag(self):
        await self.agent("ПК")
        await self.age_heartbeat("ПК", 30)
        players = (await self.request("GET", "/api/mini/music/players")).json()["players"]
        self.assertEqual(len(players), 1)
        self.assertFalse(players[0]["live"])
        self.assertFalse(players[0]["available"])

    async def test_control_to_stale_pc_fails_fast(self):
        track = await self.music_track()
        await self.pc_lease(track)
        await self.age_heartbeat("ПК", 30)
        response = await self.request("POST", "/api/mini/music/control", json={
            "source_name": "ПК", "action": "resume"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(await self.commands(), [])

    async def test_transfer_to_stale_pc_is_rejected_immediately(self):
        track = await self.music_track()
        await self.agent("ПК")
        await self.age_heartbeat("ПК", 30)
        response = await self.transfer(track, device="agent:ПК")
        self.assertEqual(response.status_code, 409, response.text)

    async def test_phone_takeover_from_stale_playing_pc_needs_no_force(self):
        track = await self.music_track()
        await self.pc_lease(track)
        await self.age_heartbeat("ПК", 30)
        response = await self.phone_claim(track)
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()["session"]
        self.assertEqual(session["device"], "local")
        self.assertEqual(session["session_key"], self.phone_key)
        self.assertEqual(session["state"], "loading")

    async def test_transfer_to_phone_from_dead_pc_is_ready_at_once(self):
        track = await self.music_track()
        await self.pc_lease(track)
        await self.age_heartbeat("ПК", 30)
        response = await self.transfer(track, position=40)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["session"]["device"], "local")
        self.assertEqual(await self.commands(), [])

    async def test_pc_queue_lease_does_not_block_phone_takeover(self):
        # Root cause: after the PC auto-advanced a queue, a phone claim was
        # rejected with agent_playback_authoritative even with force=true.
        track = await self.music_track()
        await self.pc_lease(track)
        async with self.sessions() as session:
            command = AgentCommand(source_name="ПК", command="music_play", status="completed",
                payload={"queue_managed": True, "queue_session_key": self.old_key, "track_id": track},
                result={"ok": True})
            session.add(command)
            await session.flush()
            (await session.get(MusicPlaybackState, 1)).queue_command_id = command.id
            await session.commit()
        await self.age_heartbeat("ПК", 30)
        response = await self.phone_claim(track, force=True)
        self.assertEqual(response.status_code, 200, response.text)
        async with self.sessions() as session:
            self.assertIsNone((await session.get(MusicPlaybackState, 1)).queue_command_id)
            item = await session.get(MusicSession, 1)
            self.assertEqual((item.device, item.session_key), ("local", self.phone_key))

    async def test_online_pc_still_requires_handoff_without_force(self):
        # PC remote must keep working when the PC IS online and playing.
        track = await self.music_track()
        await self.pc_lease(track)
        response = await self.phone_claim(track)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "transfer_required")
        state = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertEqual((state["device"], state["state"]), ("agent:ПК", "playing"))
        control = await self.request("POST", "/api/mini/music/control", json={
            "source_name": "ПК", "action": "pause"})
        self.assertEqual(control.status_code, 200, control.text)


if __name__ == "__main__":
    unittest.main()
