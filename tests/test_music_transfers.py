"""Real API handoffs: a new output must never start before the old output ACKs.

All players are database fixtures; this suite never sends commands to a PC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import unittest

from sqlalchemy import select

from app.models import AgentCommand, AgentCredential, HeartbeatSource
from app.music_models import MusicSession, MusicTrack
from app.music_playback_models import MusicPlaybackState, MusicTransfer
import test_music_api as fixtures


class MusicTransferTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only the fixture helpers, not the unrelated test methods.
    asyncSetUp = fixtures.MusicApiTests.asyncSetUp
    asyncTearDown = fixtures.MusicApiTests.asyncTearDown
    request = fixtures.MusicApiTests.request
    start = fixtures.MusicApiTests.start
    chunk = fixtures.MusicApiTests.chunk
    upload = fixtures.MusicApiTests.upload

    old_key = "old-player-session-key"
    new_key = "new-player-session-key"

    async def music_track(self):
        track = await self.upload()
        async with self.sessions() as session:
            item = await session.get(MusicTrack, track["id"])
            item.duration = 600
            await session.commit()
        return track["id"]

    async def agent(self, name, *, track_id=None, state="idle", online=True, paired=True):
        async with self.sessions() as session:
            session.add(HeartbeatSource(source_name=name, source_type="PC_AGENT", is_online=online,
                last_seen_at=datetime.now(timezone.utc) - timedelta(seconds=0 if online else 130),
                last_payload={"music_player": {"track_id": track_id, "state": state,
                    "position_sec": 12, "output_id": "speakers", "volume": 52}}))
            if paired:
                session.add(AgentCredential(source_name=name, key_hint="fixture", is_active=True,
                    api_key_hash=hashlib.sha256(name.encode()).hexdigest()))
            await session.commit()

    async def playing(self, track_id, *, device="local", state="playing"):
        response = await self.request("POST", "/api/mini/music/session", json={
            "session_key": self.old_key, "track_id": track_id, "device": device,
            "state": state, "position": 12, "client_id": "old-controller"})
        self.assertEqual(response.status_code, 200, response.text)

    async def transfer(self, track_id=None, **extra):
        payload = {"session_key": self.new_key, "client_id": "new-controller", "device": "local"}
        if track_id is not None:
            payload["track_id"] = track_id
        return await self.request("POST", "/api/mini/music/transfers", json={**payload, **extra})

    async def poll(self, transfer_id):
        response = await self.request("GET", f"/api/mini/music/transfers/{transfer_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def commands(self):
        async with self.sessions() as session:
            return list(await session.scalars(select(AgentCommand).order_by(AgentCommand.id)))

    async def complete(self, command_id, *, state, position=19, ok=True):
        async with self.sessions() as session:
            command = await session.get(AgentCommand, command_id)
            command.status = "completed" if ok else "failed"
            command.result = {"ok": ok, "message": "fixture result",
                "details": {"state": state, "position_sec": position}}
            source = await session.scalar(select(HeartbeatSource).where(
                HeartbeatSource.source_name == command.source_name))
            source.last_payload = {"music_player": {**source.last_payload["music_player"],
                "track_id": command.payload.get("track_id", source.last_payload["music_player"].get("track_id")),
                "state": state, "position_sec": position}}
            source.last_seen_at = datetime.now(timezone.utc)
            await session.commit()

    async def test_all_transfer_routes_require_owner(self):
        for method, suffix in (("POST", ""), ("GET", "/" + "a" * 32), ("POST", "/" + "a" * 32 + "/ack")):
            for headers, status in (({}, 401), ({"x-test-owner": "guest"}, 403)):
                with self.subTest(method=method, suffix=suffix, status=status):
                    result = await self.request(method, "/api/mini/music/transfers" + suffix,
                        json={}, headers=headers)
                    self.assertEqual(result.status_code, status, result.text)

    async def test_idle_local_transfer_is_immediate_and_preserves_queue_output(self):
        track = await self.music_track()
        response = await self.transfer(track, position=17, output_id="local-output", volume=34,
            queue=[track, track], repeat_mode="all")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["session"]["session_key"], self.new_key)
        self.assertEqual(data["session"]["position"], 17)
        self.assertEqual(data["session"]["queue"], [track])
        self.assertEqual(data["session"]["repeat_mode"], "all")
        self.assertEqual(data["session"]["output_id"], "local-output")
        self.assertEqual(data["session"]["volume"], 34)
        self.assertEqual(await self.commands(), [])

    async def test_local_to_pc_waits_for_matching_ack_before_one_play_command(self):
        track = await self.music_track()
        await self.agent("ПК")
        await self.playing(track)
        response = await self.transfer(device="agent:ПК", output_id="speakers", volume=41)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json(); transfer_id = data["transfer_id"]
        self.assertEqual(data["status"], "waiting")
        self.assertEqual(data["session"]["session_key"], self.old_key)
        self.assertEqual(await self.commands(), [])
        self.assertEqual((await self.transfer(track)).status_code, 409)
        report = await self.request("POST", "/api/mini/music/session", json={
            "session_key": self.old_key, "state": "playing", "position": 18})
        self.assertEqual(report.status_code, 409)
        self.assertEqual(report.json()["detail"]["code"], "transfer_requested")
        ack_url = f"/api/mini/music/transfers/{transfer_id}/ack"
        wrong = await self.request("POST", ack_url, json={"session_key": self.new_key, "position": 19})
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(await self.commands(), [])
        for _ in range(2):
            ack = await self.request("POST", ack_url, json={"session_key": self.old_key, "position": 19})
            self.assertEqual(ack.status_code, 200, ack.text)
        for _ in range(2):
            self.assertEqual((await self.poll(transfer_id))["status"], "waiting")
        commands = await self.commands()
        self.assertEqual([item.command for item in commands], ["music_play"])
        self.assertEqual(commands[0].payload["position_sec"], 19)
        self.assertEqual(commands[0].payload["volume"], 41)
        await self.complete(commands[0].id, state="playing")
        ready = await self.poll(transfer_id)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["session"]["device"], "agent:ПК")
        self.assertEqual(len(await self.commands()), 1)

    async def test_pc_to_local_uses_actual_pause_position_and_rejects_old_reports(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="playing")
        await self.playing(track, device="agent:PC")
        result = await self.transfer()
        self.assertEqual(result.status_code, 200, result.text)
        transfer_id = result.json()["transfer_id"]
        commands = await self.commands()
        self.assertEqual([item.command for item in commands], ["music_pause"])
        self.assertEqual((await self.poll(transfer_id))["session"]["device"], "agent:PC")
        await self.complete(commands[0].id, state="paused", position=23)
        ready = await self.poll(transfer_id)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["session"]["position"], 23)
        self.assertEqual(ready["session"]["device"], "local")
        self.assertEqual(ready["session"]["session_key"], self.new_key)
        stale = await self.request("POST", "/api/mini/music/session", json={
            "session_key": self.old_key, "device": "agent:PC", "state": "playing"})
        self.assertEqual(stale.status_code, 409)

    async def test_pc_to_pc_does_not_start_target_until_confirmed_silence(self):
        track = await self.music_track()
        await self.agent("First", track_id=track, state="playing")
        await self.agent("Second")
        await self.playing(track, device="agent:First")
        response = await self.transfer(device="agent:Second")
        transfer_id = response.json()["transfer_id"]
        commands = await self.commands()
        self.assertEqual([(item.source_name, item.command) for item in commands], [("First", "music_pause")])
        await self.complete(commands[0].id, state="paused", position=21)
        self.assertEqual((await self.poll(transfer_id))["status"], "waiting")
        commands = await self.commands()
        self.assertEqual([(item.source_name, item.command) for item in commands],
            [("First", "music_pause"), ("Second", "music_play")])
        self.assertEqual(commands[1].payload["position_sec"], 21)
        await self.complete(commands[1].id, state="playing", position=21)
        self.assertEqual((await self.poll(transfer_id))["status"], "ready")

    async def test_completed_pause_command_without_silence_fails_closed(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="playing")
        await self.playing(track, device="agent:PC")
        response = await self.transfer()
        command = (await self.commands())[0]
        await self.complete(command.id, state="playing")
        result = await self.poll(response.json()["transfer_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["session"]["device"], "agent:PC")
        self.assertEqual(len(await self.commands()), 1)

    async def test_timeout_cancels_pending_pause_without_starting_target(self):
        track = await self.music_track()
        await self.agent("PC", track_id=track, state="playing")
        await self.playing(track, device="agent:PC")
        response = await self.transfer()
        transfer_id = response.json()["transfer_id"]
        async with self.sessions() as session:
            transfer = await session.get(MusicTransfer, transfer_id)
            transfer.created_at = datetime.now(timezone.utc) - timedelta(seconds=31)
            await session.commit()
        result = await self.poll(transfer_id)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ok"])
        self.assertEqual([(item.command, item.status) for item in await self.commands()], [("music_pause", "cancelled")])
        async with self.sessions() as session:
            self.assertEqual((await session.get(MusicPlaybackState, 1)).transfer_id, "")

    async def expire_transfer(self, transfer_id):
        async with self.sessions() as session:
            transfer = await session.get(MusicTransfer, transfer_id)
            transfer.created_at = datetime.now(timezone.utc) - timedelta(seconds=31)
            await session.commit()

    async def test_abandoned_transfer_expires_without_initiator_polling(self):
        track = await self.music_track()
        await self.playing(track)
        response = await self.transfer()
        transfer_id = response.json()["transfer_id"]
        await self.expire_transfer(transfer_id)
        # The source iPhone must recover even when the controller closed before
        # polling the transfer resource again.
        report = await self.request("POST", "/api/mini/music/session", json={
            "session_key": self.old_key, "state": "playing", "position": 18})
        self.assertEqual(report.status_code, 200, report.text)
        self.assertNotIn("active_transfer_id", report.json()["session"])
        self.assertEqual((await self.poll(transfer_id))["status"], "failed")

    async def test_normal_session_poll_expires_abandoned_transfer(self):
        track = await self.music_track()
        await self.playing(track)
        transfer_id = (await self.transfer()).json()["transfer_id"]
        await self.expire_transfer(transfer_id)
        response = await self.request("GET", "/api/mini/music/session")
        self.assertNotIn("active_transfer_id", response.json()["session"])
        self.assertEqual(response.json()["session"]["state"], "playing")

    async def test_late_local_ack_cannot_advance_expired_transfer(self):
        track = await self.music_track()
        await self.agent("PC")
        await self.playing(track)
        transfer_id = (await self.transfer(device="agent:PC")).json()["transfer_id"]
        await self.expire_transfer(transfer_id)
        ack = await self.request("POST", f"/api/mini/music/transfers/{transfer_id}/ack",
            json={"session_key": self.old_key, "position": 19})
        self.assertEqual(ack.status_code, 409, ack.text)
        self.assertEqual((await self.poll(transfer_id))["status"], "failed")
        self.assertEqual(await self.commands(), [])

    async def test_cancel_same_key_waiting_transfer_preserves_source_playback(self):
        track = await self.music_track()
        await self.agent("PC")
        await self.playing(track)
        response = await self.transfer(device="agent:PC", session_key=self.old_key)
        cancelled = await self.request("POST", f"/api/mini/music/transfers/{response.json()['transfer_id']}/cancel")
        self.assertEqual(cancelled.json()["status"], "failed")
        self.assertEqual(cancelled.json()["session"]["state"], "playing")
        self.assertEqual(cancelled.json()["session"]["device"], "local")
        self.assertEqual(await self.commands(), [])

    async def test_cancel_immediately_ready_local_lease_allows_retry(self):
        track = await self.music_track()
        ready = (await self.transfer(track)).json()
        self.assertEqual(ready["status"], "ready")
        cancelled = await self.request("POST", f"/api/mini/music/transfers/{ready['transfer_id']}/cancel")
        self.assertEqual(cancelled.json()["status"], "failed")
        self.assertNotEqual(cancelled.json()["session"]["state"], "loading")
        self.assertEqual((await self.transfer(track)).json()["status"], "ready")

    async def test_late_cancel_does_not_stop_a_newer_local_selection(self):
        track = await self.music_track()
        ready = (await self.transfer(track)).json()
        report = await self.request("POST", "/api/mini/music/session", json={
            "session_key": self.new_key, "state": "playing", "position": 22})
        self.assertEqual(report.status_code, 200, report.text)
        cancelled = await self.request("POST", f"/api/mini/music/transfers/{ready['transfer_id']}/cancel")
        self.assertEqual(cancelled.json()["status"], "ready")
        self.assertEqual(cancelled.json()["session"]["state"], "playing")

    async def test_cancel_ready_pc_target_stops_pending_audio_download(self):
        track = await self.music_track()
        await self.agent("PC")
        response = await self.transfer(track, device="agent:PC")
        command = (await self.commands())[0]
        await self.complete(command.id, state="loading")
        transfer_id = response.json()["transfer_id"]
        self.assertEqual((await self.poll(transfer_id))["status"], "ready")
        cancelled = await self.request("POST", f"/api/mini/music/transfers/{transfer_id}/cancel")
        self.assertEqual(cancelled.json()["status"], "failed")
        self.assertEqual([item.command for item in await self.commands()], ["music_play", "music_stop"])

    async def test_completed_start_without_playback_confirmation_fails(self):
        track = await self.music_track()
        await self.agent("PC")
        response = await self.transfer(track, device="agent:PC")
        await self.complete((await self.commands())[0].id, state="paused")
        data = await self.poll(response.json()["transfer_id"])
        self.assertEqual(data["status"], "failed")
        self.assertFalse(data["ok"])

    async def test_timeout_after_delivered_start_requires_actual_stop_before_new_player(self):
        track = await self.music_track()
        # The PC last reported this same track paused, before receiving play.
        await self.agent("PC", track_id=track, state="paused")
        transfer_id = (await self.transfer(track, device="agent:PC")).json()["transfer_id"]
        command = (await self.commands())[0]
        async with self.sessions() as session:
            delivered = await session.get(AgentCommand, command.id)
            delivered.status = "delivered"
            delivered.delivered_at = datetime.now(timezone.utc)
            await session.commit()
        await self.expire_transfer(transfer_id)
        state = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertEqual(state["state"], "loading", "an old paused heartbeat is not proof of current silence")
        response = await self.transfer(track)
        self.assertEqual(response.json()["status"], "waiting")
        self.assertEqual([item.command for item in await self.commands()], ["music_play", "music_pause"])

    async def test_source_ack_position_is_clamped_to_track_duration(self):
        track = await self.music_track()
        await self.playing(track)
        transfer_id = (await self.transfer()).json()["transfer_id"]
        ack = await self.request("POST", f"/api/mini/music/transfers/{transfer_id}/ack",
            json={"session_key": self.old_key, "position": 601})
        self.assertEqual(ack.status_code, 200, ack.text)
        self.assertEqual((await self.poll(transfer_id))["session"]["position"], 600)

    async def test_offline_source_or_target_cannot_trigger_optimistic_play(self):
        track = await self.music_track()
        await self.agent("Offline", track_id=track, state="playing", online=False)
        target = await self.transfer(track, device="agent:Offline")
        self.assertEqual(target.status_code, 409, target.text)
        await self.playing(track, device="agent:Offline")
        response = await self.transfer()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(await self.commands(), [])
        state = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertEqual(state["state"], "unavailable")

    async def test_unpaired_target_rejected_before_current_player_is_interrupted(self):
        track = await self.music_track()
        await self.agent("Unpaired", paired=False)
        await self.playing(track)
        response = await self.transfer(device="agent:Unpaired")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(await self.commands(), [])
        async with self.sessions() as session:
            self.assertIsNone(await session.scalar(select(MusicTransfer)))
        report = await self.request("POST", "/api/mini/music/session", json={
            "session_key": self.old_key, "state": "playing", "position": 18})
        self.assertEqual(report.status_code, 200, "invalid target must not request source pause: " + report.text)

    async def test_autoplay_false_moves_paused_lease_without_play_command(self):
        track = await self.music_track()
        await self.agent("Target")
        await self.playing(track, state="paused")
        response = await self.transfer(device="agent:Target", autoplay=False)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["session"]["session_key"], self.new_key)
        self.assertEqual(await self.commands(), [])
        # A current 'idle' agent heartbeat must not turn the acknowledged paused
        # lease into optimistic playing.
        self.assertNotEqual(data["session"]["state"], "playing")

    async def test_legacy_takeover_cannot_bypass_audible_player_handoff(self):
        track = await self.music_track()
        await self.playing(track)
        response = await self.request("POST", "/api/mini/music/session", json={
            "session_key": self.new_key, "client_id": "other-controller", "takeover": True,
            "track_id": track, "state": "playing", "device": "local"})
        self.assertEqual(response.status_code, 409, "takeover must not bypass confirmed handoff: " + response.text)
        async with self.sessions() as session:
            self.assertEqual((await session.get(MusicSession, 1)).session_key, self.old_key)


if __name__ == "__main__":
    unittest.main()
