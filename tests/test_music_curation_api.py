from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, Request
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, get_session
from app.music_curation_api import build_router
from app.music_curation_models import MusicCurationBatch
from app.music_enrichment_api import build_router as enrichment_router
from app.music_models import MusicEnrichment, MusicTrack
from app.services.music_curation import revision_token
from test_music_library import silent_wav

PREFIX = "/api/mini/music/curation"


class MusicCurationApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_async_engine("sqlite+aiosqlite:///" + (self.root / "test.db").as_posix())
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def dependency():
            async with self.sessions() as session:
                yield session

        async def owner(request: Request):
            value = request.headers.get("x-owner")
            if not value:
                raise HTTPException(401)
            if value == "guest":
                raise HTTPException(403)
            return SimpleNamespace(user_id=int(value))

        self.owner = owner
        self.app = FastAPI()
        self.app.dependency_overrides[get_session] = dependency
        self.app.include_router(build_router(SimpleNamespace(music_root=str(self.root)), owner))
        self.app.include_router(enrichment_router(owner))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        async with self.sessions() as session:
            for index in (1, 2):
                data = silent_wav(index)
                name = f"{index:032x}.wav"
                (self.root / name).write_bytes(data)
                session.add(MusicTrack(id=index, title=f"Old {index}", artist="Artist", album="Album",
                    filename=f"Old {index}.wav", storage_name=name, size=len(data), sha256=str(index) * 64,
                    duration=index, mime="audio/wav", deleted=False))
            await session.commit()

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()
        self.temp.cleanup()

    async def request(self, method, path, **kwargs):
        return await self.client.request(method, PREFIX + path, headers=kwargs.pop("headers", {"x-owner": "42"}), **kwargs)

    async def body(self, ids=(1, 2), key="request_fixture"):
        changes = []
        async with self.sessions() as session:
            for key_id in ids:
                track = await session.get(MusicTrack, key_id)
                record = await session.get(MusicEnrichment, key_id)
                changes.append({"track_id": key_id, "revision_token": revision_token(track, record), "tags": {"title": f"New {key_id}"}})
        return {"confirmed": True, "request_key": key, "changes": changes}

    async def titles(self):
        async with self.sessions() as session:
            return list(await session.scalars(select(MusicTrack.title).order_by(MusicTrack.id)))

    async def test_every_route_requires_owner(self):
        for method, path in (("GET", "/tracks"), ("POST", "/audit"), ("GET", "/batches"),
                             ("POST", "/batches"), ("POST", "/batches/none/undo")):
            for headers, expected in (({}, 401), ({"x-owner": "guest"}, 403)):
                response = await self.request(method, path, headers=headers, json={})
                self.assertEqual(response.status_code, expected, response.text)

    async def test_audit_bounded_private_nonmutating_and_pagination(self):
        first = await self.request("GET", "/tracks?limit=1")
        self.assertEqual(first.json()["next_after_id"], 1)
        self.assertEqual((await self.request("GET", "/tracks?after_id=1")).json()["tracks"][0]["id"], 2)
        response = await self.request("POST", "/audit", json={"track_ids": [1, 2]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.json()["categories"]["no_artwork"], [1, 2])
        for ids in ([True], ["1"], [1, 1], list(range(1, 22))):
            self.assertEqual((await self.request("POST", "/audit", json={"track_ids": ids})).status_code, 422)
        self.assertEqual((await self.request("POST", "/audit", json={"track_ids": [999]})).status_code, 404)
        self.assertEqual(await self.titles(), ["Old 1", "Old 2"])

    async def test_executor_admission_stays_bounded_after_client_cancellation(self):
        entered, release = threading.Barrier(3), threading.Event()
        def blocked(*args, **kwargs):
            entered.wait(timeout=5)
            release.wait(timeout=5)
            return {"tracks": []}
        with patch("app.music_curation_api.audit_tracks", side_effect=blocked):
            tasks = [asyncio.create_task(self.request("POST", "/audit", json={"track_ids": [1]})) for _ in range(2)]
            try:
                await asyncio.to_thread(entered.wait, 5)
                tasks[0].cancel()
                await asyncio.gather(tasks[0], return_exceptions=True)
                busy = await self.request("POST", "/audit", json={"track_ids": [1]})
                self.assertEqual(busy.status_code, 429)
                self.assertEqual(busy.headers["retry-after"], "5")
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_batch_atomic_journal_undo_and_idempotent_retry(self):
        before_files = {path.name: path.read_bytes() for path in self.root.glob("*.wav")}
        body = await self.body()
        response = await self.request("POST", "/batches", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()["batch"]
        self.assertEqual(batch["entries"][0]["before"]["title"], "Old 1")
        self.assertEqual(await self.titles(), ["New 1", "New 2"])
        self.assertEqual((await self.request("POST", "/batches", json=body)).json()["batch"]["id"], batch["id"])
        path = f"/batches/{batch['id']}/undo"
        undo = await self.request("POST", path, json={"confirmed": True})
        self.assertEqual(undo.status_code, 200, undo.text)
        self.assertEqual(await self.titles(), ["Old 1", "Old 2"])
        self.assertEqual((await self.request("POST", path, json={"confirmed": True})).json()["batch"]["state"], "undone")
        # Retrying the original key cannot silently reapply an already undone batch.
        self.assertEqual((await self.request("POST", "/batches", json=body)).json()["batch"]["state"], "undone")
        self.assertEqual(before_files, {path.name: path.read_bytes() for path in self.root.glob("*.wav")})

    async def test_stale_second_track_rolls_back_first_and_journal(self):
        body = await self.body()
        body["changes"][1]["revision_token"] = "0" * 64
        response = await self.request("POST", "/batches", json=body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(await self.titles(), ["Old 1", "Old 2"])
        self.assertEqual((await self.request("GET", "/batches")).json(), {"batches": []})

    async def test_later_edit_blocks_entire_undo_even_same_metadata(self):
        response = await self.request("POST", "/batches", json=await self.body())
        batch = response.json()["batch"]
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, 2)
            record.revision += 1
            await session.commit()
        response = await self.request("POST", f"/batches/{batch['id']}/undo", json={"confirmed": True})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(await self.titles(), ["New 1", "New 2"])

    async def test_confirmation_keys_invalid_fields_and_owner_binding(self):
        body = await self.body()
        body["confirmed"] = False
        self.assertEqual((await self.request("POST", "/batches", json=body)).status_code, 400)
        body["confirmed"] = True
        result = await self.request("POST", "/batches", json=body)
        batch = result.json()["batch"]
        body["changes"][0]["tags"]["title"] = "Another title"
        self.assertEqual((await self.request("POST", "/batches", json=body)).status_code, 409)
        self.assertEqual((await self.request("GET", "/batches", headers={"x-owner": "43"})).json(), {"batches": []})
        self.assertEqual((await self.request("POST", f"/batches/{batch['id']}/undo",
            headers={"x-owner": "43"}, json={"confirmed": True})).status_code, 404)
        body["request_key"] = "new_request"
        body["changes"][0]["tags"]["filename"] = "../../outside"
        self.assertEqual((await self.request("POST", "/batches", json=body)).status_code, 422)

    async def test_concurrent_overlapping_batches_allow_only_one_revision(self):
        first, second = await self.body(key="request_one"), await self.body(key="request_two")
        responses = await asyncio.gather(self.request("POST", "/batches", json=first), self.request("POST", "/batches", json=second))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertEqual(len((await self.request("GET", "/batches")).json()["batches"]), 1)

    async def test_concurrent_identical_request_is_one_durable_batch(self):
        body = await self.body()
        responses = await asyncio.gather(self.request("POST", "/batches", json=body), self.request("POST", "/batches", json=body))
        self.assertEqual([response.status_code for response in responses], [200, 200], [r.text for r in responses])
        self.assertEqual(responses[0].json()["batch"]["id"], responses[1].json()["batch"]["id"])

    async def test_concurrent_undo_is_idempotent_and_deleted_member_blocks_batch(self):
        batch = (await self.request("POST", "/batches", json=await self.body())).json()["batch"]
        path = f"/batches/{batch['id']}/undo"
        async with self.sessions() as session:
            track = await session.get(MusicTrack, 2)
            track.deleted = True
            await session.commit()
        self.assertEqual((await self.request("POST", path, json={"confirmed": True})).status_code, 404)
        self.assertEqual(await self.titles(), ["New 1", "New 2"])
        async with self.sessions() as session:
            track = await session.get(MusicTrack, 2)
            track.deleted = False
            await session.commit()
        responses = await asyncio.gather(self.request("POST", path, json={"confirmed": True}),
                                         self.request("POST", path, json={"confirmed": True}))
        self.assertEqual([r.status_code for r in responses], [200, 200], [r.text for r in responses])
        self.assertEqual(await self.titles(), ["Old 1", "Old 2"])

    async def test_database_commit_failure_has_no_partial_tags_or_journal(self):
        body = await self.body()
        from sqlalchemy.ext.asyncio import AsyncSession
        with patch.object(AsyncSession, "commit", side_effect=RuntimeError("injected disk error")):
            with self.assertRaises(RuntimeError):
                await self.request("POST", "/batches", json=body)
        self.assertEqual(await self.titles(), ["Old 1", "Old 2"])
        self.assertEqual((await self.request("GET", "/batches")).json(), {"batches": []})

    async def test_undo_preserves_existing_art_lyrics_original_and_monotonic_revision(self):
        async with self.sessions() as session:
            session.add(MusicEnrichment(track_id=1, fingerprint="f" * 64, original={"title": "Imported"},
                result={"status": "matched"}, owner_lyrics={"text": "owner text"}, artwork_data=b"cover",
                dismissed=False, revision=12))
            await session.commit()
        batch = (await self.request("POST", "/batches", json=await self.body(ids=(1,)))).json()["batch"]
        with patch("app.music_enrichment_api.catalog_lookup", AsyncMock(side_effect=AssertionError("automatic tag overwrite"))):
            response = await self.client.post("/api/mini/music/tracks/1/enrichment", headers={"x-owner": "42"}, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["track"]["title"], "New 1")
        self.assertTrue(response.json()["enrichment"]["artwork_available"])
        self.assertTrue(response.json()["enrichment"]["owner_lyrics_available"])
        self.assertEqual(response.json()["enrichment"]["curation_tag_override"]["source"], "owner_batch")
        await self.request("POST", f"/batches/{batch['id']}/undo", json={"confirmed": True})
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, 1)
            self.assertEqual(record.artwork_data, b"cover")
            self.assertEqual(record.owner_lyrics, {"text": "owner text"})
            self.assertEqual(record.original, {"title": "Imported"})
            self.assertEqual(record.revision, 14)
            self.assertFalse(record.dismissed)
            self.assertNotIn("curation_tag_override", record.result)

    async def test_completed_transcript_counts_only_for_same_audio_identity(self):
        from app.transcription_models import TranscriptionJob
        async with self.sessions() as session:
            session.add(TranscriptionJob(track_id=1, sha256="1" * 64, state="done",
                result={"lines": [{"start": 0, "end": 1, "text": "Fixture"}]}))
            session.add(TranscriptionJob(track_id=2, sha256="0" * 64, state="done",
                result={"lines": [{"start": 0, "end": 1, "text": "Stale"}]}))
            await session.commit()
        report = await self.request("POST", "/audit", json={"track_ids": [1, 2]})
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()["categories"]["no_lyrics"], [2])

    async def test_background_cover_cannot_replace_manual_or_batch_tags(self):
        from app.services.music_covers import _remember_names
        batch = (await self.request("POST", "/batches", json=await self.body(ids=(1,)))).json()["batch"]
        await _remember_names(self.sessions, 1, "New 1", "Artist", ("Catalog artist", "Catalog title"))
        self.assertEqual(await self.titles(), ["New 1", "Old 2"])
        # A skipped cover write does not advance revision and invalidate Undo.
        response = await self.request("POST", f"/batches/{batch['id']}/undo", json={"confirmed": True})
        self.assertEqual(response.status_code, 200, response.text)
        async with self.sessions() as session:
            record = await session.get(MusicEnrichment, 1)
            record.dismissed = True
            await session.commit()
        await _remember_names(self.sessions, 1, "Old 1", "Artist", ("Catalog artist", "Catalog title"))
        self.assertEqual(await self.titles(), ["Old 1", "Old 2"])
        # Automatic adoption remains available for untouched imports.
        await _remember_names(self.sessions, 2, "Old 2", "Artist", ("Catalog artist", "Catalog title"))
        self.assertEqual(await self.titles(), ["Old 1", "Catalog title"])

    async def test_late_artwork_request_cannot_overwrite_same_value_batch_choice(self):
        from app.music_api import build_router as music_router
        self.app.include_router(music_router(SimpleNamespace(music_root=str(self.root)), self.owner,
            lambda _: ("https", "fixture.invalid")))
        entered, release = asyncio.Event(), asyncio.Event()
        async def lookup(*args, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return b"\xff\xd8\xfffixture"
        with patch("app.services.music_artwork.artwork_thumbnail", return_value=None), \
             patch("app.services.music_covers.display_cover", side_effect=lookup), \
             patch("app.services.music_covers.adopted_catalog_names", return_value=("Catalog artist", "Catalog title")), \
             patch("app.services.music_artwork.store_external_thumbnail", return_value=None):
            request = asyncio.create_task(self.client.get("/api/mini/music/tracks/1/artwork", headers={"x-owner": "42"}))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                body = await self.body(ids=(1,))
                body["changes"][0]["tags"]["title"] = "Old 1"
                batch_response = await self.request("POST", "/batches", json=body)
                self.assertEqual(batch_response.status_code, 200, batch_response.text)
                release.set()
                result = await asyncio.wait_for(request, timeout=5)
                self.assertEqual(result.status_code, 200, result.text)
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)
        self.assertEqual(await self.titles(), ["Old 1", "Old 2"])
        batch = batch_response.json()["batch"]
        self.assertEqual((await self.request("POST", f"/batches/{batch['id']}/undo", json={"confirmed": True})).status_code, 200)

    async def test_filename_adoption_helper_keeps_reviewed_names_but_handles_untouched_import(self):
        from app.music_enrichment_api import adopt_filename_names
        async with self.sessions() as session:
            for key in (1, 2):
                row = await session.get(MusicTrack, key)
                row.title, row.artist = f"Artist - Song {key}", ""
            await session.commit()
        body = await self.body(ids=(1,))
        body["changes"][0]["tags"]["title"] = "Artist - Song 1"
        self.assertEqual((await self.request("POST", "/batches", json=body)).status_code, 200)
        for key in (1, 2):
            async with self.sessions() as session:
                await adopt_filename_names(session, await session.get(MusicTrack, key))
                await session.commit()
        self.assertEqual(await self.titles(), ["Artist - Song 1", "Song 2"])

    async def test_filename_adoption_race_cannot_bypass_same_value_batch_or_start_lookup(self):
        from app.music_enrichment_api import adopt_filename_names
        async with self.sessions() as session:
            row = await session.get(MusicTrack, 1)
            row.title, row.artist = "Artist - Song", ""
            await session.commit()
        body = await self.body(ids=(1,))
        body["changes"][0]["tags"]["title"] = "Artist - Song"
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_adopt(session, track):
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            await adopt_filename_names(session, track)
        with patch("app.music_enrichment_api.adopt_filename_names", side_effect=delayed_adopt), \
             patch("app.music_enrichment_api.catalog_lookup", AsyncMock(side_effect=AssertionError("lookup after owner choice"))):
            request = asyncio.create_task(self.client.post("/api/mini/music/tracks/1/enrichment",
                headers={"x-owner": "42"}, json={}))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                batch_response = await self.request("POST", "/batches", json=body)
                self.assertEqual(batch_response.status_code, 200, batch_response.text)
                release.set()
                response = await asyncio.wait_for(request, timeout=5)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["track"]["title"], "Artist - Song")
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)
        self.assertEqual(await self.titles(), ["Artist - Song", "Old 2"])
