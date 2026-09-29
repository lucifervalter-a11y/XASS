"""Server-side PC transcription queue: scheduling, leases, reassignment, dedupe and API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import AgentCredential, HeartbeatSource
from app.music_models import MusicEnrichment, MusicTrack
from app.services import transcription_queue as tq
from app.services.synced_lyrics import LyricsCache, resolve
from app.transcription_models import TranscriptionJob, TranscriptionWorker
import test_music_api
from app.services import synced_lyrics

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
GPU = {"gpu": True, "gpu_name": "RTX", "vram_mb": 12288, "cpu_cores": 8, "ram_mb": 32768}
BIG_CPU = {"gpu": False, "vram_mb": 0, "cpu_cores": 16, "ram_mb": 65536}
SMALL_CPU = {"gpu": False, "vram_mb": 0, "cpu_cores": 4, "ram_mb": 8192}
IDLE = {"cpu_percent": 5, "gpu_percent": 3, "busy": False}


class QueueServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine("sqlite+aiosqlite:///" + (Path(self.temp.name) / "q.db").as_posix())
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def worker(self, session, credential_id, caps, load=IDLE, now=T0, enabled=True, state="ready"):
        return await tq.upsert_worker(session, credential_id=credential_id, source_name=f"pc{credential_id}",
                                      enabled=enabled, state=state, detail="", capabilities=caps, load=load, now=now)

    def track(self, track_id=1, duration=200.0):
        return SimpleNamespace(id=track_id, sha256="a" * 64, duration=duration)

    async def test_scheduler_prefers_gpu_vram_then_cpu_cores_and_skips_busy_offline_disabled(self):
        async with self.sessions() as session:
            small = await self.worker(session, 1, SMALL_CPU)
            big = await self.worker(session, 2, BIG_CPU)
            gpu = await self.worker(session, 3, GPU)
            job, created = await tq.request_job(session, self.track(), language="ru", user_id=1, now=T0)
            self.assertTrue(created)
            self.assertEqual((job.state, job.worker_id), ("assigned", gpu.id))
            # Second job goes to the next most powerful free worker (max one job per worker).
            job2, _ = await tq.request_job(session, self.track(2), language="ru", user_id=1, now=T0)
            self.assertEqual(job2.worker_id, big.id)
            job3, _ = await tq.request_job(session, self.track(3), language="ru", user_id=1, now=T0)
            self.assertEqual(job3.worker_id, small.id)
            job4, _ = await tq.request_job(session, self.track(4), language="ru", user_id=1, now=T0)
            self.assertEqual((job4.state, job4.worker_id), ("queued", None))

    async def test_cpu_or_gpu_over_95_blocks_and_the_client_busy_flag_does_not(self):
        async with self.sessions() as session:
            await self.worker(session, 1, GPU, load={"cpu_percent": 20, "gpu_percent": 96})
            await self.worker(session, 2, BIG_CPU, load={"cpu_percent": 96})
            await self.worker(session, 4, GPU, now=T0 - timedelta(seconds=tq.WORKER_STALE_SEC + 1))
            await self.worker(session, 5, GPU, enabled=False)
            await self.worker(session, 6, GPU, state="installing")
            job, _ = await tq.request_job(session, self.track(), language="ru", user_id=1, now=T0)
            self.assertEqual(job.state, "queued")
            status = await tq.status_payload(session, 1, T0)
            self.assertEqual(status["status"], "queued")
            desktop = await self.worker(session, 3, SMALL_CPU,
                                        load={"cpu_percent": 82, "gpu_percent": 58, "busy": True})
            await tq.schedule(session, T0)
            self.assertEqual(job.worker_id, desktop.id)
            edge = await self.worker(session, 7, GPU, load={"cpu_percent": 95, "gpu_percent": None, "busy": True})
            job2, _ = await tq.request_job(session, self.track(2), language="ru", user_id=1, now=T0)
            self.assertEqual(job2.worker_id, edge.id)

    async def test_waiting_for_pc_when_no_worker_is_online(self):
        async with self.sessions() as session:
            await tq.request_job(session, self.track(), language="ru", user_id=1, now=T0)
            status = await tq.status_payload(session, 1, T0)
            self.assertEqual(status["status"], "waiting_for_pc")
            self.assertEqual(status["message"], "Расшифруем, когда включится компьютер")
            self.assertEqual((await tq.status_payload(session, 99, T0))["status"], "none")

    async def test_preparing_pc_status_shows_setup_percent_of_installing_worker(self):
        async with self.sessions() as session:
            await tq.request_job(session, self.track(), language="ru", user_id=1, now=T0)
            await tq.upsert_worker(session, credential_id=1, source_name="pc1", enabled=True, state="installing", detail="",
                                   capabilities=GPU, load=IDLE, now=T0, setup={"stage": "torch", "percent": 12})
            second = await tq.upsert_worker(session, credential_id=2, source_name="pc2", enabled=True, state="installing",
                                            detail="", capabilities=BIG_CPU, load=IDLE, now=T0, setup={"stage": "models", "percent": 250})
            self.assertEqual(second.setup, {"stage": "models", "percent": 100})
            # A stale or disabled installer does not count.
            await tq.upsert_worker(session, credential_id=3, source_name="pc3", enabled=False, state="installing", detail="",
                                   capabilities=GPU, load=IDLE, now=T0, setup={"stage": "torch", "percent": 99})
            await tq.upsert_worker(session, credential_id=2, source_name="pc2", enabled=True, state="installing", detail="",
                                   capabilities=BIG_CPU, load=IDLE, now=T0 - timedelta(seconds=tq.WORKER_STALE_SEC + 1),
                                   setup={"stage": "models", "percent": 90})
            status = await tq.status_payload(session, 1, T0)
            self.assertEqual((status["status"], status["setup_percent"]), ("preparing_pc", 12))
            self.assertEqual(status["message"], "ПК готовится к расшифровке, 12%")
            # Once it is ready the job is offered to it; setup progress is cleared.
            ready = await self.worker(session, 1, GPU)
            self.assertEqual(ready.setup, {})
            await tq.schedule(session, T0)
            status = await tq.status_payload(session, 1, T0)
            self.assertEqual(status["status"], "queued")
            self.assertIsNone(status["setup_percent"])
            # No installer at all: back to waiting_for_pc.
            await self.worker(session, 1, GPU, enabled=False)
            status = await tq.status_payload(session, 1, T0)
            self.assertEqual((status["status"], status["setup_percent"]), ("waiting_for_pc", None))

    async def test_dedupe_one_job_per_track_and_done_is_never_rerun(self):
        async with self.sessions() as session:
            gpu = await self.worker(session, 1, GPU)
            first, created = await tq.request_job(session, self.track(), language="ru", user_id=1, now=T0)
            again, created_again = await tq.request_job(session, self.track(), language="en", user_id=2, now=T0)
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(first.id, again.id)
            job = await tq.claim_assigned(session, gpu, T0)
            await tq.complete(session, gpu, job, lines=[{"start": 1, "end": 2, "text": "раз"}], meta={}, now=T0)
            again, created = await tq.request_job(session, self.track(), language="ru", user_id=3, now=T0 + timedelta(days=1))
            self.assertFalse(created)
            self.assertEqual(again.state, "done")
            self.assertEqual(len(list(await session.scalars(select(TranscriptionJob)))), 1)
            result = await tq.done_result(session, 1)
            self.assertEqual(result["source"], "pc_transcription")
            self.assertEqual(result["lines"], [{"start": 1.0, "end": 2.0, "text": "раз"}])

    async def test_unclaimed_offer_expires_and_goes_to_next_worker_without_spending_attempt(self):
        async with self.sessions() as session:
            gpu = await self.worker(session, 1, GPU)
            cpu = await self.worker(session, 2, BIG_CPU)
            await self.worker(session, 3, BIG_CPU, load={"cpu_percent": 96})
            job, _ = await tq.request_job(session, self.track(), language="ru", user_id=1, now=T0)
            self.assertEqual(job.worker_id, gpu.id)
            # A second job occupies the CPU worker, so after expiry the GPU is the only free one...
            later = T0 + timedelta(seconds=tq.ASSIGN_TTL_SEC + 1)
            cpu.last_seen_at = gpu.last_seen_at = later
            await tq.schedule(session, later)
            # ...but the CPU worker is preferred because the GPU never picked it up.
            self.assertEqual((job.state, job.worker_id, job.attempts), ("assigned", cpu.id, 0))
            self.assertIn(gpu.id, job.failed_workers)

    async def test_running_lease_timeout_and_deadline_reassign_then_fail_after_max_attempts(self):
        async with self.sessions() as session:
            gpu = await self.worker(session, 1, GPU)
            cpu = await self.worker(session, 2, BIG_CPU)
            job, _ = await tq.request_job(session, self.track(duration=100), language="ru", user_id=1, now=T0)
            self.assertIs(await tq.claim_assigned(session, gpu, T0), job)
            self.assertEqual(job.state, "running")
            # Renewed lease keeps it running; no renewal -> reassigned to the next worker.
            t1 = T0 + timedelta(seconds=tq.RUN_LEASE_SEC - 10)
            await tq.renew(session, gpu, job, stage="separate", fraction=.3, now=t1)
            await tq.expire_leases(session, T0 + timedelta(seconds=tq.RUN_LEASE_SEC + 5))
            self.assertEqual(job.state, "running")
            t2 = t1 + timedelta(seconds=tq.RUN_LEASE_SEC + 1)
            for item in (gpu, cpu):
                item.last_seen_at = t2
            await tq.schedule(session, t2)
            self.assertEqual((job.state, job.worker_id, job.attempts, job.error), ("assigned", cpu.id, 1, "lease_expired"))
            await tq.claim_assigned(session, cpu, t2)
            # Hard deadline even with fresh renewals.
            t3 = t2 + timedelta(seconds=tq.MIN_DEADLINE_SEC + 1)
            job.lease_expires_at = t3 + timedelta(minutes=1)
            await tq.expire_leases(session, t3)
            self.assertEqual((job.state, job.attempts, job.error), ("queued", 2, "timeout"))
            for item in (gpu, cpu):
                item.last_seen_at = t3
            await tq.schedule(session, t3)
            running = await tq.claim_assigned(session, gpu, t3)
            state = await tq.fail(session, gpu, running, reason="cuda_oom", now=t3)
            self.assertEqual((state, job.attempts, job.error), ("failed", 3, "cuda_oom"))
            # Explicit owner retry of a failed job resets attempts.
            retry, created = await tq.request_job(session, self.track(duration=100), language="ru", user_id=1, now=t3)
            self.assertFalse(created)
            self.assertEqual((retry.state, retry.attempts), ("assigned", 0))

    async def test_failure_reassigns_to_other_worker_first_and_same_worker_only_as_last_resort(self):
        async with self.sessions() as session:
            gpu = await self.worker(session, 1, GPU)
            cpu = await self.worker(session, 2, SMALL_CPU)
            job, _ = await tq.request_job(session, self.track(), language="ru", user_id=1, now=T0)
            await tq.claim_assigned(session, gpu, T0)
            await tq.fail(session, gpu, job, reason="demucs_failed", now=T0)
            self.assertEqual(job.worker_id, cpu.id)
            await tq.claim_assigned(session, cpu, T0)
            await tq.fail(session, cpu, job, reason="whisper_failed", now=T0)
            # Both failed once; the most recent failure goes last, so the GPU retries.
            self.assertEqual((job.state, job.worker_id), ("assigned", gpu.id))

    async def test_restarted_worker_loses_its_running_job_and_disabled_worker_releases_offer(self):
        async with self.sessions() as session:
            gpu = await self.worker(session, 1, GPU)
            job, _ = await tq.request_job(session, self.track(), language="ru", user_id=1, now=T0)
            await tq.claim_assigned(session, gpu, T0)
            await tq.release_worker_jobs(session, gpu, T0, running_job_id=job.id)
            self.assertEqual(job.state, "running")
            await tq.release_worker_jobs(session, gpu, T0, running_job_id=None)
            self.assertEqual((job.state, job.attempts, job.error), ("queued", 1, "worker_lost_job"))
            await tq.schedule(session, T0)
            gpu = await self.worker(session, 1, GPU, enabled=False)
            await tq.release_worker_jobs(session, gpu, T0, running_job_id=None)
            self.assertEqual((job.state, job.attempts), ("queued", 1))

    async def test_complete_cleans_lines_and_empty_result_is_a_failure(self):
        async with self.sessions() as session:
            gpu = await self.worker(session, 1, GPU)
            job, _ = await tq.request_job(session, self.track(duration=10), language="ru", user_id=1, now=T0)
            await tq.claim_assigned(session, gpu, T0)
            result = await tq.complete(session, gpu, job, lines=[{"start": 99, "end": 100, "text": "late"}], meta={}, now=T0)
            self.assertFalse(result["accepted"])
            self.assertEqual(job.state, "assigned")  # requeued and re-offered
            await tq.claim_assigned(session, gpu, T0)
            result = await tq.complete(session, gpu, job, lines=[{"start": 3, "end": 2, "text": "  b  "},
                {"start": 1, "end": 2, "text": "a"}], meta={"model": "large-v3", "device": "cuda"}, now=T0)
            self.assertTrue(result["accepted"])
            self.assertEqual(job.result["lines"], [{"start": 1.0, "end": 2.0, "text": "a"}, {"start": 3.0, "end": 3.2, "text": "b"}])
            self.assertEqual(job.result["model"], "large-v3")

    async def test_empty_outage_is_missing_text_and_real_words_are_not(self):
        self.assertFalse(tq.shown_lyrics_missing(None))
        self.assertFalse(tq.shown_lyrics_missing({"status": "unavailable", "text": "есть", "synced": False, "lines": []}))
        self.assertTrue(tq.shown_lyrics_missing({"status": "unavailable", "text": "", "synced": False, "lines": []}))
        self.assertTrue(tq.shown_lyrics_missing({"status": "rate_limited", "text": "  ", "synced": False, "lines": []}))
        self.assertFalse(tq.shown_lyrics_missing({"text": "есть", "synced": True, "lines": [{"text": "есть"}]}))

    async def test_sweep_queues_a_song_with_no_text_and_skips_stored_lyrics(self):
        async with self.sessions() as session:
            spoken = MusicTrack(title="has text", artist="a", album="", filename="a.mp3", storage_name="a" * 32 + ".mp3",
                                mime="audio/mpeg", sha256="a" * 64, size=10, duration=10, deleted=False)
            bare = MusicTrack(title="no text", artist="b", album="", filename="b.mp3", storage_name="b" * 32 + ".mp3",
                              mime="audio/mpeg", sha256="b" * 64, size=10, duration=10, deleted=False)
            session.add_all([spoken, bare])
            await session.flush()
            session.add(MusicEnrichment(track_id=spoken.id, fingerprint="x", original={},
                                        result={"lyrics": {"text": "уже есть"}}, owner_lyrics={}, checked_at=T0))
            await session.commit()
            created = await tq.queue_page_without_lyrics(session, [], user_id=1, now=T0)
            self.assertEqual(created, 1)
            jobs = list(await session.scalars(select(TranscriptionJob)))
            self.assertEqual([job.track_id for job in jobs], [bare.id])
            self.assertEqual(await tq.queue_page_without_lyrics(session, [], user_id=1, now=T0), 0)
            self.assertEqual(len(list(await session.scalars(select(TranscriptionJob)))), 1)

    async def test_running_status_has_minutes_estimate(self):
        async with self.sessions() as session:
            gpu = await self.worker(session, 1, GPU)
            await tq.request_job(session, self.track(duration=240), language="ru", user_id=1, now=T0)
            job = await tq.claim_assigned(session, gpu, T0)
            status = await tq.status_payload(session, 1, T0 + timedelta(seconds=30))
            self.assertEqual(status["status"], "running")
            self.assertGreaterEqual(status["estimate_minutes"], 1)
            self.assertIn("мин", status["message"])
            await tq.renew(session, gpu, job, stage="transcribe", fraction=.5, now=T0 + timedelta(seconds=120))
            status = await tq.status_payload(session, 1, T0 + timedelta(seconds=120))
            self.assertEqual(status["estimate_minutes"], 2)


class ResolveOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_first_then_pc_transcription_then_plain(self):
        track = SimpleNamespace(id=5, title="t", artist="a", album="", duration=100, filename="t.mp3", sha256="b" * 64)
        pc = {"lines": [{"start": 1, "end": 2, "text": "auto"}]}
        synced = {"status": "synced", "synced": True, "lines": [{"start": 0, "end": 1, "text": "cat"}], "text": "cat", "source": "lrclib"}
        plain = {"status": "plain", "synced": False, "lines": [], "text": "plain", "source": "lrclib"}
        for looked, expected in ((synced, "lrclib"), (plain, "pc_transcription"), ({"status": "not_found"}, "pc_transcription")):
            client = SimpleNamespace(lookup=AsyncMock(return_value=looked))
            value = await resolve(track, owner=None, embedded=None, enrichment=None, cache=LyricsCache(None),
                                  client=client, transcription=pc, refresh=True)
            self.assertEqual(value["source"], expected)
        self.assertTrue(value["automatic"])
        self.assertEqual(value["label"], "Автоматически, может быть с ошибками")
        self.assertEqual(value["lines"], pc["lines"])
        # Hidden on-device fallback yields to the PC result.
        owner = {"source": "on_device_transcription", "synced": True, "lines": [{"time": 1, "text": "phone"}], "text": "phone"}
        client = SimpleNamespace(lookup=AsyncMock(return_value={"status": "not_found"}))
        value = await resolve(track, owner=owner, embedded=None, enrichment=None, cache=LyricsCache(None), client=client,
                              transcription=pc, refresh=True)
        self.assertEqual(value["source"], "pc_transcription")
        value = await resolve(track, owner=owner, embedded=None, enrichment=None, cache=LyricsCache(None), client=client)
        self.assertEqual(value["source"], "on_device_transcription")


class TranscriptionApiTests(test_music_api.MusicApiTests):
    KEY = "ag_fixture-transcriber"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        synced_lyrics._memory.clear()  # process-wide cache; identical fixture audio across tests
        async with self.sessions() as session:
            for name, key in (("Мой ПК", self.KEY), ("Other", "ag_fixture-other")):
                session.add(AgentCredential(source_name=name, api_key_hash=hashlib.sha256(key.encode()).hexdigest(),
                                            key_hint="test", is_active=True))
                session.add(HeartbeatSource(source_name=name, source_type="PC_AGENT", is_online=True,
                                            last_seen_at=datetime.now(timezone.utc), last_payload={}))
            await session.commit()
        self.agent = {"X-Api-Key": self.KEY}

    def poll_body(self, **overrides):
        return {"enabled": True, "state": "ready", "capabilities": GPU, "load": IDLE, **overrides}

    async def no_catalog(self):
        return patch("app.services.synced_lyrics.LrclibClient.lookup", AsyncMock(return_value={"status": "not_found"}))

    async def test_owner_auth_and_agent_auth_required(self):
        for method, path in (("GET", "/api/mini/music/tracks/1/transcription"), ("POST", "/api/mini/music/tracks/1/transcription")):
            for headers, code in (({}, 401), ({"x-test-owner": "guest"}, 403)):
                self.assertEqual((await self.request(method, path, headers=headers, json={})).status_code, code)
        for path in ("/agent/transcription/poll", "/agent/transcription/jobs/1/progress",
                     "/agent/transcription/jobs/1/complete", "/agent/transcription/jobs/1/fail"):
            response = await self.request("POST", path, headers={"X-Api-Key": "ag_wrong"}, json=self.poll_body())
            self.assertEqual(response.status_code, 401, path)

    async def test_full_flow_waiting_then_worker_downloads_transcribes_and_timed_lyrics_show_it(self):
        track = await self.upload()
        route = f"/api/mini/music/tracks/{track['id']}/transcription"
        self.assertEqual((await self.request("GET", route)).json()["status"], "none")
        with await self.no_catalog():
            created = await self.request("POST", route, json={})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["status"], "waiting_for_pc")
        self.assertEqual(created.json()["message"], "Расшифруем, когда включится компьютер")
        with await self.no_catalog():
            pending = await self.request("GET", f"/api/mini/music/tracks/{track['id']}/timed-lyrics")
        self.assertTrue(pending.json()["lyrics"]["transcription_pending"])
        # A disabled PC does not get work.
        off = await self.request("POST", "/agent/transcription/poll", headers=self.agent, json=self.poll_body(enabled=False))
        self.assertIsNone(off.json()["job"])
        polled = await self.request("POST", "/agent/transcription/poll", headers=self.agent, json=self.poll_body())
        self.assertEqual(polled.status_code, 200, polled.text)
        job = polled.json()["job"]
        self.assertEqual((job["track_id"], job["language"]), (track["id"], "ru"))
        # The short-lived agent ticket streams the audio only to this credential.
        audio = await self.request("GET", job["media_path"], headers={})
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.content, self.audio)
        self.assertEqual((await self.request("GET", route)).json()["status"], "running")
        # Another PC cannot touch this job.
        other = {"X-Api-Key": "ag_fixture-other"}
        await self.request("POST", "/agent/transcription/poll", headers=other, json=self.poll_body(capabilities=SMALL_CPU))
        denied = await self.request("POST", f"/agent/transcription/jobs/{job['id']}/complete", headers=other,
                                    json={"lines": [{"start": 0, "end": 1, "text": "x"}]})
        self.assertEqual(denied.status_code, 409)
        progress = await self.request("POST", f"/agent/transcription/jobs/{job['id']}/progress", headers=self.agent,
                                      json={"stage": "transcribe", "fraction": .5})
        self.assertEqual(progress.status_code, 200, progress.text)
        done = await self.request("POST", f"/agent/transcription/jobs/{job['id']}/complete", headers=self.agent,
            json={"lines": [{"start": 0.5, "end": 1.5, "text": "первая строка"}], "model": "large-v3", "device": "cuda"})
        self.assertEqual(done.json()["state"], "done")
        self.assertEqual((await self.request("GET", route)).json()["status"], "done")
        with await self.no_catalog():
            lyrics = await self.request("GET", f"/api/mini/music/tracks/{track['id']}/timed-lyrics")
        value = lyrics.json()["lyrics"]
        self.assertEqual((value["source"], value["synced"], value["automatic"]), ("pc_transcription", True, True))
        self.assertEqual(value["lines"], [{"start": 0.5, "end": 1.5, "text": "первая строка"}])
        self.assertNotIn("transcription_pending", value)
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value={"status": "not_found"})):
            plain = await self.request("GET", f"/api/mini/music/tracks/{track['id']}/lyrics")
        self.assertEqual(plain.json()["lyrics"]["source"], "pc_transcription")
        self.assertEqual(plain.json()["lyrics"]["lines"], [{"time": 0.5, "text": "первая строка"}])
        # Joining a done job never re-runs it.
        again = await self.request("POST", route, json={"force": True})
        self.assertEqual(again.json()["status"], "done")
        self.assertIsNone((await self.request("POST", "/agent/transcription/poll", headers=self.agent, json=self.poll_body())).json()["job"])

    async def test_installing_pc_reports_setup_progress_to_owner_status(self):
        track = await self.upload()
        route = f"/api/mini/music/tracks/{track['id']}/transcription"
        with await self.no_catalog():
            await self.request("POST", route, json={})
        polled = await self.request("POST", "/agent/transcription/poll", headers=self.agent,
                                    json=self.poll_body(state="installing", setup={"stage": "torch", "percent": 37}))
        self.assertEqual(polled.status_code, 200, polled.text)
        self.assertIsNone(polled.json()["job"])
        status = (await self.request("GET", route)).json()
        self.assertEqual((status["status"], status["setup_percent"]), ("preparing_pc", 37))
        self.assertEqual(status["message"], "ПК готовится к расшифровке, 37%")
        for bad in ({"stage": "torch", "percent": 101}, {"stage": "rm -rf", "percent": 1}):
            response = await self.request("POST", "/agent/transcription/poll", headers=self.agent,
                                          json=self.poll_body(state="installing", setup=bad))
            self.assertEqual(response.status_code, 422, bad)
        job = (await self.request("POST", "/agent/transcription/poll", headers=self.agent, json=self.poll_body())).json()["job"]
        self.assertEqual(job["track_id"], track["id"])
        self.assertEqual((await self.request("GET", route)).json()["status"], "running")

    async def test_library_page_queues_songs_without_text_and_retries_a_silent_one(self):
        bare = await self.upload()
        spoken = await self.upload(test_music_api.silent_wav(6))
        missed = await self.upload(test_music_api.silent_wav(7))
        async with self.sessions() as session:
            session.add(MusicEnrichment(track_id=spoken["id"], fingerprint="x", original={},
                                        result={"lyrics": {"text": "уже есть", "lines": [], "synced": False}},
                                        owner_lyrics={}, checked_at=datetime.now(timezone.utc)))
            session.add(TranscriptionJob(track_id=missed["id"], sha256="a" * 64, duration=7, language="ru",
                                         state="failed", error="no_speech_detected", failed_workers=[], progress={},
                                         result={}, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
            await session.commit()
        page = await self.request("GET", "/api/mini/music/library")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["total"], 3)
        async with self.sessions() as session:
            jobs = {job.track_id: job for job in await session.scalars(select(TranscriptionJob))}
        self.assertEqual(jobs[bare["id"]].state, "queued")
        self.assertNotIn(spoken["id"], jobs)
        self.assertEqual(jobs[missed["id"]].state, "queued")
        self.assertTrue(jobs[missed["id"]].result.get("retried_empty"))
        await self.request("GET", "/api/mini/music/library")
        async with self.sessions() as session:
            self.assertEqual(len(list(await session.scalars(select(TranscriptionJob)))), 2)

    async def test_empty_catalog_lyrics_queue_the_pc_once(self):
        """Found recording, no words: the lyrics screen and Now Playing start the PC."""
        track = await self.upload()
        page = f"/api/mini/music/tracks/{track['id']}/lyrics"
        timed = f"/api/mini/music/tracks/{track['id']}/timed-lyrics"
        found_without_words = {"status": "candidate", "reason": "duration_mismatch",
                               "lyrics": {"text": "", "lines": [], "synced": False, "source": "none", "status": "instrumental"}}
        nothing = {"status": "instrumental", "synced": False, "lines": [], "text": "", "source": "lrclib"}
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=found_without_words)), \
             patch("app.services.synced_lyrics.LrclibClient.lookup", AsyncMock(return_value=nothing)):
            opened = await self.request("GET", page)
            playing = await self.request("GET", timed)
            again = await self.request("GET", timed)
        self.assertEqual(opened.json()["lyrics"]["text"], "")
        self.assertTrue(playing.json()["lyrics"]["transcription_pending"])
        self.assertTrue(again.json()["lyrics"]["transcription_pending"])
        async with self.sessions() as session:
            jobs = list(await session.scalars(select(TranscriptionJob)))
        self.assertEqual([(job.track_id, job.state, job.language) for job in jobs], [(track["id"], "queued", "ru")])

    async def test_real_catalog_text_stays_and_an_empty_outage_queues_the_pc(self):
        synced = {"status": "synced", "synced": True, "lines": [{"start": 0, "end": 1, "text": "есть текст"}],
                  "text": "есть текст", "source": "lrclib"}
        with_words = {"status": "candidate", "reason": "duration_mismatch",
                      "lyrics": {"text": "есть текст", "lines": [{"time": 1, "text": "есть текст"}],
                                 "synced": True, "source": "lrclib"}}
        kept = await self.upload()
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=with_words)), \
             patch("app.services.synced_lyrics.LrclibClient.lookup", AsyncMock(return_value=synced)):
            page = await self.request("GET", f"/api/mini/music/tracks/{kept['id']}/lyrics")
            playing = await self.request("GET", f"/api/mini/music/tracks/{kept['id']}/timed-lyrics")
        self.assertEqual(page.json()["lyrics"]["text"], "есть текст")
        self.assertEqual(playing.json()["lyrics"].get("text", ""), "есть текст")
        self.assertNotIn("transcription_pending", playing.json()["lyrics"])
        down = {"status": "unavailable", "synced": False, "lines": [], "text": ""}
        outage = {"status": "unavailable", "lyrics": {"text": "", "lines": [], "synced": False}}
        bare = await self.upload(test_music_api.silent_wav(6))
        with patch("app.services.music_enrichment.enrich_track", AsyncMock(return_value=outage)), \
             patch("app.services.synced_lyrics.LrclibClient.lookup", AsyncMock(return_value=down)):
            page = await self.request("GET", f"/api/mini/music/tracks/{bare['id']}/lyrics")
            playing = await self.request("GET", f"/api/mini/music/tracks/{bare['id']}/timed-lyrics")
        self.assertEqual(page.json()["lyrics"]["text"], "")
        self.assertTrue(playing.json()["lyrics"]["transcription_pending"])
        async with self.sessions() as session:
            jobs = list(await session.scalars(select(TranscriptionJob)))
        self.assertEqual([job.track_id for job in jobs], [bare["id"]])

    async def test_pc_poll_queues_a_song_nobody_opened(self):
        track = await self.upload()
        polled = await self.request("POST", "/agent/transcription/poll", headers=self.agent, json=self.poll_body())
        self.assertEqual(polled.status_code, 200, polled.text)
        self.assertEqual(polled.json()["job"]["track_id"], track["id"])

    async def test_library_queues_songs_beyond_the_loaded_page(self):
        first = await self.upload(test_music_api.silent_wav(5))
        second = await self.upload(test_music_api.silent_wav(6))
        self.assertNotEqual(first["id"], second["id"])
        page = await self.request("GET", "/api/mini/music/library", params={"limit": 1})
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(len(page.json()["tracks"]), 1)
        async with self.sessions() as session:
            ids = sorted(job.track_id for job in await session.scalars(select(TranscriptionJob)))
        self.assertEqual(ids, sorted([first["id"], second["id"]]))

    async def test_failed_transcription_is_not_requeued_by_opening_the_song(self):
        track = await self.upload()
        with await self.no_catalog():
            created = await self.request("POST", f"/api/mini/music/tracks/{track['id']}/transcription", json={})
        self.assertEqual(created.json()["status"], "waiting_for_pc")
        async with self.sessions() as session:
            job = await session.scalar(select(TranscriptionJob))
            job.state = "failed"
            await session.commit()
        with await self.no_catalog():
            playing = await self.request("GET", f"/api/mini/music/tracks/{track['id']}/timed-lyrics")
        self.assertNotIn("transcription_pending", playing.json()["lyrics"])
        async with self.sessions() as session:
            jobs = list(await session.scalars(select(TranscriptionJob)))
        self.assertEqual([job.state for job in jobs], ["failed"])

    async def test_catalog_lyrics_prevent_job_unless_forced(self):
        track = await self.upload()
        route = f"/api/mini/music/tracks/{track['id']}/transcription"
        synced = {"status": "synced", "synced": True, "lines": [{"start": 0, "end": 1, "text": "cat"}], "text": "cat", "source": "lrclib"}
        with patch("app.services.synced_lyrics.LrclibClient.lookup", AsyncMock(return_value=synced)):
            response = await self.request("POST", route, json={})
        self.assertEqual(response.json()["status"], "catalog_available")
        async with self.sessions() as session:
            self.assertIsNone(await session.scalar(select(TranscriptionJob)))
        forced = await self.request("POST", route, json={"force": True, "language": "en"})
        self.assertEqual((forced.json()["status"], forced.json()["language"]), ("waiting_for_pc", "en"))

    async def test_worker_failure_and_busy_worker_reporting(self):
        track = await self.upload()
        with await self.no_catalog():
            await self.request("POST", f"/api/mini/music/tracks/{track['id']}/transcription", json={})
        busy = await self.request("POST", "/agent/transcription/poll", headers=self.agent,
                                  json=self.poll_body(load={"cpu_percent": 96, "busy": True}))
        self.assertIsNone(busy.json()["job"])
        self.assertEqual((await self.request("GET", f"/api/mini/music/tracks/{track['id']}/transcription")).json()["status"], "queued")
        job = (await self.request("POST", "/agent/transcription/poll", headers=self.agent, json=self.poll_body())).json()["job"]
        failed = await self.request("POST", f"/agent/transcription/jobs/{job['id']}/fail", headers=self.agent, json={"reason": "cuda_oom"})
        self.assertEqual(failed.json()["state"], "assigned")  # re-offered (only worker online, attempt 1 of 3)
        stale = await self.request("POST", f"/agent/transcription/jobs/{job['id']}/progress", headers=self.agent, json={})
        self.assertEqual(stale.status_code, 409)
        bad = await self.request("POST", "/agent/transcription/poll", headers=self.agent,
                                 json=self.poll_body(capabilities={"vram_mb": -1}))
        self.assertEqual(bad.status_code, 422)


# Run only the transcription tests here, not the inherited music API suite again.
for _name in dir(test_music_api.MusicApiTests):
    if _name.startswith("test_"):
        setattr(TranscriptionApiTests, _name, None)


if __name__ == "__main__":
    unittest.main()
