from __future__ import annotations

import base64
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, get_session
from app.models import AgentCommand, AgentCredential, HeartbeatSource
from app.music_api import build_router
from app.music_models import MusicSession, MusicTrack, MusicUpload
from app.services.music_library import CHUNK_BYTES, issue_ticket
from app.services.music_broadcast import current_broadcast
from test_music_library import silent_wav


class MusicApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = SimpleNamespace(music_root=str(self.root / "music"), music_max_upload_bytes=2 * 1024 * 1024,
            music_min_free_bytes=0, bot_token="fixture-bot", setup_api_key="fixture-setup",
            pwa_session_generation_path=str(self.root / "generation"), profile_json_path=str(self.root / "profile.json"),
            profile_public_url="https://fixture.invalid")
        self.engine = create_async_engine("sqlite+aiosqlite:///" + (self.root / "test.db").as_posix())
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def dependency():
            async with self.sessions() as session:
                yield session

        async def owner(request: Request):
            value = request.headers.get("x-test-owner")
            if not value:
                raise HTTPException(401, "not authenticated")
            if value == "guest":
                raise HTTPException(403, "not owner")
            return SimpleNamespace(user_id=int(value), is_owner=True)

        self.app = FastAPI()
        self.app.dependency_overrides[get_session] = dependency
        self.app.include_router(build_router(self.settings, owner, lambda _: ("https", "fixture.invalid")))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.headers = {"x-test-owner": "42"}
        self.audio = silent_wav()

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()
        self.temp.cleanup()

    async def request(self, method, path, **kwargs):
        return await self.client.request(method, path, headers=kwargs.pop("headers", self.headers), **kwargs)

    async def start(self, data=None, name="fixture.wav"):
        data = self.audio if data is None else data
        response = await self.request("POST", "/api/mini/music/uploads", json={"filename": name, "size": len(data)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["upload_id"]

    async def chunk(self, upload_id, data, offset=0, **kwargs):
        return await self.request("PUT", f"/api/mini/music/uploads/{upload_id}",
                                  json={"offset": offset, "data": base64.b64encode(data).decode()}, **kwargs)

    async def upload(self, data=None, name="fixture.wav"):
        data = self.audio if data is None else data
        upload_id = await self.start(data, name)
        self.assertEqual((await self.chunk(upload_id, data)).status_code, 200)
        response = await self.request("POST", f"/api/mini/music/uploads/{upload_id}/finish")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["track"]

    async def ticket(self, track, purpose="listen"):
        response = await self.request("POST", f"/api/mini/music/tracks/{track['id']}/ticket", json={"purpose": purpose})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["path"]

    async def test_owner_auth_required_for_every_private_route_and_guest_denied(self):
        routes = [("GET", "/library"), ("POST", "/uploads"), ("PUT", "/uploads/" + "a" * 32),
                  ("POST", "/uploads/" + "a" * 32 + "/finish"), ("PATCH", "/tracks/1"),
                  ("DELETE", "/tracks/1"), ("POST", "/playlists"), ("PUT", "/playlists/1"),
                  ("DELETE", "/playlists/1"), ("POST", "/tracks/1/ticket"), ("POST", "/control"),
                  ("GET", "/control/1"), ("GET", "/session"), ("POST", "/session"),
                  ("DELETE", "/uploads/" + "a" * 32), ("GET", "/players")]
        for method, path in routes:
            for headers, status in (({}, 401), ({"x-test-owner": "guest"}, 403)):
                with self.subTest(method=method, path=path, status=status):
                    response = await self.request(method, "/api/mini/music" + path, headers=headers, json={})
                    self.assertEqual(response.status_code, status, response.text)

    async def test_chunk_offsets_retries_owner_binding_finish_and_deduplication(self):
        upload_id = await self.start()
        finish = f"/api/mini/music/uploads/{upload_id}/finish"
        self.assertEqual((await self.request("POST", finish)).status_code, 409)
        self.assertEqual((await self.chunk(upload_id, self.audio[:40], offset=1)).status_code, 409)
        self.assertEqual((await self.chunk(upload_id, self.audio[:40], headers={"x-test-owner": "43"})).status_code, 404)
        self.assertEqual((await self.chunk(upload_id, self.audio[:40])).json()["offset"], 40)
        self.assertEqual((await self.chunk(upload_id, self.audio[:40])).json()["offset"], 40)
        self.assertEqual((await self.chunk(upload_id, b"changed")).status_code, 409)
        self.assertEqual((await self.chunk(upload_id, self.audio[40:], 40)).json()["offset"], len(self.audio))
        result = await self.request("POST", finish)
        self.assertEqual(result.status_code, 200, result.text)
        track = result.json()["track"]
        self.assertEqual((await self.request("POST", finish)).json()["track"]["id"], track["id"])
        self.assertEqual((await self.chunk(upload_id, self.audio[:40])).json()["track_id"], track["id"])
        duplicate = await self.upload(name="same-bytes.wav")
        self.assertEqual(duplicate["id"], track["id"])
        self.assertEqual(len((await self.request("GET", "/api/mini/music/library")).json()["tracks"]), 1)
        self.assertEqual(len(list((self.root / "music").glob("*.wav"))), 1)
        self.assertEqual(list((self.root / "music").glob("*.part")), [])

    async def test_malformed_oversize_expired_and_incomplete_uploads_fail_closed(self):
        for payload, status in (({"filename": "a.exe", "size": 8}, 400), ({"filename": "a.wav", "size": 0}, 422),
                                ({"filename": "a.wav", "size": self.settings.music_max_upload_bytes + 1}, 413)):
            result = await self.request("POST", "/api/mini/music/uploads", json=payload)
            self.assertEqual(result.status_code, status, result.text)
        upload_id = await self.start()
        path = f"/api/mini/music/uploads/{upload_id}"
        result = await self.request("PUT", path, json={"offset": 0, "data": "not%base64"})
        self.assertEqual(result.status_code, 400)
        self.assertEqual((await self.chunk(upload_id, b"x" * (CHUNK_BYTES + 1))).status_code, 413)
        async with self.sessions() as session:
            item = await session.get(MusicUpload, upload_id)
            item.created_at = datetime.now(timezone.utc) - timedelta(days=2)
            await session.commit()
        self.assertEqual((await self.chunk(upload_id, self.audio)).status_code, 410)
        bad_id = await self.start(b"not real audio")
        self.assertEqual((await self.chunk(bad_id, b"not real audio")).status_code, 200)
        self.assertEqual((await self.request("POST", f"/api/mini/music/uploads/{bad_id}/finish")).status_code, 422)

    async def test_favorites_search_playlists_and_soft_delete_revoke_streams(self):
        one = await self.upload()
        two = await self.upload(silent_wav(6), "second.wav")
        path = await self.ticket(one)
        response = await self.request("PATCH", f"/api/mini/music/tracks/{one['id']}",
                                      json={"title": "100%_song", "artist": "Автор", "favorite": True})
        self.assertEqual(response.status_code, 200)
        favorite = (await self.request("GET", "/api/mini/music/library?favorite=true")).json()["tracks"]
        self.assertEqual([item["id"] for item in favorite], [one["id"]])
        searched = (await self.request("GET", "/api/mini/music/library", params={"q": "%_"})).json()["tracks"]
        self.assertEqual([item["id"] for item in searched], [one["id"]])
        playlist = await self.request("POST", "/api/mini/music/playlists", json={"name": "Плейлист", "track_ids": [two["id"], one["id"], two["id"]]})
        self.assertEqual(playlist.status_code, 200, playlist.text)
        playlist_id = playlist.json()["playlist"]["id"]
        tracks = (await self.request("GET", "/api/mini/music/library", params={"playlist": playlist_id})).json()["tracks"]
        self.assertEqual([item["id"] for item in tracks], [two["id"], one["id"]])
        invalid = await self.request("PUT", f"/api/mini/music/playlists/{playlist_id}", json={"name": "bad", "track_ids": [999999]})
        self.assertEqual(invalid.status_code, 400)
        removed = await self.request("DELETE", f"/api/mini/music/tracks/{one['id']}")
        self.assertTrue(removed.json()["file_preserved"])
        self.assertEqual((await self.client.get(path)).status_code, 404)
        self.assertEqual((await self.request("POST", f"/api/mini/music/tracks/{one['id']}/ticket", json={})).status_code, 404)
        library = (await self.request("GET", "/api/mini/music/library")).json()
        self.assertEqual(library["playlists"][0]["track_ids"], [two["id"]])
        self.assertEqual(len(list((self.root / "music").glob("*.wav"))), 2)
        self.assertEqual((await self.request("DELETE", f"/api/mini/music/playlists/{playlist_id}")).status_code, 200)

    async def test_stream_supports_range_head_download_and_rejects_wrong_purpose(self):
        track = await self.upload()
        path = await self.ticket(track)
        self.assertEqual((await self.client.get(f"/api/music/tracks/{track['id']}/stream")).status_code, 401)
        response = await self.client.get(path, headers={"Range": "bytes=0-15"})
        self.assertEqual(response.status_code, 206, response.text)
        self.assertEqual(response.content, self.audio[:16])
        self.assertEqual(response.headers["content-range"], f"bytes 0-15/{len(self.audio)}")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        response = await self.client.head(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(int(response.headers["content-length"]), len(self.audio))
        self.assertEqual((await self.client.get(path, headers={"Range": "bytes=999999-"})).status_code, 416)
        download = await self.client.get(await self.ticket(track, "download"))
        self.assertTrue(download.headers["content-disposition"].startswith("attachment"))
        agent_ticket = issue_ticket(self.settings, track["id"], purpose="agent", binding="test")
        self.assertEqual((await self.client.get(f"/api/music/tracks/{track['id']}/stream", params={"ticket": agent_ticket})).status_code, 401)
        Path(self.settings.pwa_session_generation_path).write_text("1")
        self.assertEqual((await self.client.get(path)).status_code, 401)

    async def test_agent_stream_ticket_is_bound_to_an_active_credential(self):
        track = await self.upload()
        binding = hashlib.sha256(b"fixture-agent-key").hexdigest()
        async with self.sessions() as session:
            session.add(AgentCredential(source_name="PC", api_key_hash=binding, key_hint="fixture", is_active=True))
            await session.commit()
        value = issue_ticket(self.settings, track["id"], purpose="agent", binding=binding)
        path = f"/agent/music/tracks/{track['id']}/stream?ticket={value}"
        self.assertEqual((await self.client.get(path)).status_code, 200)
        listen = issue_ticket(self.settings, track["id"], purpose="listen")
        self.assertEqual((await self.client.get(f"/agent/music/tracks/{track['id']}/stream?ticket={listen}")).status_code, 401)
        async with self.sessions() as session:
            credential = await session.scalar(select(AgentCredential))
            credential.is_active = False
            await session.commit()
        self.assertEqual((await self.client.get(path)).status_code, 403)

    async def test_cancel_upload_removes_only_selected_unfinished_file(self):
        first, other = await self.start(), await self.start()
        await self.chunk(first, self.audio[:40])
        await self.chunk(other, self.audio[:40])
        path = f"/api/mini/music/uploads/{first}"
        wrong = await self.request("DELETE", path, headers={"x-test-owner": "43"})
        self.assertEqual(wrong.status_code, 404)
        self.assertTrue((self.root / "music" / (first + ".part")).is_file())
        response = await self.request("DELETE", path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse((self.root / "music" / (first + ".part")).exists())
        self.assertTrue((self.root / "music" / (other + ".part")).is_file())
        self.assertEqual((await self.chunk(first, self.audio)).status_code, 404)
        await self.chunk(other, self.audio[40:], 40)
        self.assertEqual((await self.request("POST", f"/api/mini/music/uploads/{other}/finish")).status_code, 200)
        self.assertEqual((await self.request("DELETE", f"/api/mini/music/uploads/{other}")).status_code, 409)
        self.assertEqual(len(list((self.root / "music").glob("*.wav"))), 1)

    async def test_players_and_controls_reject_old_offline_unpaired_agents_and_coalesce_pending(self):
        track = await self.upload()
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            for name in ("Ready", "Old", "Offline", "Unpaired"):
                session.add(HeartbeatSource(source_name=name, source_type="PC_AGENT", is_online=name != "Offline",
                    last_seen_at=now if name != "Offline" else now - timedelta(minutes=5),
                    last_payload={} if name == "Old" else {"agent_version": "0.16.0", "music_player": {"state": "idle"}}))
                if name != "Unpaired":
                    session.add(AgentCredential(source_name=name, key_hint="fixture", is_active=True,
                        api_key_hash=hashlib.sha256(name.encode()).hexdigest()))
            await session.commit()
        response = await self.request("GET", "/api/mini/music/players")
        self.assertEqual(response.status_code, 200, response.text)
        players = {item["source_name"]: item for item in response.json()["players"]}
        self.assertTrue(players["Ready"]["available"])
        for name in ("Old", "Offline", "Unpaired", "Missing"):
            if name in players:
                self.assertFalse(players[name]["available"])
            result = await self.request("POST", "/api/mini/music/control", json={"source_name": name, "action": "play", "track_id": track["id"]})
            self.assertEqual(result.status_code, 409, result.text)
        commands = []
        for _ in range(2):
            result = await self.request("POST", "/api/mini/music/control", json={"source_name": "Ready", "action": "play", "track_id": track["id"]})
            self.assertEqual(result.status_code, 200, result.text)
            commands.append(result.json()["command_id"])
        async with self.sessions() as session:
            first, current = [await session.get(AgentCommand, value) for value in commands]
            self.assertEqual(first.status, "cancelled")
            self.assertEqual(current.status, "pending")
            self.assertEqual(current.command, "music_play")
            self.assertGreater(current.payload["expires_at"], now.timestamp())
            media = current.payload["media_path"]
            self.assertTrue(current.payload["url"].startswith("https://fixture.invalid/agent/music/"))
            self.assertNotIn("api_key", current.payload)
        self.assertEqual((await self.client.get(media)).status_code, 200)
        result = await self.request("GET", f"/api/mini/music/control/{commands[-1]}")
        self.assertEqual(result.json()["status"], "pending")

    async def test_concurrent_repeated_finish_is_idempotent(self):
        upload_id = await self.start()
        await self.chunk(upload_id, self.audio)
        path = f"/api/mini/music/uploads/{upload_id}/finish"
        results = await asyncio.gather(self.request("POST", path), self.request("POST", path))
        self.assertTrue(all(response.status_code == 200 for response in results), [item.text for item in results])
        self.assertEqual(results[0].json()["track"]["id"], results[1].json()["track"]["id"])
        self.assertEqual(len(list((self.root / "music").glob("*.wav"))), 1)

    async def test_agent_broadcast_uses_fresh_matching_player_state_not_stale_site_session(self):
        track = await self.upload()
        async with self.sessions() as session:
            # A longer duration isolates heartbeat expiry from natural track end.
            item = await session.get(MusicTrack, track["id"])
            item.duration = 600
            source = HeartbeatSource(source_name="PC", source_type="PC_AGENT", is_online=True,
                last_payload={"music_player": {"track_id": track["id"], "state": "playing", "position_sec": 10}})
            session.add_all([source, MusicSession(id=1, track_id=track["id"], device="agent:PC",
                state="playing", share_site=True, session_key="fixture-session")])
            await session.commit()
            self.assertIsNotNone(await current_broadcast(session))
            for details in ({"track_id": track["id"] + 1, "state": "playing"},
                            {"track_id": track["id"], "state": "paused"},
                            {"track_id": track["id"], "state": "playing", "position_sec": "not a number"},
                            {"track_id": track["id"], "state": "playing", "position_sec": float("nan")},
                            "bad-state"):
                source.last_payload = {"music_player": details}
                self.assertIsNone(await current_broadcast(session))
            source.last_payload = {"music_player": {"track_id": track["id"], "state": "playing", "position_sec": 10}}
            source.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=121)
            self.assertIsNone(await current_broadcast(session))

    async def test_public_broadcast_exposes_only_current_opted_in_track_and_revokes_on_change(self):
        one, two = await self.upload(), await self.upload(silent_wav(6), "second.wav")
        key = "fixture-session-key"
        payload = {"session_key": key, "track_id": one["id"], "state": "playing", "position": 0}
        response = await self.request("POST", "/api/mini/music/session", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse((await self.client.get("/api/music/public")).json()["playing"])
        self.assertEqual((await self.request("POST", "/api/mini/music/session", json={**payload, "share_site": True})).status_code, 200)
        public = (await self.client.get("/api/music/public")).json()
        self.assertTrue(public["playing"])
        self.assertEqual(public["track"]["id"], one["id"])
        self.assertNotIn("tracks", public)
        self.assertNotIn("filename", public["track"])
        path = public["path"]
        self.assertEqual((await self.client.get(path)).status_code, 200)
        conflict = await self.request("POST", "/api/mini/music/session", json={**payload, "session_key": "another-session-key"})
        self.assertEqual(conflict.status_code, 409)
        await self.request("POST", "/api/mini/music/session", json={**payload, "track_id": two["id"]})
        self.assertEqual((await self.client.get(path)).status_code, 403)
        new_path = (await self.client.get("/api/music/public")).json()["path"]
        await self.request("POST", "/api/mini/music/session", json={**payload, "track_id": two["id"], "share_site": False})
        self.assertFalse((await self.client.get("/api/music/public")).json()["playing"])
        self.assertEqual((await self.client.get(new_path)).status_code, 403)


    async def test_broadcast_restores_previous_source_and_partial_toggle_preserves_playback(self):
        from app.services.profile_editor import load_profile, save_profile
        profile_path = Path(self.settings.profile_json_path)
        save_profile(profile_path, {"now_listening_source": "vk"})
        track = await self.upload()
        key = "fixture-session-key"
        self.assertEqual((await self.request("POST", "/api/mini/music/session", json={
            "session_key": key, "track_id": track["id"], "state": "playing", "share_site": True
        })).status_code, 200)
        self.assertEqual(load_profile(profile_path)["now_listening_source"], "xass_music")
        await self.request("POST", "/api/mini/music/session", json={"session_key": key, "share_site": False})
        self.assertEqual(load_profile(profile_path)["now_listening_source"], "vk")
        state = (await self.request("GET", "/api/mini/music/session")).json()["session"]
        self.assertEqual(state["state"], "playing")
        self.assertEqual(state["track_id"], track["id"])
        self.assertEqual((await self.request("POST", "/api/mini/music/session", json={
            "session_key": key, "share_discord": True
        })).status_code, 409, "unconfigured Discord must not claim success")


if __name__ == "__main__":
    unittest.main()
