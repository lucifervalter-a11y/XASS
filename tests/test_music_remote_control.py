"""Bounded owner-only native inbox; HTTP acceptance is not playback success."""
from datetime import datetime, timedelta, timezone
import unittest

from app.music_models import MusicSession
from app.music_playback_models import MusicPlaybackState, MusicRemoteCommand
import test_music_api as fixtures


class MusicRemoteControlTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MusicApiTests.asyncSetUp
    asyncTearDown = fixtures.MusicApiTests.asyncTearDown
    request = fixtures.MusicApiTests.request
    start = fixtures.MusicApiTests.start
    chunk = fixtures.MusicApiTests.chunk
    upload = fixtures.MusicApiTests.upload
    key = "fixture-native-player-key"
    other_key = "different-native-player-key"

    async def player(self, *, state="playing", device="local"):
        track = await self.upload()
        async with self.sessions() as session:
            session.add(MusicSession(id=1, track_id=track["id"], device=device,
                session_key=self.key, state=state, position=1))
            session.add(MusicPlaybackState(id=1, client_id="native-1", volume=70, revision=1))
            await session.commit()
        return track["id"]

    async def command(self, action="pause", **extra):
        return await self.request("POST", "/api/mini/music/session/control",
            json={"target_key": self.key, "action": action, **extra})

    async def inbox(self, key=None):
        return await self.request("GET", "/api/mini/music/session/commands",
            params={"session_key": key or self.key})

    async def ack(self, command_id, **extra):
        return await self.request("POST", f"/api/mini/music/session/commands/{command_id}/ack",
            json={"session_key": self.key, "ok": True, "state": "paused", "position": 2, **extra})

    async def state(self):
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            meta = await session.get(MusicPlaybackState, 1)
            return item, meta

    async def test_all_routes_require_owner_and_validate_input(self):
        command_id = "a" * 32
        for method, suffix in (("POST", "/control"), ("GET", "/commands"),
                              ("GET", "/commands/" + command_id), ("POST", "/commands/" + command_id + "/ack")):
            for headers, status in (({}, 401), ({"x-test-owner": "guest"}, 403)):
                with self.subTest(method=method, suffix=suffix, status=status):
                    response = await self.request(method, "/api/mini/music/session" + suffix, headers=headers,
                        json={}, params={"session_key": self.key})
                    self.assertEqual(response.status_code, status, response.text)
        await self.player()
        for extra in ({"action": "delete"}, {"volume": 101}, {"position": -1}, {"target_key": "short"}):
            self.assertEqual((await self.command(**extra)).status_code, 422)

    async def test_acceptance_and_delivery_never_optimistically_pause(self):
        await self.player()
        response = await self.command()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "pending")
        command_id = response.json()["command_id"]
        before, meta_before = await self.state()
        self.assertEqual(before.state, "playing")
        for _ in range(2):
            inbox = await self.inbox()
            self.assertEqual([item["id"] for item in inbox.json()["commands"]], [command_id])
            self.assertEqual(inbox.json()["commands"][0]["action"], "pause")
            self.assertGreater(inbox.json()["commands"][0]["expires_at"], datetime.now(timezone.utc).timestamp())
        self.assertEqual((await self.state())[0].state, "playing")
        wrong = await self.ack(command_id, session_key=self.other_key)
        self.assertEqual(wrong.status_code, 403)
        ack = await self.ack(command_id)
        self.assertEqual(ack.status_code, 200, ack.text)
        self.assertEqual(ack.json()["status"], "completed")
        after, meta_after = await self.state()
        self.assertEqual(after.state, "paused")
        self.assertEqual(after.position, 2)
        self.assertEqual(meta_after.revision, meta_before.revision + 1)
        self.assertEqual((await self.inbox()).json()["commands"], [])
        # Retry after a lost HTTP response cannot apply a different position/state.
        retry = await self.ack(command_id, state="playing", position=3)
        self.assertEqual(retry.json()["status"], "completed")
        after_retry, meta_retry = await self.state()
        self.assertEqual(after_retry.state, "paused")
        self.assertEqual(after_retry.position, 2)
        self.assertEqual(meta_retry.revision, meta_after.revision)

    async def test_commands_are_bound_to_current_local_session(self):
        await self.player()
        self.assertEqual((await self.command(target_key=self.other_key)).status_code, 409)
        command_id = (await self.command()).json()["command_id"]
        self.assertEqual((await self.inbox(self.other_key)).json()["commands"], [])
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            item.session_key = self.other_key
            await session.commit()
        self.assertEqual((await self.inbox()).json()["commands"], [])
        self.assertEqual((await self.ack(command_id)).status_code, 409)
        self.assertEqual((await self.state())[0].state, "playing")
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            item.device = "agent:PC"
            await session.commit()
        self.assertEqual((await self.command(target_key=self.other_key)).status_code, 409)

    async def test_expired_commands_never_reappear_or_change_playback(self):
        await self.player()
        command_id = (await self.command()).json()["command_id"]
        async with self.sessions() as session:
            item = await session.get(MusicRemoteCommand, command_id)
            item.created_at = datetime.now(timezone.utc) - timedelta(seconds=31)
            await session.commit()
        self.assertEqual((await self.inbox()).json()["commands"], [])
        status = await self.request("GET", f"/api/mini/music/session/commands/{command_id}")
        self.assertEqual(status.json()["status"], "expired")
        self.assertIn("iPhone", status.json()["error"])
        self.assertEqual((await self.ack(command_id)).status_code, 409)
        self.assertEqual((await self.state())[0].state, "playing")
        self.assertEqual((await self.command()).status_code, 200, "expired commands must not exhaust the inbox")

    async def test_active_transfer_blocks_new_commands_and_late_ack(self):
        await self.player()
        command_id = (await self.command()).json()["command_id"]
        async with self.sessions() as session:
            meta = await session.get(MusicPlaybackState, 1)
            meta.transfer_id = "a" * 32
            await session.commit()
        self.assertEqual((await self.command()).status_code, 409)
        self.assertEqual((await self.inbox()).json()["commands"], [],
            "do not deliver an old resume while the source is acknowledging a handoff")
        self.assertEqual((await self.ack(command_id)).status_code, 409)
        self.assertEqual((await self.state())[0].state, "playing")

    async def test_real_handoff_cancels_old_resume_before_pause_ack(self):
        track_id = await self.player()
        command_id = (await self.command("resume")).json()["command_id"]
        transfer = await self.request("POST", "/api/mini/music/transfers", json={
            "session_key": self.other_key, "client_id": "native-2", "device": "local", "track_id": track_id})
        self.assertEqual(transfer.status_code, 200, transfer.text)
        self.assertEqual(transfer.json()["status"], "waiting")
        self.assertEqual((await self.inbox()).json()["commands"], [])
        status = await self.request("GET", f"/api/mini/music/session/commands/{command_id}")
        self.assertEqual(status.json()["status"], "cancelled")
        self.assertEqual((await self.ack(command_id, state="playing")).status_code, 409)
        transfer_id = transfer.json()["transfer_id"]
        self.assertEqual((await self.request("POST", f"/api/mini/music/transfers/{transfer_id}/ack",
            json={"session_key": self.key, "position": 2})).status_code, 200)
        ready = await self.request("GET", f"/api/mini/music/transfers/{transfer_id}")
        self.assertEqual(ready.json()["status"], "ready")
        self.assertEqual((await self.ack(command_id, state="playing")).status_code, 409)
        self.assertEqual((await self.state())[0].session_key, self.other_key)

    async def test_queue_limit_is_bounded_and_failed_ack_is_not_success(self):
        await self.player()
        ids = []
        for _ in range(16):
            response = await self.command()
            self.assertEqual(response.status_code, 200, response.text)
            ids.append(response.json()["command_id"])
        self.assertEqual((await self.command()).status_code, 429)
        self.assertEqual([item["id"] for item in (await self.inbox()).json()["commands"]], ids)
        failed = await self.ack(ids[0], ok=False, state="error", error="No active audio item")
        self.assertEqual(failed.json()["status"], "failed")
        self.assertEqual((await self.state())[0].state, "playing")
        status = await self.request("GET", f"/api/mini/music/session/commands/{ids[0]}")
        self.assertEqual(status.json()["error"], "No active audio item")
        self.assertEqual((await self.command()).status_code, 200)

    async def test_volume_and_next_update_only_confirmed_fields(self):
        track_id = await self.player()
        command_id = (await self.command("volume", volume=23)).json()["command_id"]
        self.assertEqual((await self.state())[1].volume, 70)
        self.assertEqual((await self.ack(command_id, state="playing")).json()["status"], "completed")
        item, meta = await self.state()
        self.assertEqual(meta.volume, 23)
        self.assertEqual(item.state, "playing")
        self.assertEqual(item.position, 1)
        command_id = (await self.command("next")).json()["command_id"]
        self.assertEqual((await self.ack(command_id, state="playing", position=0)).json()["status"], "completed")
        item, _ = await self.state()
        self.assertEqual(item.track_id, track_id, "next track comes from the real session reporter, not an optimistic server advance")
        self.assertEqual(item.position, 1)

    async def test_contradictory_pause_ack_cannot_claim_success(self):
        await self.player()
        command_id = (await self.command()).json()["command_id"]
        response = await self.ack(command_id, state="playing")
        if response.status_code == 200:
            self.assertNotEqual(response.json()["status"], "completed", "a playing response is not a confirmed pause")
        else:
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual((await self.state())[0].state, "playing")


if __name__ == "__main__":
    unittest.main()
