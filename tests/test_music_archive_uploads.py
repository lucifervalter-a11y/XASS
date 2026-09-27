from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
import httpx
from pydantic import ValidationError

from app.config import Settings
from app.music_api import CHUNK_JSON_BYTES, UploadChunk, build_router
from app.music_models import MusicTrack, MusicUpload
from app.services import music_ingest
from app.services.music_library import CHUNK_BYTES
import test_music_api as fixtures
from test_music_library import silent_wav


def archive_bytes(entries=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in entries or [("01 first.wav", silent_wav(5)), ("02 second.wav", silent_wav(6))]:
            archive.writestr(name, data)
    return output.getvalue()


class MusicArchiveUploadTests(unittest.IsolatedAsyncioTestCase):
    request = fixtures.MusicApiTests.request
    start = fixtures.MusicApiTests.start
    chunk = fixtures.MusicApiTests.chunk

    async def asyncSetUp(self):
        await fixtures.MusicApiTests.asyncSetUp(self)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self):
        if self.lifespan is not None:
            await self.lifespan.__aexit__(None, None, None)
        await fixtures.MusicApiTests.asyncTearDown(self)

    async def archive(self, entries=None):
        body = archive_bytes(entries)
        identity = await self.start(body, "Моя музыка.zip")
        for offset in range(0, len(body), CHUNK_BYTES):
            self.assertEqual((await self.chunk(identity, body[offset:offset + CHUNK_BYTES], offset)).status_code, 200)
        return identity

    async def finish(self, identity, *, asynchronous=True):
        kwargs = {"json": {"async": True}} if asynchronous else {}
        return await self.request("POST", f"/api/mini/music/uploads/{identity}/finish", **kwargs)

    async def completed(self, identity, *, request=None):
        request = request or self.request
        async with asyncio.timeout(5):
            while True:
                response = await request("GET", f"/api/mini/music/uploads/{identity}")
                self.assertEqual(response.status_code, 200, response.text)
                if response.json()["status"] != "processing":
                    return response.json()
                await asyncio.sleep(0.01)

    async def test_archive_317_mib_start_uses_separate_bounded_limit_without_allocating_archive(self):
        settings = Settings(_env_file=None)
        self.assertEqual(settings.music_max_upload_bytes, 128 * 1024 * 1024)
        self.assertEqual(settings.music_max_archive_upload_bytes, 512 * 1024 * 1024)
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, music_max_archive_upload_bytes=1024 * 1024 * 1024 + 1)
        page = (await self.request("GET", "/api/mini/music/library")).json()
        self.assertEqual(page["max_archive_upload_bytes"], 512 * 1024 * 1024)
        self.assertEqual(page["max_upload_bytes"], self.settings.music_max_upload_bytes)
        for name, size, expected in (("Music.zip", 317 * 1024 * 1024, 200),
                                     ("Music.mp3", 317 * 1024 * 1024, 413),
                                     ("Music.zip", 512 * 1024 * 1024 + 1, 413),
                                     ("Music.zip", 1024 * 1024 * 1024 + 1, 422)):
            with patch("app.music_api.shutil.disk_usage", return_value=SimpleNamespace(free=4 * 1024 ** 3)):
                result = await self.request("POST", "/api/mini/music/uploads", json={"filename": name, "size": size})
            self.assertEqual(result.status_code, expected, result.text)
        self.assertEqual(list(Path(self.settings.music_root).glob("*.part")), [])
        self.settings.music_max_archive_upload_bytes = 100
        rejected = await self.request("POST", "/api/mini/music/uploads", json={"filename": "custom.zip", "size": 101})
        self.assertEqual(rejected.status_code, 413)

    async def test_archive_total_may_exceed_single_track_limit_but_each_track_may_not(self):
        self.settings.music_max_upload_bytes = 100_000
        self.settings.music_max_archive_upload_bytes = 512_000
        identity = await self.archive()
        response = await self.finish(identity, asynchronous=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["added"], 2)
        self.assertNotIn("status", response.json(), "Legacy finish keeps its final receipt contract")
        identity = await self.archive([("too-big.wav", silent_wav(7))])
        response = await self.finish(identity, asynchronous=False)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual((await self.request("GET", "/api/mini/music/library")).json()["total"], 2)

    async def test_async_status_auth_dedup_cancel_and_expired_cleanup_do_not_touch_active_job(self):
        identity = await self.archive()
        path = Path(self.settings.music_root) / (identity + ".part")
        entered, release = asyncio.Event(), asyncio.Event()
        real_ingest = music_ingest.ingest_path
        calls = 0

        async def delayed(*args, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return await real_ingest(*args, **kwargs)

        with patch.object(music_ingest, "ingest_path", side_effect=delayed):
            response = await self.finish(identity)
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["retry_after"], 1)
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual((await self.finish(identity)).status_code, 202)
            status_path = f"/api/mini/music/uploads/{identity}"
            for headers, expected in (({}, 401), ({"x-test-owner": "guest"}, 403), ({"x-test-owner": "43"}, 404)):
                self.assertEqual((await self.request("GET", status_path, headers=headers)).status_code, expected)
            poll = await self.request("GET", status_path)
            self.assertEqual(poll.json()["status"], "processing")
            self.assertEqual(poll.headers["cache-control"], "private, no-store")
            self.assertEqual((await self.request("DELETE", status_path)).status_code, 409)
            async with self.sessions() as session:
                item = await session.get(MusicUpload, identity)
                item.created_at = datetime.now(timezone.utc) - timedelta(days=2)
                await session.commit()
            await self.start()
            self.assertTrue(path.is_file(), "Lazy expiration must not unlink a running extractor's source")
            release.set()
            receipt = await self.completed(identity)
        self.assertEqual(calls, 1)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["added"], 2)
        self.assertFalse(path.exists())
        retry = await self.finish(identity)
        self.assertEqual(retry.json(), receipt)
        legacy = await self.finish(identity, asynchronous=False)
        self.assertEqual(legacy.json()["tracks"], receipt["tracks"])
        self.assertEqual((await self.request("DELETE", status_path)).status_code, 409)

    async def test_only_one_extractor_and_one_waiter_are_allowed(self):
        identities = [await self.archive() for _ in range(3)]
        entered, release = asyncio.Event(), asyncio.Event()
        active = maximum = calls = 0

        async def delayed(*args, **kwargs):
            nonlocal active, maximum, calls
            calls += 1; active += 1; maximum = max(maximum, active)
            entered.set()
            try:
                await release.wait()
                return music_ingest.ImportResult()
            finally:
                active -= 1

        with patch.object(music_ingest, "ingest_path", side_effect=delayed):
            self.assertEqual((await self.finish(identities[0])).status_code, 202)
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual((await self.finish(identities[1])).status_code, 202)
            self.assertEqual((await self.finish(identities[2])).status_code, 429)
            self.assertEqual(calls, 1)
            release.set()
            for identity in identities[:2]:
                self.assertEqual((await self.completed(identity))["status"], "completed")
        self.assertEqual(maximum, 1)
        self.assertEqual(calls, 2)

    async def test_failed_worker_is_sanitized_keeps_part_and_can_be_cancelled(self):
        identity = await self.archive()
        path = Path(self.settings.music_root) / (identity + ".part")
        with patch.object(music_ingest, "ingest_path", side_effect=RuntimeError("secret-url?token=never-log-this")), \
             self.assertLogs("app.music_api", level="WARNING") as logs:
            self.assertEqual((await self.finish(identity)).status_code, 202)
            result = await self.completed(identity)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["retryable"])
        self.assertNotIn("never-log-this", str(result) + str(logs.output))
        self.assertTrue(path.exists())
        self.assertEqual((await self.request("DELETE", f"/api/mini/music/uploads/{identity}")).status_code, 200)
        self.assertFalse(path.exists())

    async def test_restart_reports_intact_upload_ready_then_persists_final_receipt(self):
        identity = await self.archive()
        entered = asyncio.Event()

        async def interrupted(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(music_ingest, "ingest_path", side_effect=interrupted):
            await self.finish(identity)
            await asyncio.wait_for(entered.wait(), 2)
            await self.lifespan.__aexit__(None, None, None)
            self.lifespan = None
        path = Path(self.settings.music_root) / (identity + ".part")
        self.assertTrue(path.is_file())
        restarted = FastAPI()
        restarted.dependency_overrides.update(self.app.dependency_overrides)
        restarted.include_router(build_router(self.settings, lambda: SimpleNamespace(user_id=42), lambda _: ("https", "fixture.invalid")))
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://test") as client:
                result = await client.get(f"/api/mini/music/uploads/{identity}")
                self.assertEqual(result.json()["status"], "ready_to_finish")
                result = await client.post(f"/api/mini/music/uploads/{identity}/finish", json={"async": True})
                self.assertEqual(result.status_code, 202)
                completed = await self.completed(identity, request=client.request)
                self.assertEqual(completed["added"], 2)
        self.assertFalse(path.exists())
        # The original router also reads the persisted receipt, not process memory.
        self.assertEqual((await self.request("GET", f"/api/mini/music/uploads/{identity}")).json(), completed)

    async def test_chunk_body_is_bounded_before_json_decode_for_declared_and_chunked_requests(self):
        identity = await self.start()
        route = f"/api/mini/music/uploads/{identity}"
        consumed = 0

        async def oversized():
            nonlocal consumed
            for _ in range(40):
                consumed += 64 * 1024
                yield b"x" * (64 * 1024)

        with patch.object(UploadChunk, "model_validate_json") as decoder:
            response = await self.request("PUT", route, headers={**self.headers, "Content-Length": str(CHUNK_JSON_BYTES + 1)}, content=oversized())
            self.assertEqual(response.status_code, 413)
            self.assertEqual(consumed, 0)
            response = await self.request("PUT", route, content=oversized())
            self.assertEqual(response.status_code, 413)
            self.assertLessEqual(consumed, CHUNK_JSON_BYTES + 64 * 1024)
            decoder.assert_not_called()
        self.assertFalse((Path(self.settings.music_root) / (identity + ".part")).exists())

    async def test_legal_escaped_base64_slashes_keep_full_chunk_compatible_with_ios_json(self):
        data = b"\xff" * CHUNK_BYTES
        identity = await self.start(data)
        body = json.dumps({"offset": 0, "data": base64.b64encode(data).decode()}).replace("/", "\\/").encode()
        self.assertGreater(len(body), CHUNK_BYTES * 2)
        self.assertLessEqual(len(body), CHUNK_JSON_BYTES)
        response = await self.request("PUT", f"/api/mini/music/uploads/{identity}",
            headers={**self.headers, "Content-Type": "application/json"}, content=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["offset"], len(data))
        self.assertEqual((Path(self.settings.music_root) / (identity + ".part")).read_bytes(), data)

    async def test_shutdown_waits_for_unpack_thread_and_retains_retryable_source(self):
        identity = await self.archive()
        source = Path(self.settings.music_root) / (identity + ".part")
        entered, release = threading.Event(), threading.Event()
        source_still_present = []
        unpack = music_ingest.unpack_music_zip

        def delayed_unpack(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Fixture thread release timed out")
            source_still_present.append(source.is_file())
            return unpack(*args)

        with patch.object(music_ingest, "unpack_music_zip", side_effect=delayed_unpack):
            await self.finish(identity)
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            shutdown = asyncio.create_task(self.lifespan.__aexit__(None, None, None))
            self.lifespan = None
            try:
                await asyncio.sleep(0.01)
                self.assertFalse(shutdown.done(), "Worker must stop before staging cleanup")
                self.assertTrue(source.is_file())
            finally:
                release.set()
                await asyncio.wait_for(shutdown, 3)
        self.assertEqual(source_still_present, [True])
        self.assertTrue(source.is_file(), "Shutdown never deletes the durable upload")
        self.assertEqual(list((Path(self.settings.music_root) / ".telegram-ingest").glob("incoming-*")), [])
        self.assertEqual((await self.request("GET", f"/api/mini/music/uploads/{identity}")).json()["status"], "ready_to_finish")

    async def test_completed_receipt_cleans_only_its_interrupted_temporary_archive(self):
        identity = await self.archive()
        await self.finish(identity)
        receipt = await self.completed(identity)
        source = Path(self.settings.music_root) / (identity + ".part")
        other = Path(self.settings.music_root) / ("f" * 32 + ".part")
        source.write_bytes(b"simulated shutdown after receipt commit")
        other.write_bytes(b"unrelated unfinished upload")
        result = await self.request("GET", f"/api/mini/music/uploads/{identity}")
        self.assertEqual(result.json(), receipt)
        self.assertFalse(source.exists())
        self.assertEqual(other.read_bytes(), b"unrelated unfinished upload")
        async with self.sessions() as session:
            for value in receipt["tracks"]:
                track = await session.get(MusicTrack, value["id"])
                self.assertTrue((Path(self.settings.music_root) / track.storage_name).is_file())

    async def test_archive_quota_and_expanded_disk_reserve_remain_enforced(self):
        self.settings.music_min_free_bytes = 100
        with patch("app.music_api.shutil.disk_usage", return_value=SimpleNamespace(free=999)):
            response = await self.request("POST", "/api/mini/music/uploads", json={"filename": "Music.zip", "size": 900})
        self.assertEqual(response.status_code, 507)
        self.settings.music_min_free_bytes = 0
        identity = await self.archive()
        with patch("app.services.music_ingest.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
            response = await self.finish(identity, asynchronous=False)
        self.assertEqual(response.status_code, 422)
        self.assertEqual((await self.request("GET", "/api/mini/music/library")).json()["total"], 0)


if __name__ == "__main__":
    unittest.main()
