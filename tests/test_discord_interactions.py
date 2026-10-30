"""Real Ed25519 signed requests; no Discord credentials, live guild or network."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.discord_interactions import (BroadcastCache, BroadcastSnapshot, CACHE_TTL, MAX_BODY,
                                      build_router, music_response, public_url)
from app.music_models import MusicSession, MusicTrack, MusicEnrichment
from app.models import HeartbeatSource


class DiscordInteractionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.settings = SimpleNamespace(discord_public_key=self.key.public_key().public_bytes_raw().hex(),
            discord_listen_url="https://redvps.site", profile_public_url="", music_root="unused")
        self.clock = 100.0
        self.cache = BroadcastCache(clock=lambda: self.clock)
        self.snapshot = BroadcastSnapshot(42, "Название", "Исполнитель", "Альбом", 225, 84, self.clock)
        self.app = FastAPI()
        self.sessions = unittest.mock.Mock(side_effect=AssertionError("Request must not query SQL"))
        self.app.include_router(build_router(self.settings, self.sessions, cache=self.cache))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://fixture.invalid")

    async def asyncTearDown(self):
        await self.client.aclose()

    def signed(self, payload, timestamp=None, key=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        timestamp = str(int(time.time())) if timestamp is None else timestamp
        signature = (key or self.key).sign(timestamp.encode() + body).hex()
        return body, {"X-Signature-Ed25519": signature, "X-Signature-Timestamp": timestamp,
                      "Content-Type": "application/json"}

    async def send(self, payload, **kwargs):
        body, headers = self.signed(payload, **kwargs)
        return await self.client.post("/api/discord/interactions", content=body, headers=headers)

    async def test_valid_ping_is_pong_without_sql(self):
        response = await self.send({"type": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"type": 1})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.sessions.assert_not_called()

    async def test_playing_music_embed_progress_and_link_without_sql_or_network(self):
        self.cache.snapshot = self.snapshot
        started = time.perf_counter()
        response = await self.send({"type": 2, "data": {"name": "music", "type": 1}})
        self.assertLess(time.perf_counter() - started, 1.5)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["type"], 4)
        data = result["data"]
        embed = data["embeds"][0]
        self.assertEqual(embed["title"], "Название")
        self.assertIn("1:24 / 3:45", embed["description"])
        self.assertIn("🔘", embed["description"])
        self.assertEqual(embed["fields"][0]["value"], "Альбом")
        self.assertEqual(data["components"][0]["components"][0]["url"], "https://redvps.site")
        self.assertEqual(data["allowed_mentions"], {"parse": []})
        self.sessions.assert_not_called()

    async def test_pause_expiry_end_and_bad_positions_fail_closed(self):
        for snapshot in (None, replace(self.snapshot, captured_at=self.clock-CACHE_TTL-.01),
                         replace(self.snapshot, captured_at=self.clock+1), replace(self.snapshot, duration=0),
                         replace(self.snapshot, position=225), replace(self.snapshot, position=float("nan")),
                         replace(self.snapshot, duration=float("inf"))):
            self.cache.snapshot = snapshot
            result = (await self.send({"type": 2, "data": {"name": "music"}})).json()
            self.assertEqual(result["data"]["content"], "Сейчас ничего не играет в XASS")
            self.assertNotIn("embeds", result["data"])

    async def test_signature_covers_exact_body_and_timestamp(self):
        body, headers = self.signed(b'{ "type": 1 }')
        self.assertEqual((await self.client.post("/api/discord/interactions", content=body, headers=headers)).status_code, 200)
        self.assertEqual((await self.client.post("/api/discord/interactions", content=b'{"type":1}', headers=headers)).status_code, 401)
        headers["X-Signature-Timestamp"] = str(int(headers["X-Signature-Timestamp"])+1)
        self.assertEqual((await self.client.post("/api/discord/interactions", content=body, headers=headers)).status_code, 401)
        self.assertEqual((await self.send({"type": 1}, key=Ed25519PrivateKey.generate())).status_code, 401)

    async def test_missing_malformed_duplicate_and_stale_headers_rejected(self):
        body, headers = self.signed({"type": 1})
        variants = [{}, {**headers, "X-Signature-Ed25519": "ff"},
                    {**headers, "X-Signature-Ed25519": "g"*128},
                    {**headers, "X-Signature-Timestamp": "NaN"},
                    list(headers.items()) + [("X-Signature-Timestamp", headers["X-Signature-Timestamp"])] ]
        for values in variants:
            self.assertEqual((await self.client.post("/api/discord/interactions", content=body, headers=values)).status_code, 401)
        for timestamp in (int(time.time())-301, int(time.time())+40):
            self.assertEqual((await self.send({"type": 1}, timestamp=str(timestamp))).status_code, 401)

    async def test_bad_signature_is_rejected_before_parsing_body(self):
        response = await self.send(b'not json', key=Ed25519PrivateKey.generate())
        self.assertEqual(response.status_code, 401)

    async def test_signed_bad_payloads_and_oversize_are_bounded(self):
        for payload in (b'not json', b'\xff', [], {"type": True}, {"type": 3}, {"type": 2, "data": []}):
            self.assertEqual((await self.send(payload)).status_code, 400)
        self.assertEqual((await self.send(b' '*(MAX_BODY+1))).status_code, 413)

    async def test_slow_chunked_body_exits_before_discord_deadline(self):
        body, headers = self.signed({"type": 1})
        async def chunks():
            yield body[:1]
            await asyncio.sleep(2)
            yield body[1:]
        started = time.perf_counter()
        response = await self.client.post("/api/discord/interactions", content=chunks(), headers=headers)
        self.assertEqual(response.status_code, 408)
        self.assertLess(time.perf_counter() - started, 1.5)

    async def test_unknown_command_gets_fast_ephemeral_reply(self):
        data = (await self.send({"type": 2, "data": {"name": "other"}})).json()["data"]
        self.assertEqual(data["flags"], 64)
        self.assertIn("/music", data["content"])

    async def test_unconfigured_public_key_fails_closed(self):
        settings = SimpleNamespace(**{**vars(self.settings), "discord_public_key": ""})
        app = FastAPI()
        app.include_router(build_router(settings, self.sessions, cache=self.cache))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid") as client:
            body, headers = self.signed({"type": 1})
            self.assertEqual((await client.post("/api/discord/interactions", content=body, headers=headers)).status_code, 503)

    async def test_public_artwork_is_only_current_broadcast_and_revocable(self):
        artwork = b"\xff\xd8\xfffixture-sanitized-cover"
        self.cache.snapshot = replace(self.snapshot, artwork=artwork)
        response = await self.send({"type": 2, "data": {"name": "music"}})
        url = response.json()["data"]["embeds"][0]["thumbnail"]["url"]
        self.assertNotIn("ticket", url)
        path = url.replace("https://redvps.site", "")
        cover = await self.client.get(path)
        self.assertEqual(cover.content, artwork)
        self.assertEqual(cover.headers["content-type"], "image/jpeg")
        self.assertEqual(cover.headers["cache-control"], "no-store")
        self.assertEqual((await self.client.get(path.replace("/42/", "/43/"))).status_code, 404)
        self.cache.invalidate()
        self.assertEqual((await self.client.get(path)).status_code, 404)

    async def test_display_metadata_cannot_ping_members_or_inject_markdown(self):
        self.cache.snapshot = replace(self.snapshot, title="@everyone **x** [y](https://evil.invalid)", artist="<@123>")
        data = (await self.send({"type": 2, "data": {"name": "music"}})).json()["data"]
        self.assertNotIn("@everyone", data["embeds"][0]["title"])
        self.assertIn("\\*", data["embeds"][0]["title"])
        self.assertEqual(data["allowed_mentions"]["parse"], [])

    def test_listen_url_rejects_private_credentials_and_tickets(self):
        for url in ("http://redvps.site", "https://localhost", "https://127.0.0.1", "https://10.0.0.1",
                    "https://x.local", "https://user:pass@redvps.site", "https://redvps.site/?ticket=private",
                    "https://redvps.site/#private", "https://redvps.site:444", "https://redvps.site/\r\n"):
            self.assertEqual(public_url(url), "", url)
        self.assertEqual(public_url("https://redvps.site/music"), "https://redvps.site/music")


class BroadcastCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_async_engine("sqlite+aiosqlite:///" + (self.root/"fixture.db").as_posix())
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add(MusicTrack(id=1, title="Public song", artist="A", album="B", filename="private.mp3",
                storage_name="a"*32+".mp3", mime="audio/mpeg", sha256="a"*64, size=100, duration=180))
            session.add(MusicSession(id=1, track_id=1, device="local", state="playing", position=10,
                share_site=True, session_key="never-disclose", updated_at=datetime.now(timezone.utc)))
            await session.commit()
        self.cache = BroadcastCache()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_refresh_only_explicit_public_opt_in_no_private_metadata(self):
        await self.cache.refresh(self.sessions, self.root)
        self.assertEqual(self.cache.current()[0].title, "Public song")
        data = json.dumps(music_response(self.cache, "https://redvps.site"))
        self.assertNotIn("private.mp3", data)
        self.assertNotIn("never-disclose", data)
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            item.share_site = False
            item.share_discord = True  # RPC consent is not public-broadcast consent.
            await session.commit()
        await self.cache.refresh(self.sessions, self.root)
        self.assertIsNone(self.cache.current())

    async def test_local_commit_immediately_invalidates_orm_and_bulk_updates(self):
        async with self.cache.watch_writes(self.sessions):
            await self.cache.refresh(self.sessions, self.root)
            async with self.sessions() as session:
                item = await session.get(MusicSession, 1)
                item.state = "paused"
                await session.commit()
            self.assertIsNone(self.cache.current())
            async with self.sessions() as session:
                await session.execute(update(MusicSession).values(state="playing"))
                await session.commit()
            await self.cache.refresh(self.sessions, self.root)
            self.assertIsNotNone(self.cache.current())
            async with self.sessions() as session:
                await session.execute(update(MusicTrack).values(deleted=True))
                await session.commit()
            self.assertIsNone(self.cache.current())

    async def agent_broadcast(self):
        async with self.sessions() as session:
            item = await session.get(MusicSession, 1)
            item.device = "agent:Active"
            session.add_all([
                HeartbeatSource(source_name="Active", source_type="PC_AGENT", is_online=True,
                    last_seen_at=datetime.now(timezone.utc), last_payload={"music_player": {
                        "track_id": 1, "state": "playing", "position_sec": 10}}),
                HeartbeatSource(source_name="Other", source_type="PC_AGENT", is_online=True,
                    last_seen_at=datetime.now(timezone.utc), last_payload={}),
            ])
            await session.commit()

    async def test_unrelated_orm_heartbeat_does_not_hide_public_music(self):
        await self.agent_broadcast()
        async with self.cache.watch_writes(self.sessions):
            await self.cache.refresh(self.sessions, self.root)
            snapshot, _ = self.cache.current()
            self.assertEqual(snapshot.source_name, "Active")
            generation = self.cache.generation
            async with self.sessions() as session:
                other = await session.get(HeartbeatSource, 2)
                other.last_seen_at = datetime.now(timezone.utc)
                other.last_payload = {"music_player": {"track_id": 99, "state": "playing", "position_sec": 1}}
                await session.commit()
            self.assertIs(self.cache.current()[0], snapshot)
            self.assertEqual(self.cache.generation, generation)
            self.assertIn("embeds", music_response(self.cache, "https://redvps.site")["data"])

    async def test_active_offline_state_track_rename_and_delete_invalidate(self):
        await self.agent_broadcast()
        for change in ("offline", "paused", "track", "rename", "delete"):
            with self.subTest(change=change):
                async with self.cache.watch_writes(self.sessions):
                    await self.cache.refresh(self.sessions, self.root)
                    self.assertIsNotNone(self.cache.current())
                    async with self.sessions() as session:
                        active = await session.get(HeartbeatSource, 1)
                        if change == "offline":
                            active.is_online = False
                        elif change == "paused":
                            active.last_payload = {"music_player": {"track_id": 1, "state": "paused"}}
                        elif change == "track":
                            active.last_payload = {"music_player": {"track_id": 2, "state": "playing"}}
                        elif change == "rename":
                            active.source_name = "Renamed"
                        else:
                            await session.delete(active)
                        await session.flush()
                        self.assertIsNone(self.cache.current())
                        await session.rollback()

    async def test_public_share_revocation_and_bulk_heartbeat_still_fail_closed(self):
        await self.agent_broadcast()
        async with self.cache.watch_writes(self.sessions):
            await self.cache.refresh(self.sessions, self.root)
            async with self.sessions() as session:
                item = await session.get(MusicSession, 1)
                item.share_site = False
                await session.flush()
                self.assertIsNone(self.cache.current())
                await session.rollback()
            await self.cache.refresh(self.sessions, self.root)
            self.assertIsNotNone(self.cache.current())
            async with self.sessions() as session:
                await session.execute(update(HeartbeatSource).where(HeartbeatSource.source_name == "Other")
                    .values(last_seen_at=datetime.now(timezone.utc)))
                await session.commit()
            self.assertIsNone(self.cache.current())

    async def test_active_offline_write_during_refresh_cannot_republish_old_read(self):
        await self.agent_broadcast()
        from app.discord_interactions import current_broadcast
        entered, release = asyncio.Event(), asyncio.Event()

        async def reading(session):
            value = await current_broadcast(session)
            entered.set()
            await release.wait()
            return value

        async with self.cache.watch_writes(self.sessions):
            await self.cache.refresh(self.sessions, self.root)
            with patch("app.discord_interactions.current_broadcast", side_effect=reading):
                task = asyncio.create_task(self.cache.refresh(self.sessions, self.root))
                await entered.wait()
                async with self.sessions() as session:
                    active = await session.get(HeartbeatSource, 1)
                    active.is_online = False
                    await session.commit()
                self.assertIsNone(self.cache.current())
                release.set()
                await task
            self.assertIsNone(self.cache.current())
            self.assertEqual(self.cache._refresh_sources, {})

    async def test_inflight_old_read_cannot_republish_revoked_snapshot(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def reading(session):
            value = await session.get(MusicTrack, 1)
            entered.set()
            await release.wait()
            return value, 10
        with patch("app.discord_interactions.current_broadcast", side_effect=reading):
            task = asyncio.create_task(self.cache.refresh(self.sessions, self.root))
            await entered.wait()
            self.cache.invalidate()
            release.set()
            await task
        self.assertIsNone(self.cache.current())

    async def test_database_error_clears_cache_without_leaking_exception(self):
        await self.cache.refresh(self.sessions, self.root)
        self.assertIsNotNone(self.cache.current())
        with patch("app.discord_interactions.current_broadcast", side_effect=RuntimeError("private token")):
            await self.cache.refresh(self.sessions, self.root)
        self.assertIsNone(self.cache.current())

    async def test_cached_manual_cover_without_public_private_ticket(self):
        jpeg = b"\xff\xd8\xfffixture"
        async with self.sessions() as session:
            session.add(MusicEnrichment(track_id=1, original={}, result={"manual_artwork": {"source": "owner"}},
                artwork_data=jpeg, dismissed=True))
            await session.commit()
        await self.cache.refresh(self.sessions, self.root)
        self.assertEqual(self.cache.current()[0].artwork, jpeg)

    async def test_real_router_lifespan_refreshes_and_clears_on_shutdown(self):
        key = Ed25519PrivateKey.generate()
        settings = SimpleNamespace(discord_public_key=key.public_key().public_bytes_raw().hex(),
            discord_listen_url="https://redvps.site", music_root=self.root)
        app = FastAPI()
        app.include_router(build_router(settings, self.sessions, cache=self.cache))
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(2):
                while self.cache.current() is None:
                    await asyncio.sleep(.01)
            async with self.sessions() as session:
                item = await session.get(MusicSession, 1)
                item.share_site = False
                await session.commit()
            self.assertIsNone(self.cache.current())
        self.assertIsNone(self.cache.current())


if __name__ == "__main__":
    unittest.main()
