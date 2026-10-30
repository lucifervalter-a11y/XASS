"""Real storage API + unchanged PC transfer client, with temporary files only."""
from __future__ import annotations

import asyncio
import unittest

import httpx
import test_music_storage as fixtures
from app.music_storage_models import MusicStorageJob
from app.services.music_storage import part_path
from pc_client.music_storage import stored_path, transfer


class MusicRestoreRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MusicStorageTests.asyncSetUp
    asyncTearDown = fixtures.MusicStorageTests.asyncTearDown
    request = fixtures.MusicStorageTests.request
    replicate = fixtures.MusicStorageTests.replicate
    ack = fixtures.MusicStorageTests.ack
    evict = fixtures.MusicStorageTests.evict

    async def restore_job(self):
        self.assertEqual((await self.ack(await self.replicate())).status_code, 200)
        self.assertEqual((await self.evict()).status_code, 200)
        response = await self.request('POST', f'/api/mini/music/storage/{self.track_id}/restore')
        self.job_id = response.json()['job_id']
        self.partial = part_path(self.settings, self.job_id)
        self.rejected = self.partial.with_suffix('.rejected')
        self.pc_root = self.root / 'pc-replicas'; self.pc_root.mkdir()
        self.replica = stored_path(self.pc_root, self.sha); self.replica.write_bytes(self.bytes)
        self.original = self.root / 'user-original.wav'; self.original.write_bytes(self.bytes)
        self.other_partial = part_path(self.settings, 'f' * 32)
        self.other_partial.write_bytes(b'unrelated incomplete upload')
        self.calls = []
        return await self.poll_job()

    async def poll_job(self):
        response = await self.request('GET', '/agent/music-storage/jobs', headers=self.agent)
        self.assertEqual(response.status_code, 200, response.text)
        return next((j for j in response.json()['jobs'] if j['id'] == self.job_id), None)

    async def run_pc(self, job):
        """Use the actual synchronous PC client against the actual ASGI routes."""
        loop = asyncio.get_running_loop()
        def handler(request):
            future = asyncio.run_coroutine_threadsafe(self.client.request(
                request.method, str(request.url), headers={**dict(request.headers), **self.agent},
                content=request.content), loop)
            response = future.result(timeout=20)
            self.calls.append((request.method, request.url.path, request.url.params.get('offset'), response.status_code))
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)
        def run():
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                transfer(client, 'http://test', self.pc_root, job)
        await asyncio.to_thread(run)

    def assert_originals_preserved(self):
        self.assertEqual(self.original.read_bytes(), self.bytes)
        self.assertEqual(self.replica.read_bytes(), self.bytes)
        self.assertEqual(self.other_partial.read_bytes(), b'unrelated incomplete upload')

    async def finish(self):
        return await self.ack({'id': self.job_id})

    async def test_full_wrong_hash_recovers_on_next_poll_with_existing_pc_client(self):
        await self.restore_job()
        corrupt = b'x' * len(self.bytes); self.partial.write_bytes(corrupt)
        job = await self.poll_job()
        self.assertEqual(job['offset'], len(self.bytes))
        with self.assertRaises(httpx.HTTPStatusError):
            await self.run_pc(job)
        job = await self.poll_job()
        self.assertEqual(job['offset'], 0)
        self.assertEqual(job['error_code'], 'restore_checksum_retry')
        self.assertEqual(self.rejected.read_bytes(), corrupt)
        await self.run_pc(job)
        self.assertIsNone(await self.poll_job())
        self.assertEqual(self.path.read_bytes(), self.bytes)
        self.assertEqual([c[2] for c in self.calls if c[0] == 'PUT'], ['0'])
        self.assert_originals_preserved()

    async def test_normal_resume_and_idempotent_chunks_keep_partial_bytes(self):
        await self.restore_job(); self.partial.write_bytes(self.bytes[:19])
        endpoint = f'/agent/music-storage/jobs/{self.job_id}/chunk'
        replay = await self.request('PUT', endpoint, params={'offset': 0}, content=self.bytes[:19], headers=self.agent)
        self.assertEqual(replay.json()['offset'], 19)
        conflict = await self.request('PUT', endpoint, params={'offset': 0}, content=b'wrong', headers=self.agent)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.partial.read_bytes(), self.bytes[:19])
        await self.run_pc(await self.poll_job())
        self.assertEqual([c[2] for c in self.calls if c[0] == 'PUT'], ['19'])
        self.assertFalse(self.rejected.exists())
        self.assertEqual(self.path.read_bytes(), self.bytes)
        self.assert_originals_preserved()

    async def test_repeated_finish_after_reset_does_not_consume_another_retry(self):
        await self.restore_job(); self.partial.write_bytes(b'x' * len(self.bytes))
        for _ in range(3):
            response = await self.finish()
            self.assertEqual(response.status_code, 409)
        job = await self.poll_job()
        self.assertEqual(job['offset'], 0)
        self.assertIn(job['status'], {'pending', 'running'})
        await self.run_pc(job)
        self.assertEqual((await self.finish()).status_code, 200)
        self.assertEqual((await self.finish()).status_code, 200)
        self.assertEqual(self.path.read_bytes(), self.bytes)
        self.assert_originals_preserved()

    async def test_second_corrupt_attempt_stops_job_and_preserves_both_failed_files(self):
        await self.restore_job()
        first, second = b'x' * len(self.bytes), b'y' * len(self.bytes)
        self.partial.write_bytes(first); self.assertEqual((await self.finish()).status_code, 409)
        self.partial.write_bytes(second)
        for _ in range(3):
            self.assertEqual((await self.finish()).status_code, 409)
        self.assertIsNone(await self.poll_job())
        async with self.sessions() as session:
            value = await session.get(MusicStorageJob, self.job_id)
            self.assertEqual((value.status, value.error_code), ('failed', 'restore_checksum_failed'))
        self.assertEqual(self.rejected.read_bytes(), first)
        self.assertEqual(self.partial.read_bytes(), second)
        self.assertFalse(self.path.exists())
        self.assert_originals_preserved()

    async def test_short_partial_finish_preserves_resumable_data(self):
        await self.restore_job(); self.partial.write_bytes(self.bytes[:19])
        self.assertEqual((await self.finish()).status_code, 409)
        self.assertEqual(self.partial.read_bytes(), self.bytes[:19])
        self.assertFalse(self.rejected.exists())
        await self.run_pc(await self.poll_job())
        self.assertEqual(self.path.read_bytes(), self.bytes)
        self.assert_originals_preserved()

    async def test_wrong_ack_and_other_agent_cannot_reset_temp_file(self):
        await self.restore_job(); corrupt = b'x' * len(self.bytes); self.partial.write_bytes(corrupt)
        self.assertEqual((await self.ack({'id': self.job_id}, sha256='0' * 64)).status_code, 409)
        response = await self.request('POST', f'/agent/music-storage/jobs/{self.job_id}/finish',
                                      headers=self.other, json={'sha256': self.sha, 'size': len(self.bytes)})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.partial.read_bytes(), corrupt)
        self.assertFalse(self.rejected.exists())
        self.assert_originals_preserved()

    async def test_rename_before_sql_commit_recovers_without_replacing_valid_destination(self):
        await self.restore_job()
        self.partial.write_bytes(self.bytes); self.partial.replace(self.path)
        before = self.path.stat().st_mtime_ns
        self.assertEqual((await self.finish()).status_code, 200)
        self.assertEqual((await self.finish()).status_code, 200)
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertFalse(self.partial.exists())
        self.assertFalse(self.rejected.exists())
        self.assert_originals_preserved()

    async def test_quarantine_before_sql_commit_still_bounds_future_retry(self):
        await self.restore_job()
        first = b'x' * len(self.bytes)
        self.partial.write_bytes(first); self.partial.replace(self.rejected)
        # Simulated crash: filesystem rename persisted, DB retry flag did not.
        self.partial.write_bytes(b'y' * len(self.bytes))
        self.assertEqual((await self.finish()).status_code, 409)
        self.assertIsNone(await self.poll_job())
        self.assertEqual(self.rejected.read_bytes(), first)
        self.assertEqual(self.partial.read_bytes(), b'y' * len(self.bytes))
        self.assert_originals_preserved()

    async def test_quarantine_before_sql_commit_can_still_finish_a_valid_retry(self):
        await self.restore_job()
        self.partial.write_bytes(b'x' * len(self.bytes)); self.partial.replace(self.rejected)
        job = await self.poll_job(); self.assertEqual(job['offset'], 0)
        await self.run_pc(job)
        self.assertIsNone(await self.poll_job())
        self.assertEqual(self.path.read_bytes(), self.bytes)
        self.assert_originals_preserved()

    async def test_concurrent_duplicate_finish_preserves_one_retry(self):
        await self.restore_job(); self.partial.write_bytes(b'x' * len(self.bytes))
        responses = await asyncio.gather(self.finish(), self.finish())
        self.assertEqual([r.status_code for r in responses], [409, 409])
        job = await self.poll_job()
        self.assertIsNotNone(job); self.assertEqual(job['offset'], 0)
        await self.run_pc(job)
        self.assertEqual(self.path.read_bytes(), self.bytes)
        self.assert_originals_preserved()

    async def test_unavailable_pc_replica_never_resets_server_partial(self):
        await self.restore_job(); corrupt = b'x' * len(self.bytes); self.partial.write_bytes(corrupt)
        self.replica.write_bytes(b'corrupt replica')
        await self.run_pc(await self.poll_job())
        self.assertEqual(self.partial.read_bytes(), corrupt)
        self.assertEqual(self.replica.read_bytes(), b'corrupt replica')
        self.assertEqual(self.original.read_bytes(), self.bytes)
        self.assertFalse(self.rejected.exists())
        self.assertIsNone(await self.poll_job())
        self.assertEqual([c[1].rsplit('/', 1)[-1] for c in self.calls], ['unavailable'])


if __name__ == '__main__':
    unittest.main()
