from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import io
import json
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import Base
from app.music_models import MusicPlaylist, MusicTrack
from app.services import music_ingest as ingest
from app.services.music_library import content_lock, display_title, inspect_audio
from test_music_library import silent_wav


def message(file_id="first", *, message_id=1, name="first_song.wav", owner=42, **media):
    return {"message_id": message_id, "from": {"id": owner}, "chat": {"id": owner, "type": "private"},
            "audio": {"file_id": file_id, "file_unique_id": "unique-" + file_id, "file_name": name, **media}}


def archive_bytes(entries, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return output.getvalue()


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, data, owner):
        self.data, self.owner = data, owner

    async def __aiter__(self):
        for offset in range(0, len(self.data), 4096):
            chunk = self.data[offset:offset + 4096]
            self.owner.bytes_read += len(chunk)
            await asyncio.sleep(0)
            yield chunk


class TelegramMusicIngestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(_env_file=None, owner_user_id=42, music_root=str(self.root / "music"),
                                 music_min_free_bytes=0)
        self.engine = create_async_engine("sqlite+aiosqlite:///" + (self.root / "fixture.db").as_posix())
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.files = {}
        self.bytes_read = 0
        self.downloads = 0
        self.response_headers = {}
        self.response_status = 200
        self.hide_size = False

        async def metadata(file_id):
            return {"file_path": "music/" + file_id, "file_size": 0 if self.hide_size else len(self.files[file_id])}

        async def transport(request):
            self.downloads += 1
            data = self.files[request.url.path.rsplit("/", 1)[-1]]
            return httpx.Response(self.response_status, headers=self.response_headers,
                                  stream=ChunkStream(data, self))

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        self.bot = SimpleNamespace(client=self.client, file_url="https://api.telegram.org/file/botFIXTURE_ONLY",
                                   get_file=AsyncMock(side_effect=metadata))
        self.send = AsyncMock()
        self.service = ingest.TelegramMusicIngest(self.settings, self.bot, self.sessions, self.send,
                                                 debounce_seconds=0.01)

    async def asyncTearDown(self):
        await self.service.close()
        await self.client.aclose()
        await self.engine.dispose()
        self.temp.cleanup()

    def put(self, file_id, body=None, **kwargs):
        self.files[file_id] = silent_wav() if body is None else body
        return message(file_id, **kwargs)

    async def import_message(self, value):
        item = ingest.music_attachment(value, self.settings.owner_user_id)
        self.assertIsNotNone(item)
        return await self.service.ingest(item)

    async def rows(self):
        async with self.sessions() as session:
            return list(await session.scalars(select(MusicTrack).order_by(MusicTrack.id)))

    async def test_audio_document_tags_fallback_names_and_sha_dedup(self):
        first = self.put("first", name="../folder/first_song.wav", title="Моя песня", performer="Автор")
        added = await self.import_message(first)
        duplicate = self.put("second", message_id=2, name="other_name.wav")
        duplicate["document"] = duplicate.pop("audio")
        result = await self.import_message(duplicate)
        self.assertEqual(result.existing, added.added)
        tracks = await self.rows()
        self.assertEqual(len(tracks), 1)
        self.assertEqual((tracks[0].filename, tracks[0].title, tracks[0].artist),
                         ("first_song.wav", "Моя песня", "Автор"))
        self.assertTrue((Path(self.settings.music_root) / tracks[0].storage_name).is_file())

    async def test_receipts_survive_restart_and_file_unique_id_avoids_redownload(self):
        first = self.put("first")
        result = await self.import_message(first)
        self.service = ingest.TelegramMusicIngest(self.settings, self.bot, self.sessions, self.send)
        repeat = message("first", message_id=9)
        duplicate = await self.import_message(repeat)
        self.assertEqual(duplicate.existing, result.added)
        self.assertEqual(self.downloads, 1)
        self.assertEqual(len(await self.rows()), 1)
        receipts = list((Path(self.settings.music_root) / ".telegram-ingest").glob("*.json"))
        self.assertEqual(len(receipts), 3)
        for path in receipts:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("FIXTURE_ONLY", text)
            self.assertNotIn("unique-first", text)

    async def test_zip_order_mixed_duplicates_metadata_and_non_audio_skip(self):
        existing = await self.import_message(self.put("existing"))
        body = archive_bytes([("album/02_other_song.wav", silent_wav(6)), ("album/01_first.wav", silent_wav()),
                              ("cover.jpg", b"cover"), ("album/03_end.wav", silent_wav(7))])
        value = self.put("zip", body, message_id=2, name="album.zip")
        value["document"] = value.pop("audio")
        result = await self.import_message(value)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(len(result.added), 2)
        self.assertEqual(result.ordered, [result.added[0], existing.added[0], result.added[1]])
        tracks = await self.rows()
        self.assertEqual(tracks[1].title, "02 other song")
        self.assertEqual(tracks[1].filename, "02_other_song.wav")
        repeat = await self.import_message(value)
        self.assertEqual(repeat.ordered, result.ordered)
        self.assertEqual(self.downloads, 2)

    async def test_batch_album_one_summary_preserves_message_order(self):
        third = self.put("third", silent_wav(7), message_id=30, name="third.wav")
        first = self.put("first", silent_wav(5), message_id=10, name="first.wav")
        second = self.put("second", silent_wav(6), message_id=20, name="second.wav")
        for item in (third, first, second, first):
            item["media_group_id"] = "album-001"
            self.assertTrue(self.service.enqueue(item))
        await asyncio.wait_for(self.service.task, 5)
        self.send.assert_awaited_once()
        self.assertIn("Добавлено: 3", self.send.await_args.args[1])
        self.assertEqual([row.filename for row in await self.rows()], ["first.wav", "second.wav", "third.wav"])
        self.assertEqual(self.downloads, 3)

    async def test_burst_arriving_during_download_gets_one_summary(self):
        first, second = self.put("first"), self.put("second", silent_wav(6), message_id=2)
        original = self.service.ingest

        async def joining(item):
            if item.message_id == 1:
                self.service.enqueue(second)
            return await original(item)

        with patch.object(self.service, "ingest", side_effect=joining):
            self.service.enqueue(first)
            await asyncio.wait_for(self.service.task, 5)
        self.send.assert_awaited_once()
        self.assertIn("Добавлено: 2", self.send.await_args.args[1])

    async def test_owner_boundary_ignores_group_business_voice_and_unrelated_docs(self):
        values = [message(owner=7), message(), message(), message(), message(), message()]
        values[1]["chat"]["type"] = "supergroup"
        values[2]["chat"]["id"] = 8
        values[3]["business_connection_id"] = "private-business"
        values[4]["voice"] = values[4].pop("audio")
        values[5]["document"] = {"file_id": "other", "file_name": "notes.txt"}
        values[5].pop("audio")
        for value in values:
            self.assertFalse(self.service.enqueue(value))
        self.assertIsNone(self.service.task)
        self.bot.get_file.assert_not_awaited()

    async def test_malformed_audio_is_not_imported_and_error_contains_no_transport_secret(self):
        result = await self.import_message(self.put("broken", b"not an mp3", name="bad.mp3"))
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(await self.rows(), [])
        value = self.put("network", message_id=2)
        self.bot.get_file.side_effect = httpx.ConnectError("https://api.telegram.org/file/botSECRET")
        self.service.enqueue(value)
        await asyncio.wait_for(self.service.task, 5)
        self.assertNotIn("SECRET", self.send.await_args.args[1])
        self.assertNotIn("api.telegram.org", self.send.await_args.args[1])

    async def test_declared_oversize_is_rejected_before_request(self):
        value = self.put("big", file_size=ingest.TELEGRAM_MAX_BYTES + 1)
        with self.assertRaisesRegex(ingest.IngestError, "Mini App"):
            await self.import_message(value)
        self.bot.get_file.assert_not_awaited()
        self.assertFalse(list((Path(self.settings.music_root) / ".telegram-ingest").glob("incoming-*")))

    async def test_actual_stream_is_bounded_without_size_metadata(self):
        self.hide_size = True
        value = self.put("huge", b"0" * (4 * ingest.IO_CHUNK))
        with patch.object(ingest, "TELEGRAM_MAX_BYTES", 1024):
            with self.assertRaisesRegex(ingest.IngestError, "размер"):
                await self.import_message(value)
        self.assertLessEqual(self.bytes_read, ingest.IO_CHUNK)
        self.assertEqual(await self.rows(), [])
        self.assertFalse(list((Path(self.settings.music_root) / ".telegram-ingest").glob("incoming-*")))

    async def test_http_redirect_encoding_and_unsafe_telegram_path_rejected(self):
        value = self.put("first")
        self.response_status = 302
        self.response_headers = {"location": "https://evil.invalid/steal"}
        with self.assertRaises(ingest.IngestError):
            await self.import_message(value)
        self.assertEqual(self.downloads, 1)
        self.response_status = 200
        self.response_headers = {"content-encoding": "gzip"}
        with self.assertRaises(ingest.IngestError):
            await self.import_message(value)
        self.assertEqual(self.bytes_read, 0)
        for path in ("../token", "https://evil.invalid/a", "/absolute", "music/%2fsecret", "music//a"):
            self.bot.get_file = AsyncMock(return_value={"file_path": path})
            with self.assertRaises(ingest.IngestError):
                await self.import_message(value)
        self.assertEqual(self.downloads, 2)

    async def test_disk_reserve_and_bounded_queue(self):
        value = self.put("first")
        with patch.object(ingest.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ingest.IngestError, "места"):
                await self.import_message(value)
        self.assertEqual(self.downloads, 0)
        with patch.object(ingest, "MAX_PENDING_MESSAGES", 2):
            self.service.enqueue(value)
            self.service.enqueue(self.put("second", silent_wav(6), message_id=2))
            self.service.enqueue(self.put("third", silent_wav(7), message_id=3))
            await asyncio.wait_for(self.service.task, 5)
        self.assertIn("Очередь заполнена", self.send.await_args.args[1])
        self.assertEqual(len(await self.rows()), 2)

    async def test_common_lock_prevents_concurrent_service_sha_duplicates(self):
        first = ingest.music_attachment(self.put("first"), 42)
        second = ingest.music_attachment(self.put("second", message_id=2), 42)
        other = ingest.TelegramMusicIngest(self.settings, self.bot, self.sessions, self.send)
        results = await asyncio.gather(self.service.ingest(first), other.ingest(second))
        self.assertEqual(sum(len(value.added) for value in results), 1)
        self.assertEqual(sum(len(value.existing) for value in results), 1)
        self.assertEqual(len(await self.rows()), 1)
        root = Path(self.settings.music_root)
        lock = content_lock(root, "digest")
        self.assertIs(content_lock(root, "digest"), lock)

    async def test_zip_rejected_before_any_library_track_and_staging_cleaned(self):
        body = archive_bytes([("good.wav", silent_wav()), ("../escaped.wav", silent_wav())])
        with self.assertRaisesRegex(ingest.IngestError, "путь"):
            await self.import_message(self.put("zip", body, name="music.zip"))
        self.assertEqual(await self.rows(), [])
        self.assertFalse(list(self.root.rglob("escaped.wav")))
        self.assertFalse(list((Path(self.settings.music_root) / ".telegram-ingest").glob("incoming-*")))

    async def test_filename_humanization_preserves_real_title_tags(self):
        path = self.root / "my_favorite_song.wav"
        path.write_bytes(silent_wav())
        self.assertEqual(display_title(path.name), "my favorite song")
        self.assertEqual(inspect_audio(path, path.name)["title"], "my favorite song")
        fake = SimpleNamespace(info=SimpleNamespace(length=3), tags={"title": ["Tagged_Name"], "artist": ["Artist"]})
        FakeAudio = type("FakeAudio", (), {"__module__": "mutagen.wave"})
        tagged = FakeAudio()
        tagged.info, tagged.tags = fake.info, fake.tags
        with patch("app.services.music_library.mutagen.File", return_value=tagged):
            self.assertEqual(inspect_audio(path, path.name)["title"], "Tagged_Name")

    async def test_deleted_track_message_retry_is_not_restored_but_new_message_can_reupload(self):
        value = self.put("first")
        original = await self.import_message(value)
        async with self.sessions() as session:
            track = await session.get(MusicTrack, original.added[0])
            track.deleted = True
            await session.commit()
        retry = await self.import_message(value)
        self.assertEqual(retry.removed, 1)
        self.assertEqual(retry.existing, [])
        self.assertEqual(self.downloads, 1)
        value["message_id"] = 2
        added = await self.import_message(value)
        self.assertEqual(len(added.added), 1)
        self.assertNotEqual(added.added, original.added)

    async def test_handler_retains_explicit_avatar_project_dialogs_and_commands(self):
        from app.telegram_handler import TelegramUpdateHandler

        handler = TelegramUpdateHandler(self.settings, self.bot)
        hooks = ["_cache_recent_message", "_notify_edit_events", "_notify_deleted_events",
                 "_maybe_handle_away_mode", "_maybe_handle_chat_mute", "_maybe_handle_away_bypass_contact",
                 "_maybe_handle_profile_avatar_upload", "_maybe_handle_profile_dialog_input",
                 "_maybe_handle_projects_upload", "_maybe_handle_projects_dialog_input"]
        for hook in hooks:
            setattr(handler, hook, AsyncMock(return_value=False))
        handler._safe_send = AsyncMock()
        handler._handle_dot_command = AsyncMock(return_value=True)
        value = self.put("first")
        with patch("app.telegram_handler.get_or_create_app_config", AsyncMock(return_value=SimpleNamespace())), \
                patch("app.telegram_handler.handle_update_logging", AsyncMock()):
            for chosen in ("_maybe_handle_profile_avatar_upload", "_maybe_handle_profile_dialog_input",
                           "_maybe_handle_projects_upload", "_maybe_handle_projects_dialog_input"):
                getattr(handler, chosen).return_value = True
                await handler.handle_update(SimpleNamespace(bind=self.engine), {"message": value})
                self.assertIsNone(handler.music_ingest, chosen)
                getattr(handler, chosen).return_value = False
            await handler.handle_update(SimpleNamespace(bind=self.engine), {"business_message": value})
            self.assertIsNone(handler.music_ingest)
            command = {"message_id": 10, "from": {"id": 42}, "chat": {"id": 42, "type": "private"},
                       "text": ".muz my song"}
            await handler.handle_update(SimpleNamespace(bind=self.engine), {"message": command})
            handler._handle_dot_command.assert_awaited_once()
            await handler.handle_update(SimpleNamespace(bind=self.engine), {"message": value})
            self.assertIsNotNone(handler.music_ingest)
            await handler.close()
            self.assertTrue(handler.music_ingest.closed)
            self.assertTrue(handler.music_ingest.task.done())
        self.bot.get_file.assert_not_awaited()

    async def test_close_cancels_download_without_import_or_partial_files(self):
        value = self.put("first")
        started = asyncio.Event()

        async def stalled(file_id):
            started.set()
            await asyncio.Event().wait()

        self.bot.get_file.side_effect = stalled
        self.service.enqueue(value)
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(self.service.close(), 2)
        self.assertEqual(await self.rows(), [])
        self.assertFalse(list((Path(self.settings.music_root) / ".telegram-ingest").glob("incoming-*")))
        self.assertFalse(self.service.enqueue(value))

    async def test_durable_queue_replays_after_restart_and_stores_no_whole_message(self):
        value = self.put("first")
        value["text"] = "PRIVATE CHAT TEXT MUST NOT BE IN JOURNAL"
        value["caption"] = "PRIVATE CAPTION"
        self.service.enqueue(value)
        await self.service.close()
        journal = Path(self.settings.music_root) / ".telegram-ingest" / "pending"
        jobs = list(journal.glob("*.json"))
        self.assertEqual(len(jobs), 1)
        text = jobs[0].read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("FIXTURE_ONLY", text)
        self.assertEqual(set(json.loads(text)), set(ingest.Attachment.__dataclass_fields__))
        self.service = ingest.TelegramMusicIngest(self.settings, self.bot, self.sessions, self.send,
                                                 debounce_seconds=0.01)
        await self.service.start()
        await asyncio.wait_for(self.service.task, 5)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(list(journal.glob("*.json")), [])
        self.send.assert_awaited_once()

    async def test_crash_after_receipt_before_job_removal_does_not_duplicate_output(self):
        value = self.put("first")
        self.service.enqueue(value)
        await self.service.close()
        await self.import_message(value)  # Emulate committed track + receipt before the crash.
        self.service = ingest.TelegramMusicIngest(self.settings, self.bot, self.sessions, self.send)
        await self.service.start()
        self.assertIsNone(self.service.task)
        self.assertEqual(self.downloads, 1)
        self.send.assert_not_awaited()
        self.assertTrue(self.service.enqueue(value))  # Duplicate Telegram delivery is also silent.
        self.assertIsNone(self.service.task)

    async def test_corrupt_or_foreign_owner_queue_is_not_replayed(self):
        value = self.put("first")
        self.service.enqueue(value)
        await self.service.close()
        journal = Path(self.settings.music_root) / ".telegram-ingest" / "pending"
        path = next(journal.glob("*.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["chat_id"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.service = ingest.TelegramMusicIngest(self.settings, self.bot, self.sessions, self.send)
        await self.service.start()
        self.assertIsNone(self.service.task)
        self.bot.get_file.assert_not_awaited()
        self.send.assert_awaited_once()
        self.assertTrue(path.is_file())  # Never delete an unrecognized user's data.

    async def test_new_upload_restores_evicted_server_copy_under_same_track_id(self):
        value = self.put("first")
        original = await self.import_message(value)
        async with self.sessions() as session:
            track = await session.get(MusicTrack, original.added[0])
            track.favorite = True
            playlist = MusicPlaylist(name="Keep order", track_ids=[track.id])
            session.add(playlist)
            await session.commit()
            old_storage, playlist_id = track.storage_name, playlist.id
        path = Path(self.settings.music_root) / old_storage
        path.unlink()  # Fixture-only eviction, never a real user track.
        value["message_id"] = 2
        restored = await self.import_message(value)
        self.assertEqual(restored.added, [])
        self.assertEqual(restored.existing, original.added)
        self.assertEqual(restored.restored, 1)
        self.assertEqual(path.read_bytes(), self.files["first"])
        self.assertEqual(self.downloads, 2)
        async with self.sessions() as session:
            track = await session.get(MusicTrack, original.added[0])
            playlist = await session.get(MusicPlaylist, playlist_id)
            self.assertTrue(track.favorite)
            self.assertEqual(track.storage_name, old_storage)
            self.assertEqual(playlist.track_ids, original.added)

    async def test_standalone_zip_keeps_caller_source_and_order(self):
        root = ingest.private_root(self.settings)
        source = root / "owner-upload.part"
        body = archive_bytes([("folder/second_song.wav", silent_wav(6)), ("folder/first_song.wav", silent_wav())])
        source.write_bytes(body)
        result = await ingest.ingest_path(self.settings, self.sessions, source, "my-album.zip")
        self.assertEqual(source.read_bytes(), body)
        tracks = await self.rows()
        self.assertEqual(result.ordered, [track.id for track in tracks])
        self.assertEqual([track.title for track in tracks], ["second song", "first song"])
        retry = await ingest.ingest_path(self.settings, self.sessions, source, "my-album.zip")
        self.assertEqual(retry.existing, result.ordered)
        self.assertEqual(retry.added, [])
        outside = self.root / "outside.wav"
        outside.write_bytes(silent_wav())
        with self.assertRaisesRegex(ingest.IngestError, "приватном"):
            await ingest.ingest_path(self.settings, self.sessions, outside, "outside.wav")
        self.assertTrue(outside.exists())

    async def test_standalone_commit_failure_keeps_source_and_removes_no_other_data(self):
        root = ingest.private_root(self.settings)
        source = root / "owner-upload.part"
        source.write_bytes(silent_wav())

        @asynccontextmanager
        async def failing_session():
            async with self.sessions() as session:
                with patch.object(session, "commit", AsyncMock(side_effect=RuntimeError("fixture commit failure"))):
                    yield session

        with self.assertRaisesRegex(RuntimeError, "fixture"):
            await ingest.ingest_path(self.settings, failing_session, source, "fixture.wav")
        self.assertEqual(source.read_bytes(), silent_wav())
        self.assertEqual(list(root.glob("*.wav")), [])
        self.assertEqual(await self.rows(), [])

    async def test_database_sha_lock_dedupes_independent_engines_without_python_lock(self):
        root = ingest.private_root(self.settings)
        source = root / "owner-upload.part"
        source.write_bytes(silent_wav())
        other_engine = create_async_engine(self.engine.url)
        other_sessions = async_sessionmaker(other_engine, expire_on_commit=False)

        @asynccontextmanager
        async def no_python_lock(*args):
            yield

        try:
            with patch.object(ingest, "content_lock", no_python_lock):
                results = await asyncio.gather(
                    ingest.ingest_path(self.settings, self.sessions, source, "first.wav"),
                    ingest.ingest_path(self.settings, other_sessions, source, "second.wav"))
            self.assertEqual(sum(len(result.added) for result in results), 1)
            self.assertEqual(sum(len(result.existing) for result in results), 1)
            self.assertEqual(len(await self.rows()), 1)
        finally:
            await other_engine.dispose()


class ZipValidationTests(unittest.TestCase):
    def plan(self, infos):
        return ingest._zip_plan(SimpleNamespace(infolist=lambda: infos), 128 * 1024 * 1024)

    @staticmethod
    def info(name="song.wav", *, size=1000, compressed=1000):
        value = zipfile.ZipInfo(name)
        value.file_size, value.compress_size = size, compressed
        return value

    def test_unsafe_paths_duplicate_normalized_names_and_special_entries(self):
        for name in ("../song.wav", "/song.wav", "C:/song.wav", "C:song.wav", "dir\\song.wav", "a//song.wav", "a/./song.wav"):
            with self.subTest(name=name), self.assertRaises(ingest.IngestError):
                self.plan([self.info(name)])
        nul = self.info()
        nul.orig_filename = "song.wav\x00.exe"
        with self.assertRaises(ingest.IngestError):
            self.plan([nul])
        with self.assertRaises(ingest.IngestError):
            self.plan([self.info("Song.wav"), self.info("song.wav")])
        for kind in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR):
            value = self.info()
            value.external_attr = (kind | 0o777) << 16
            with self.assertRaises(ingest.IngestError):
                self.plan([value])

    def test_encrypted_nested_unsupported_compression_size_count_and_bomb(self):
        encrypted = self.info()
        encrypted.flag_bits |= 1
        unsupported = self.info()
        unsupported.compress_type = zipfile.ZIP_LZMA
        for value in (encrypted, unsupported, self.info("nested.zip"), self.info(size=100_001, compressed=1000),
                      self.info(size=129 * 1024 * 1024, compressed=129 * 1024 * 1024)):
            with self.assertRaises(ingest.IngestError):
                self.plan([value])
        with self.assertRaises(ingest.IngestError):
            self.plan([self.info(f"{index}.wav") for index in range(201)])
        with self.assertRaises(ingest.IngestError):
            self.plan([self.info(f"{index}.txt") for index in range(501)])
        with patch.object(ingest, "ZIP_MAX_EXPANDED", 1999), self.assertRaises(ingest.IngestError):
            self.plan([self.info("1.wav"), self.info("2.wav")])

    def test_real_deflate_bomb_and_unsupported_files_do_not_extract(self):
        with tempfile.TemporaryDirectory() as working:
            root = Path(working)
            source = root / "bomb.zip"
            source.write_bytes(archive_bytes([("song.wav", b"0" * 200_000)], zipfile.ZIP_DEFLATED))
            settings = SimpleNamespace(music_max_upload_bytes=128 * 1024 * 1024, music_min_free_bytes=0)
            with self.assertRaises(ingest.IngestError):
                ingest.unpack_music_zip(source, root, settings)
            self.assertEqual([path.name for path in root.iterdir()], ["bomb.zip"])

    def test_central_directory_limits_checked_before_zipfile_allocation(self):
        with tempfile.TemporaryDirectory() as working:
            root = Path(working)
            source = root / "many.zip"
            source.write_bytes(archive_bytes([(str(index) + ".wav", b"x") for index in range(501)]))
            settings = SimpleNamespace(music_max_upload_bytes=128 * 1024 * 1024, music_min_free_bytes=0)
            with patch.object(ingest.zipfile, "ZipFile") as parser, self.assertRaises(ingest.IngestError):
                ingest.unpack_music_zip(source, root, settings)
            parser.assert_not_called()
            # A lying EOCD entry count must not evade the actual-entry bound.
            body = bytearray(source.read_bytes())
            eocd = body.rfind(b"PK\x05\x06")
            struct.pack_into("<HH", body, eocd + 8, 1, 1)
            source.write_bytes(body)
            with patch.object(ingest.zipfile, "ZipFile") as parser, self.assertRaises(ingest.IngestError):
                ingest.unpack_music_zip(source, root, settings)
            parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
