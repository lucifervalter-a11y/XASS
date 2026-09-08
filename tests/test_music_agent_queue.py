"""Database-backed PC queue tests; never execute commands or use a real player."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import AgentCommand, AgentCredential, HeartbeatSource
from app.music_models import MusicSession, MusicTrack
from app.music_playback_models import MusicPlaybackState
from app.music_storage_models import MusicStorageCopy, MusicStorageJob
from app.services.music_agent_queue import advance_agent_queue, cancel_agent_queue
from app.services.music_library import verify_ticket
from test_music_library import silent_wav


class MusicAgentQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = SimpleNamespace(music_root=str(self.root), music_min_free_bytes=0,
            bot_token="fixture-bot", setup_api_key="fixture-setup", owner_user_id=42,
            pwa_session_generation_path=str(self.root / "generation"))
        self.engine = create_async_engine("sqlite+aiosqlite:///" + (self.root / "test.db").as_posix())
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.origin = "https://fixture.invalid:8443"
        self.key_hash = hashlib.sha256(b"fixture-issued-key").hexdigest()
        self.audio = silent_wav()
        self.paths = {}
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            for ident in (1, 2, 3):
                name = f"{ident:032x}.wav"
                self.paths[ident] = self.root / name; self.paths[ident].write_bytes(self.audio)
                session.add(MusicTrack(id=ident, filename=f"Track {ident}.wav", storage_name=name,
                    title=f"Track {ident}", artist="Fixture", mime="audio/wav", duration=60,
                    sha256=hashlib.sha256(self.audio).hexdigest(), size=len(self.audio)))
            source = HeartbeatSource(source_name="Мой ПК", source_type="PC_AGENT", is_online=True,
                last_seen_at=now, last_payload={"music_player": {
                    "track_id": 1, "state": "ended", "position_sec": 60, "output_id": "fixture-output", "volume": 43}})
            credential = AgentCredential(source_name="Мой ПК", api_key_hash=self.key_hash, key_hint="fixture", is_active=True)
            session.add_all([source, credential, MusicSession(id=1, track_id=1, device="agent:Мой ПК",
                session_key="fixture-queue-session", state="playing", position=0, updated_at=now - timedelta(seconds=1)),
                MusicPlaybackState(id=1, queue=[1, 2, 3], repeat_mode="off", volume=70)])
            await session.commit()
            self.source_id, self.credential_id = source.id, credential.id

    async def asyncTearDown(self):
        await self.engine.dispose(); self.temp.cleanup()

    async def advance(self, origin=None):
        async with self.sessions() as session:
            source = await session.get(HeartbeatSource, self.source_id)
            return await advance_agent_queue(session, self.settings, source, self.origin if origin is None else origin)

    async def commands(self):
        async with self.sessions() as session:
            return list(await session.scalars(select(AgentCommand).order_by(AgentCommand.id)))

    async def state(self):
        async with self.sessions() as session:
            return await session.get(MusicSession, 1), await session.get(MusicPlaybackState, 1)

    async def report(self, ident, state, *, seconds_ago=0):
        async with self.sessions() as session:
            source = await session.get(HeartbeatSource, self.source_id)
            source.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
            source.last_payload = {"music_player": {"track_id": ident, "state": state, "position_sec": 60 if state == "ended" else 1,
                "output_id": "fixture-output", "volume": 43}}
            await session.commit()

    async def ack(self, ident=None, *, ok=True):
        async with self.sessions() as session:
            meta = await session.get(MusicPlaybackState, 1)
            command = await session.get(AgentCommand, ident or meta.queue_command_id)
            command.status = "completed" if ok else "failed"
            command.result = {"ok": ok, "message": "fixture result", "details": {"state": "loading", "track_id": command.payload["track_id"]}}
            command.completed_at = datetime.now(timezone.utc) - timedelta(milliseconds=1)
            await session.commit()

    async def test_next_track_reservation_is_atomic_and_ticket_is_credential_bound(self):
        result = await self.advance()
        self.assertEqual(result["status"], "waiting")
        item, meta = await self.state()
        command = (await self.commands())[0]
        self.assertEqual((item.track_id, item.state, meta.queue_command_id), (2, "loading", command.id))
        self.assertEqual((command.command, command.status, command.source_name), ("music_play", "pending", "Мой ПК"))
        self.assertTrue(command.payload["queue_managed"])
        self.assertEqual(command.payload["position_sec"], 0)
        self.assertEqual(command.payload["output_id"], "fixture-output")
        self.assertEqual(command.payload["volume"], 43)
        self.assertTrue(command.payload["url"].startswith(self.origin + "/agent/music/"))
        ticket = command.payload["media_path"].split("ticket=", 1)[1]
        claims = verify_ticket(self.settings, ticket, 2, purposes=("agent",))
        self.assertEqual(claims["b"], self.key_hash)
        self.assertNotIn("api_key", command.payload)
        self.assertLessEqual(command.payload["expires_at"] - datetime.now(timezone.utc).timestamp(), 120)

    async def test_concurrent_and_duplicate_ended_heartbeat_reserve_only_one_command(self):
        results = await asyncio.gather(self.advance(), self.advance())
        self.assertTrue(all(value["status"] == "waiting" for value in results), results)
        self.assertEqual(len(await self.commands()), 1)
        for _ in range(3):
            await self.advance()
        self.assertEqual(len(await self.commands()), 1)
        self.assertEqual((await self.state())[0].track_id, 2)

    async def test_queue_order_deduplicates_and_skips_deleted_or_pc_unsupported(self):
        async with self.sessions() as session:
            meta = await session.get(MusicPlaybackState, 1)
            meta.queue = [1, 1, 999, 2, 3, 2, True, "3"]
            (await session.get(MusicTrack, 2)).deleted = True
            await session.commit()
        await self.advance()
        self.assertEqual((await self.state())[0].track_id, 3)

    async def test_repeat_all_wraps_and_off_finishes_without_extra_commands(self):
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1); item.track_id = 3
            await session.commit()
        await self.report(3, "ended")
        self.assertEqual((await self.advance())["status"], "ended")
        self.assertEqual((await self.state())[0].state, "ended")
        self.assertEqual(await self.commands(), [])
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1); item.state = "playing"
            (await session.get(MusicPlaybackState, 1)).repeat_mode = "all"
            await session.commit()
        await self.report(3, "ended")
        await self.advance()
        self.assertEqual((await self.state())[0].track_id, 1)

    async def test_repeat_one_requires_ack_and_fresh_playing_evidence(self):
        async with self.sessions() as session:
            (await session.get(MusicPlaybackState, 1)).repeat_mode = "one"
            await session.commit()
        await self.advance()
        self.assertEqual((await self.state())[0].track_id, 1)
        await self.ack()
        await self.report(1, "ended")  # ACK plus an old ended snapshot is not a new cycle.
        self.assertEqual((await self.advance())["status"], "waiting")
        self.assertEqual(len(await self.commands()), 1)
        await self.report(1, "playing")
        self.assertEqual((await self.advance())["status"], "playing")
        await self.report(1, "ended")
        await self.advance()
        self.assertEqual(len(await self.commands()), 2)
        self.assertEqual((await self.commands())[1].payload["track_id"], 1)
        await self.advance()
        self.assertEqual(len(await self.commands()), 2)

    async def test_stale_different_track_and_paused_reports_never_auto_start(self):
        for ident, state, age in ((1, "ended", 121), (99, "ended", 0), (1, "paused", 0), (1, "playing", 0)):
            await self.report(ident, state, seconds_ago=age)
            await self.advance()
        self.assertEqual(await self.commands(), [])
        async with self.sessions() as session:
            (await session.get(MusicSession, 1)).state = "paused"
            await session.commit()
        await self.report(1, "ended")
        await self.advance()
        self.assertEqual(await self.commands(), [])

    async def test_revoked_offline_unpaired_or_other_canonical_device_are_ignored(self):
        async with self.sessions() as session:
            (await session.get(AgentCredential, self.credential_id)).is_active = False
            await session.commit()
        self.assertEqual((await self.advance())["status"], "ignored")
        async with self.sessions() as session:
            await session.delete(await session.get(AgentCredential, self.credential_id))
            await session.commit()
        self.assertEqual((await self.advance())["status"], "ignored")
        self.assertEqual(await self.commands(), [])

    async def test_expired_start_becomes_error_not_another_play(self):
        await self.advance()
        async with self.sessions() as session:
            command = await session.scalar(select(AgentCommand))
            command.payload = {**command.payload, "expires_at": int(datetime.now(timezone.utc).timestamp()) - 1}
            command.status = "delivered"
            await session.commit()
        result = await self.advance()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "queue_start_timeout")
        self.assertEqual((await self.state())[0].state, "error")
        self.assertEqual(len(await self.commands()), 1)

    async def test_actual_player_failure_does_not_advance_further(self):
        await self.advance(); await self.ack(); await self.report(2, "error")
        result = await self.advance()
        self.assertEqual(result["status"], "failed")
        self.assertEqual((await self.state())[0].state, "error")
        await self.report(2, "ended"); await self.advance()
        self.assertEqual(len(await self.commands()), 1)

    async def test_cold_track_reserves_undeliverable_command_and_restores_before_ticket(self):
        self.paths[2].unlink()
        async with self.sessions() as session:
            track = await session.get(MusicTrack, 2)
            session.add(MusicStorageCopy(track_id=2, credential_id=self.credential_id,
                sha256=track.sha256, size=track.size))
            await session.commit()
        result = await self.advance()
        self.assertEqual(result["status"], "awaiting_media")
        command = (await self.commands())[0]
        self.assertEqual(command.status, "awaiting_media")
        self.assertNotIn("url", command.payload)
        await self.advance()
        async with self.sessions() as session:
            jobs = list(await session.scalars(select(MusicStorageJob)))
            self.assertEqual(len(jobs), 1)
            self.assertEqual((jobs[0].operation, jobs[0].track_id), ("restore", 2))
        self.paths[2].write_bytes(self.audio)  # The verified storage receiver has published the file.
        await self.advance()
        commands = await self.commands()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].status, "pending")
        self.assertIn("url", commands[0].payload)

    async def test_session_change_cancels_reserved_cold_command_without_resurrection(self):
        # Reserve an undelivered command; a different lease must not reuse it.
        await self.advance()
        async with self.sessions() as session:
            (await session.get(MusicSession, 1)).session_key = "new-owner-playback-key"
            await session.commit()
        self.paths[2].write_bytes(self.audio)
        await self.advance()
        self.assertEqual((await self.commands())[0].status, "cancelled")
        self.assertIsNone((await self.state())[1].queue_command_id)

    async def test_manual_cancel_restores_actual_track_and_rolls_back_with_failed_action(self):
        await self.advance()
        async with self.sessions() as session:
            result = await cancel_agent_queue(session, "Мой ПК")
            self.assertEqual(result["cancelled"], True)
            self.assertEqual(result["restored_from_heartbeat"], True)
            await session.rollback()
        self.assertEqual((await self.commands())[0].status, "pending", "caller owns the commit")
        async with self.sessions() as session:
            await cancel_agent_queue(session, "Other PC")
            await session.commit()
        self.assertEqual((await self.commands())[0].status, "pending", "another source cannot cancel this queue")
        async with self.sessions() as session:
            await cancel_agent_queue(session, "Мой ПК")
            await session.commit()
        item, meta = await self.state()
        self.assertEqual((item.track_id, item.state, item.position), (1, "ended", 60))
        self.assertIsNone(meta.queue_command_id)
        self.assertEqual((await self.commands())[0].status, "cancelled")
        await self.report(1, "ended"); await self.advance()
        self.assertEqual(len(await self.commands()), 1, "cancelled reservation must not restart from repeated end")
        await self.report(1, "playing"); await self.advance()  # Actual manual resume re-arms the chosen queue.
        await self.report(1, "ended"); await self.advance()
        self.assertEqual(len(await self.commands()), 2)

    async def test_cancel_does_not_claim_a_delivered_command_was_stopped(self):
        await self.advance()
        async with self.sessions() as session:
            command = await session.scalar(select(AgentCommand))
            command.status = "delivered"; command.delivered_at = datetime.now(timezone.utc)
            await session.commit()
        async with self.sessions() as session:
            result = await cancel_agent_queue(session, "Мой ПК")
            self.assertFalse(result["cancelled"])
            self.assertFalse(result["restored_from_heartbeat"])
            await session.commit()
        self.assertEqual((await self.commands())[0].status, "delivered")
        self.assertEqual((await self.state())[0].track_id, 2)

    async def test_missing_file_without_replica_is_an_explicit_error(self):
        self.paths[2].unlink()
        result = await self.advance()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "queue_file_unavailable")
        self.assertEqual((await self.state())[0].state, "error")

    async def test_negative_agent_ack_stays_error_without_repeated_start(self):
        await self.advance(); await self.ack(ok=False)
        self.assertEqual((await self.advance())["status"], "failed")
        self.assertEqual((await self.state())[0].state, "error")
        await self.report(2, "ended"); await self.advance()
        self.assertEqual(len(await self.commands()), 1)

    async def test_manual_pause_cancels_cold_wait_even_when_file_arrives_later(self):
        self.paths[2].unlink()
        async with self.sessions() as session:
            track = await session.get(MusicTrack, 2)
            session.add(MusicStorageCopy(track_id=2, credential_id=self.credential_id,
                sha256=track.sha256, size=track.size))
            await session.commit()
        await self.advance()
        self.assertEqual((await self.commands())[0].status, "awaiting_media")
        async with self.sessions() as session:
            await cancel_agent_queue(session, "Мой ПК"); await session.commit()
        self.paths[2].write_bytes(self.audio)
        await self.report(1, "paused"); await self.advance()
        self.assertEqual([(command.status, command.command) for command in await self.commands()], [("cancelled", "music_play")])
        self.assertEqual((await self.state())[0].track_id, 1)

    async def test_unsafe_origin_never_issues_a_playable_command(self):
        result = await self.advance("https://user:password@elsewhere.invalid/")
        self.assertEqual(result["status"], "failed")
        command = (await self.commands())[0]
        self.assertEqual(command.status, "failed")
        self.assertNotIn("url", command.payload)


if __name__ == "__main__":
    unittest.main()
