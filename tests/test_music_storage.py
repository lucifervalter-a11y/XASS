from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db import Base, get_session
from app.models import AgentCredential, HeartbeatSource
from app.music_models import MusicTrack, MusicPlaylist
from app.music_storage_api import build_router
from app.music_storage_models import MusicStorageCopy, MusicStorageJob
from app.native_api import binding_hash, consume_native_proof, digest
from app.native_models import NativeDevice, NativeProof
from app.services.music_library import inspect_audio
from app.services.music_storage import check_vk_capabilities, part_path
from test_music_library import silent_wav


class MusicStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.music = self.root / "music"; self.music.mkdir()
        self.settings = SimpleNamespace(music_root=str(self.music), music_min_free_bytes=0,
            agent_api_key="legacy-global", profile_json_path=str(self.root / "profile.json"), vk_access_token="",
            pwa_session_generation_path=str(self.root / "generation"))
        self.engine = create_async_engine("sqlite+aiosqlite:///" + (self.root / "test.db").as_posix())
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async def dependency():
            async with self.sessions() as session:
                yield session
        async def owner(request: Request):
            if request.headers.get("x-owner") != "yes":
                raise HTTPException(403, "owner only")
            return SimpleNamespace(user_id=42)
        self.proofs = []
        async def proof(**kwargs):
            if kwargs["action_proof"].startswith("xna_"):
                if not await consume_native_proof(kwargs["session"], kwargs["action_proof"], kwargs["user"].user_id,
                        kwargs["purpose"], kwargs["binding"], self.settings):
                    raise HTTPException(428, "native proof required")
                return
            if kwargs["action_proof"] != "approved":
                raise HTTPException(428, "proof required")
            self.proofs.append(kwargs)
        self.app = FastAPI(); self.app.dependency_overrides[get_session] = dependency
        self.app.include_router(build_router(self.settings, owner, proof))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.owner = {"x-owner": "yes"}
        self.agent = {"X-Api-Key": "ag_fixture-one"}
        self.other = {"X-Api-Key": "ag_fixture-two"}
        self.bytes = silent_wav()
        self.path = self.music / ("a" * 32 + ".wav"); self.path.write_bytes(self.bytes)
        async with self.sessions() as session:
            for name, key in (("Мой ПК", "ag_fixture-one"), ("Other", "ag_fixture-two")):
                session.add(AgentCredential(source_name=name, api_key_hash=hashlib.sha256(key.encode()).hexdigest(),
                    key_hint="test", is_active=True))
                session.add(HeartbeatSource(source_name=name, source_type="PC_AGENT", is_online=True,
                    last_seen_at=datetime.now(timezone.utc), last_payload={"music_storage": {"version": 1}}))
            item = MusicTrack(**inspect_audio(self.path, "original.wav"), filename="original.wav", storage_name=self.path.name)
            session.add(item); await session.commit(); self.track_id = item.id; self.sha = item.sha256

    async def asyncTearDown(self):
        await self.client.aclose(); await self.engine.dispose(); self.temp.cleanup()

    async def request(self, method, path, **kwargs):
        return await self.client.request(method, path, headers=kwargs.pop("headers", self.owner), **kwargs)

    async def replicate(self):
        r = await self.request("POST", f"/api/mini/music/storage/{self.track_id}/replicate", json={"source_name": "Мой ПК"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["job"]

    async def ack(self, job, **kwargs):
        return await self.request("POST", f"/agent/music-storage/jobs/{job['id']}/finish", headers=self.agent,
            json={"sha256": self.sha, "size": len(self.bytes), **kwargs})

    async def evict(self, **kwargs):
        return await self.request("POST", f"/api/mini/music/storage/{self.track_id}/free-server-copy",
            json={"source_name": "Мой ПК", "sha256": self.sha, "confirm": "FREE SERVER COPY", "action_proof": "approved", **kwargs})

    async def test_owner_and_issued_agent_identity_are_required(self):
        self.assertEqual((await self.request("GET", "/api/mini/music/storage", headers={})).status_code, 403)
        job = await self.replicate()
        path = f"/agent/music-storage/jobs/{job['id']}/chunk"
        self.assertEqual((await self.request("GET", path, headers={})).status_code, 401)
        self.assertEqual((await self.request("GET", path, headers={"X-Api-Key": "legacy-global"})).status_code, 403)
        self.assertEqual((await self.request("GET", path, headers=self.other)).status_code, 404)
        self.assertEqual((await self.request("GET", path, headers=self.agent)).content, self.bytes)
        async with self.sessions() as session:
            key = await session.scalar(select(AgentCredential).where(AgentCredential.source_name == "Мой ПК"))
            key.is_active = False; await session.commit()
        self.assertEqual((await self.request("GET", path, headers=self.agent)).status_code, 401)

    async def test_replication_verification_explicit_eviction_and_restore_roundtrip(self):
        job = await self.replicate()
        self.assertEqual((await self.replicate())["id"], job["id"])
        self.assertEqual((await self.evict()).status_code, 409)
        self.assertEqual((await self.ack(job, sha256="0" * 64)).status_code, 409)
        self.assertEqual((await self.ack(job)).status_code, 200)
        self.assertEqual((await self.ack(job)).status_code, 200)
        self.assertTrue(self.path.is_file(), "replication must never evict the server copy")
        self.assertEqual((await self.evict(action_proof="")).status_code, 428)
        self.assertEqual((await self.evict(confirm="yes")).status_code, 409)
        self.assertEqual((await self.evict()).status_code, 200)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.proofs[-1]["binding"], {"source_name": "Мой ПК", "sha256": self.sha})
        r = await self.request("POST", f"/api/mini/music/storage/{self.track_id}/restore")
        self.assertEqual(r.json()["status"], "restore_pending")
        job_id = r.json()["job_id"]
        chunk = f"/agent/music-storage/jobs/{job_id}/chunk"
        a = await self.request("PUT", chunk, params={"offset": 0}, content=self.bytes[:50], headers=self.agent)
        self.assertEqual(a.json()["offset"], 50)
        self.assertEqual((await self.request("PUT", chunk, params={"offset": 0}, content=self.bytes[:50], headers=self.agent)).json()["offset"], 50)
        self.assertEqual((await self.request("PUT", chunk, params={"offset": 0}, content=b"changed", headers=self.agent)).status_code, 409)
        self.assertEqual((await self.ack({"id": job_id})).status_code, 409)
        await self.request("PUT", chunk, params={"offset": 50}, content=self.bytes[50:], headers=self.agent)
        self.assertEqual((await self.ack({"id": job_id})).status_code, 200)
        self.assertEqual(self.path.read_bytes(), self.bytes)

    async def test_idempotent_eviction_still_consumes_native_proof_and_blocks_replay(self):
        await self.ack(await self.replicate())
        self.assertEqual((await self.evict()).status_code, 200)
        token = "xna_fixture-single-use-eviction-authorization"
        async with self.sessions() as session:
            session.add(NativeDevice(id="d" * 32, owner_id=42, public_key="fixture-unused-public-key", generation=0))
            session.add(NativeProof(id=digest(token), owner_id=42, device_id="d" * 32,
                purpose=f"music:evict:{self.track_id}:{self.sha}", generation=0,
                binding_hash=binding_hash({"source_name": "Мой ПК", "sha256": self.sha}),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=60)))
            await session.commit()
        self.assertFalse(self.path.exists())
        result = await self.evict(action_proof=token)
        self.assertEqual(result.status_code, 200, result.text)
        async with self.sessions() as session:
            self.assertTrue((await session.get(NativeProof, digest(token))).used,
                "even an idempotent successful response must commit single-use proof consumption")
        self.path.write_bytes(self.bytes)  # A later restore must not be undone by replay.
        self.assertEqual((await self.evict(action_proof=token)).status_code, 428)
        self.assertEqual(self.path.read_bytes(), self.bytes)

    async def test_offline_agent_preserves_metadata_and_reports_unavailable(self):
        job = await self.replicate(); await self.ack(job); await self.evict()
        async with self.sessions() as session:
            source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == "Мой ПК"))
            source.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            await session.commit()
        response = await self.request("POST", f"/api/mini/music/storage/{self.track_id}/restore")
        self.assertEqual(response.json()["status"], "agent_offline")
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(MusicTrack, self.track_id))
            self.assertIsNotNone(await session.scalar(select(MusicStorageCopy)))

    async def test_corrupt_restore_cannot_replace_final_file(self):
        job = await self.replicate(); await self.ack(job); await self.evict()
        job_id = (await self.request("POST", f"/api/mini/music/storage/{self.track_id}/restore")).json()["job_id"]
        await self.request("PUT", f"/agent/music-storage/jobs/{job_id}/chunk", content=b"x" * len(self.bytes), headers=self.agent)
        self.assertEqual((await self.ack({"id": job_id})).status_code, 409)
        self.assertFalse(self.path.exists())

    async def test_expired_replica_and_changed_server_file_cannot_be_evicted(self):
        job = await self.replicate(); await self.ack(job)
        async with self.sessions() as session:
            copy = await session.scalar(select(MusicStorageCopy)); copy.verified_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await session.commit()
        self.assertEqual((await self.evict()).status_code, 409)
        job = await self.replicate(); await self.ack(job)
        self.path.write_bytes(b"changed")
        self.assertEqual((await self.evict()).status_code, 409)
        self.assertEqual(self.path.read_bytes(), b"changed")

    async def test_incremental_import_dedupes_tracks_and_preserves_playlist_order(self):
        async def run_import():
            r = await self.request("POST", "/api/mini/music/storage/import-directory", json={
                "source_name": "Мой ПК", "root": "documents", "path": "Music", "confirm": "IMPORT DIRECTORY"})
            self.assertEqual(r.status_code, 200, r.text); run_id = r.json()["import_id"]
            file_ids = []
            for index, data in enumerate((silent_wav(3), self.bytes)):
                digest = hashlib.sha256(data).hexdigest()
                r = await self.request("POST", f"/agent/music-storage/imports/{run_id}/files", headers=self.agent,
                    json={"path_key": hashlib.sha256(str(index).encode()).hexdigest(), "sha256": digest,
                          "size": len(data), "filename": f"{index}.wav", "ordinal": index})
                self.assertEqual(r.status_code, 200, r.text); record = r.json(); file_ids.append(record["file_id"])
                if not record["track_id"]:
                    prefix = f"/agent/music-storage/imports/{run_id}/files/{record['file_id']}"
                    self.assertEqual((await self.request("PUT", prefix + "/chunk", content=data, headers=self.agent)).status_code, 200)
                    result = await self.request("POST", prefix + "/finish", headers=self.agent)
                    self.assertEqual(result.status_code, 200, result.text)
            result = await self.request("POST", f"/agent/music-storage/imports/{run_id}/finish", headers=self.agent,
                                        json={"file_ids": file_ids})
            self.assertEqual(result.status_code, 200, result.text)
            return result.json()["result"]["playlist_id"]
        first, second = await run_import(), await run_import()
        self.assertEqual(first, second)
        async with self.sessions() as session:
            self.assertEqual(len(list(await session.scalars(select(MusicTrack)))), 2)
            playlist = await session.get(MusicPlaylist, first)
            self.assertEqual(playlist.track_ids[-1], self.track_id)
            self.assertEqual(len(playlist.track_ids), 2)

    async def test_import_requires_selected_root_confirmation_and_rejects_absolute_paths(self):
        for path in ("../private", "C:/Users", "/etc", "Music/../../Windows"):
            response = await self.request("POST", "/api/mini/music/storage/import-directory", json={
                "source_name": "Мой ПК", "root": "documents", "path": path, "confirm": "IMPORT DIRECTORY"})
            self.assertEqual(response.status_code, 400)

    async def test_restore_recovers_crash_after_file_rename_before_sql_commit(self):
        value = await self.replicate(); await self.ack(value); await self.evict()
        job_id = (await self.request("POST", f"/api/mini/music/storage/{self.track_id}/restore")).json()["job_id"]
        partial = part_path(self.settings, job_id); partial.write_bytes(self.bytes); partial.replace(self.path)
        self.assertEqual((await self.ack({"id": job_id})).status_code, 200)
        self.assertEqual(self.path.read_bytes(), self.bytes)

    async def test_import_cancel_blocks_further_agent_writes_without_deleting_originals(self):
        r = await self.request("POST", "/api/mini/music/storage/import-directory", json={
            "source_name": "Мой ПК", "root": "documents", "path": "Music", "confirm": "IMPORT DIRECTORY"})
        run_id = r.json()["import_id"]
        self.assertEqual((await self.request("DELETE", f"/api/mini/music/storage/imports/{run_id}")).json()["status"], "cancelled")
        response = await self.request("POST", f"/agent/music-storage/imports/{run_id}/files", headers=self.agent,
            json={"path_key": "b" * 64, "sha256": self.sha, "size": len(self.bytes), "filename": "a.wav", "ordinal": 0})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.path.exists())

    async def test_import_recovers_renamed_staging_file_and_retains_hash_dedupe(self):
        response = await self.request("POST", "/api/mini/music/storage/import-directory", json={
            "source_name": "Мой ПК", "root": "xass_files", "path": "", "confirm": "IMPORT DIRECTORY"})
        run_id = response.json()["import_id"]
        data = silent_wav(5); digest = hashlib.sha256(data).hexdigest()
        payload = {"path_key": "e" * 64, "sha256": digest, "size": len(data), "filename": "five.wav", "ordinal": 0}
        response = await self.request("POST", f"/agent/music-storage/imports/{run_id}/files", json=payload, headers=self.agent)
        file_id = response.json()["file_id"]
        destination = self.music / (file_id + ".wav"); destination.write_bytes(data)
        registered = await self.request("POST", f"/agent/music-storage/imports/{run_id}/files", json=payload, headers=self.agent)
        self.assertEqual(registered.json()["offset"], len(data))
        response = await self.request("POST", f"/agent/music-storage/imports/{run_id}/files/{file_id}/finish", headers=self.agent)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(destination.read_bytes(), data)

    async def test_import_same_file_restores_cold_track_without_changing_id(self):
        job = await self.replicate(); await self.ack(job); await self.evict()
        response = await self.request("POST", "/api/mini/music/storage/import-directory", json={
            "source_name": "Мой ПК", "root": "xass_files", "path": "", "confirm": "IMPORT DIRECTORY"})
        run_id = response.json()["import_id"]
        response = await self.request("POST", f"/agent/music-storage/imports/{run_id}/files", headers=self.agent,
            json={"path_key": "f" * 64, "sha256": self.sha, "size": len(self.bytes), "filename": "original.wav", "ordinal": 0})
        record = response.json()
        self.assertIsNone(record["track_id"])
        self.assertEqual(record["offset"], 0)
        prefix = f"/agent/music-storage/imports/{run_id}/files/{record['file_id']}"
        self.assertEqual((await self.request("PUT", prefix + "/chunk", content=self.bytes, headers=self.agent)).status_code, 200)
        result = await self.request("POST", prefix + "/finish", headers=self.agent)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["track_id"], self.track_id)
        self.assertEqual(self.path.read_bytes(), self.bytes)


class VkCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_status_token_does_not_claim_music_export_access(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"response": [{"id": 42}]} if request.url.path.endswith("users.get") else {"response": {"text": "status"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            value = await check_vk_capabilities("private-token", client=client)
        self.assertTrue(value["identity_verified"])
        self.assertTrue(value["capabilities"]["now_playing"])
        self.assertFalse(value["capabilities"]["file_export"])
        self.assertEqual(value["reason_code"], "music_access_not_granted")
        self.assertTrue(all("private-token" not in str(r.url) for r in requests))
        self.assertNotIn("private-token", str(value))

    async def test_denied_and_unavailable_are_not_success(self):
        for response, expected in ((httpx.Response(200, json={"error": {"error_code": 5}}), "invalid_or_denied"),
                                   (httpx.Response(503), "service_unavailable")):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
                value = await check_vk_capabilities("token", client=client)
                self.assertEqual(value["token_status"], expected)
                self.assertIsNot(value["identity_verified"], True)
