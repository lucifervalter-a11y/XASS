from __future__ import annotations

import asyncio
from contextlib import closing
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import get_session
from app.server_migration_api import build_router
from app.services.server_backup import create_snapshot, decrypt_file, encrypt_file, extract_verified_tar, restore_snapshot
from app.services.server_transfers import TransferStore, decode_code, encode_code
from app.telegram_handler import TelegramUpdateHandler

PASSWORD = 'test archive passphrase 2026'


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'source'
        self.root.mkdir()
        (self.root / 'data').mkdir()
        (self.root / 'index.php').write_text('<?php echo "site";')
        (self.root / '.env').write_text('BOT_TOKEN=source-secret\n')
        (self.root / 'data/profile.json').write_text('{"name":"Test"}')
        self.external = self.base / 'media'
        self.external.mkdir()
        (self.external / 'message.jpg').write_bytes(b'actual media')
        self.db = sqlite3.connect(self.root / 'data/serverredus.db')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE media_assets(id INTEGER PRIMARY KEY, local_path TEXT)')
        self.db.execute('INSERT INTO media_assets VALUES (1, ?)', (str(self.external / 'message.jpg'),))
        self.db.commit()
        self.settings = Settings(_env_file=None, bot_token='effective-secret', owner_user_id=42,
                                 media_root=str(self.external), server_backup_dir=str(self.base / 'backups'))
        self.archive = self.base / 'server.xass-server'

    def test_roundtrip_wal_external_media_environment_and_site(self):
        manifest = create_snapshot(self.root, self.settings, self.archive, password=PASSWORD)
        self.assertEqual(manifest['database'], 'sqlite')
        target = self.base / 'new-server'
        restore_snapshot(self.archive, target, password=PASSWORD)
        self.assertEqual((target / 'index.php').read_bytes(), (self.root / 'index.php').read_bytes())
        self.assertEqual((target / 'restored/media_root/message.jpg').read_bytes(), b'actual media')
        with closing(sqlite3.connect(target / 'data/serverredus.db')) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT local_path FROM media_assets').fetchone()[0], str(target / 'restored/media_root/message.jpg'))
        restored = Settings(_env_file=target / '.env')
        self.assertEqual(restored.bot_token, 'effective-secret')
        self.assertEqual(restored.owner_user_id, 42)
        self.assertFalse(restored.polling_drop_pending_updates)
        self.assertEqual((target / '.env.source').read_text(), 'BOT_TOKEN=source-secret\n')
        self.assertTrue((target / '.migration-pending').exists())
        if os.name != 'nt':  # Windows chmod does not implement POSIX owner/group bits.
            self.assertEqual((target / '.env').stat().st_mode & 0o777, 0o600)
        self.assertFalse((target / 'data/serverredus.db-wal').exists())
        with self.assertRaises(ValueError):
            restore_snapshot(self.archive, target, password=PASSWORD)

    def test_wrong_password_and_tamper_publish_no_plaintext(self):
        create_snapshot(self.root, self.settings, self.archive, password=PASSWORD)
        for password, tamper in [('incorrect password 123', False), (PASSWORD, True)]:
            if tamper:
                data = bytearray(self.archive.read_bytes()); data[-25] ^= 1; self.archive.write_bytes(data)
            output = self.base / 'plaintext.tar.gz'
            with self.assertRaises(ValueError):
                decrypt_file(self.archive, output, password=password)
            self.assertFalse(output.exists())

    def test_rsa_delivery_encryption_and_existing_destination(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        pub = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        key = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        create_snapshot(self.root, self.settings, self.archive, public_key=pub)
        restore_snapshot(self.archive, self.base / 'restored', private_key=key)
        original = self.archive.read_bytes()
        with self.assertRaises(FileExistsError):
            encrypt_file(self.root / 'index.php', self.archive, password=PASSWORD)
        self.assertEqual(self.archive.read_bytes(), original)

    def test_live_database_is_not_copied_and_backups_are_excluded(self):
        backup_dir = Path(self.settings.server_backup_dir); backup_dir.mkdir()
        (backup_dir / 'secret-code').write_text('not part of a snapshot')
        create_snapshot(self.root, self.settings, self.archive, password=PASSWORD)
        plain = self.base / 'plain.tar.gz'; decrypt_file(self.archive, plain, password=PASSWORD)
        with tarfile.open(plain) as tar:
            names = tar.getnames()
        self.assertIn('database/sqlite.db', names)
        self.assertNotIn('payload/data/serverredus.db', names)
        self.assertFalse(any('secret-code' in name for name in names))

    def test_tar_traversal_symlinks_and_duplicate_members_rejected(self):
        for index, name in enumerate(['../escape', '/absolute', 'payload/../../escape', 'payload/C:escape', 'payload/CON', 'payload/link', 'payload/a']):
            archive = self.base / f'bad{index}.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                item = tarfile.TarInfo(name)
                if name.endswith('link'):
                    item.type = tarfile.SYMTYPE; item.linkname = '/etc/passwd'
                tar.addfile(item)
                if name == 'payload/a': tar.addfile(item)
            destination = self.base / f'unpack{index}'; destination.mkdir()
            with self.assertRaises(ValueError): extract_verified_tar(archive, destination)
        self.assertFalse((self.base / 'escape').exists())

    def test_restore_cancels_old_commands_without_changing_original(self):
        self.db.execute('CREATE TABLE agent_commands(id INTEGER PRIMARY KEY, status TEXT, result TEXT, completed_at TEXT)')
        self.db.execute("INSERT INTO agent_commands VALUES(1, 'delivered', '{}', NULL)")
        self.db.execute('CREATE TABLE heartbeat_sources(id INTEGER PRIMARY KEY, is_online BOOLEAN)')
        self.db.execute('INSERT INTO heartbeat_sources VALUES(1, 1)')
        self.db.commit()
        create_snapshot(self.root, self.settings, self.archive, password=PASSWORD)
        target = self.base / 'restored-command-state'
        restore_snapshot(self.archive, target, password=PASSWORD)
        with closing(sqlite3.connect(target / 'data/serverredus.db')) as db:
            status, result, completed = db.execute('SELECT status, result, completed_at FROM agent_commands').fetchone()
            self.assertEqual(status, 'failed')
            self.assertEqual(json.loads(result)['details']['reason'], 'server_migration')
            self.assertTrue(completed)
            self.assertEqual(db.execute('SELECT is_online FROM heartbeat_sources').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT status FROM agent_commands').fetchone()[0], 'delivered')

    def test_interpolation_in_resolved_secrets_is_rejected_before_export(self):
        self.settings.setup_api_key = '${UNRELATED_SECRET}'
        with self.assertRaisesRegex(ValueError, 'literal'):
            create_snapshot(self.root, self.settings, self.archive, password=PASSWORD)
        self.assertFalse(self.archive.exists())

    def test_encrypted_snapshot_omits_checkout_pc_keys_but_keeps_server_state(self):
        workstation_secret = b"WORKSTATION_ONLY_KEY_SENTINEL"
        names = ["config.json", "config.json.bak", ".config.json.1.2.tmp", ".xass-master.key", "data/.xass-master.key", ".command-results.json", "Archive/private.jpg", "logs/debug.txt", ".venv312/Lib/secret.py"]
        for name in names:
            path = self.root / 'pc_client' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(workstation_secret)
        (self.root / 'pc_client/secret_store.py').write_text('# keep source')
        (self.root / 'data/.xass-master.key').write_bytes(b'SERVER_KEY_MUST_SURVIVE')
        (self.root / 'data/config.json.bak').write_bytes(b'SERVER_BACKUP_MUST_SURVIVE')
        workspace = self.root / 'pc_client/Archive/server-workspace'
        workspace.mkdir()
        (workspace / 'upload.txt').write_bytes(b'SERVER_UPLOAD_MUST_SURVIVE')
        self.settings.agent_workspace_dir = str(workspace)
        create_snapshot(self.root, self.settings, self.archive, password=PASSWORD)
        plain = self.base / 'verified.tar.gz'
        decrypt_file(self.archive, plain, password=PASSWORD)
        with tarfile.open(plain) as packaged:
            members = set(packaged.getnames())
            for name in names:
                self.assertNotIn('payload/pc_client/' + name, members)
            self.assertIn('payload/pc_client/secret_store.py', members)
            self.assertEqual(packaged.extractfile('payload/data/.xass-master.key').read(), b'SERVER_KEY_MUST_SURVIVE')
            self.assertEqual(packaged.extractfile('payload/data/config.json.bak').read(), b'SERVER_BACKUP_MUST_SURVIVE')
            self.assertEqual(packaged.extractfile('payload/pc_client/Archive/server-workspace/upload.txt').read(), b'SERVER_UPLOAD_MUST_SURVIVE')
            self.assertIn(b'effective-secret', packaged.extractfile('settings.json').read())
            self.assertIn('database/sqlite.db', members)
            self.assertIn('payload/restored/media_root/message.jpg', members)
            for item in packaged:
                self.assertNotIn(workstation_secret, packaged.extractfile(item).read())


class TransferTests(unittest.TestCase):
    def test_single_consumer_under_concurrency_expiry_and_revoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransferStore(Path(tmp))
            job = store.create(); store.path(job).write_bytes(b'encrypted'); store.finish(job)
            ticket = store.ticket(job)
            def claim(_):
                try: store.consume(ticket); return True
                except ValueError: return False
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(claim, range(8))), 1)
            expired = store.ticket(job, ttl=-1)
            with self.assertRaises(ValueError): store.consume(expired)
            revoked = store.ticket(job); store.revoke(job)
            with self.assertRaises(ValueError): store.consume(revoked)
            self.assertNotIn(ticket, (Path(tmp) / 'transfers.sqlite3').read_bytes().decode(errors='ignore'))

    def test_pending_copy_does_not_consume_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransferStore(Path(tmp)); job = store.create(); ticket = store.ticket(job)
            with self.assertRaises(ValueError): store.consume(ticket)
            store.path(job).write_bytes(b'encrypted'); store.finish(job)
            self.assertEqual(store.consume(ticket), store.path(job))

    def test_codes_require_https_and_no_credentials_or_paths(self):
        code = encode_code('https://xass.example', 'a' * 43, PASSWORD)
        self.assertEqual(decode_code(code)['password'], PASSWORD)
        for origin in ['http://xass.example', 'https://user:password@xass.example', 'https://xass.example/path', 'https://xass.example?token=1']:
            with self.assertRaises(ValueError): encode_code(origin, 'a' * 43, PASSWORD)

    def test_api_authorization_and_native_post_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransferStore(Path(tmp)); job = store.create(); store.path(job).write_bytes(b'encrypted'); store.finish(job)
            async def owner(): raise HTTPException(401)
            proof = AsyncMock()
            api = FastAPI(); api.include_router(build_router(Settings(_env_file=None, server_backup_dir=tmp), owner, proof, lambda req: ('xass.example','https://xass.example')))
            async def session(): yield None
            api.dependency_overrides[get_session] = session
            with TestClient(api) as client:
                self.assertEqual(client.get('/api/mini/server-backups/' + job).status_code, 401)
                self.assertEqual(client.post('/api/mini/server-backups', json={'passphrase': PASSWORD}).status_code, 401)
                self.assertEqual(client.post('/api/server-transfer/download', data={'ticket': 'fake'}).status_code, 410)
                ticket = store.ticket(job)
                r = client.post('/api/server-transfer/download', data={'ticket': ticket})
                self.assertEqual(r.content, b'encrypted'); self.assertIn('no-store', r.headers['cache-control'])
                self.assertEqual(client.post('/api/server-transfer/download', data={'ticket': ticket}).status_code, 410)
                async def authorized(): return SimpleNamespace(user_id=42)
                api.dependency_overrides[owner] = authorized
                r = client.post('/api/mini/server-backups/' + job + '/download-ticket')
                self.assertEqual(r.status_code, 200); proof.assert_awaited_once()


class MinimalBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_removes_keyboard_and_legacy_callbacks_redirect(self):
        bot = AsyncMock(); settings = Settings(_env_file=None, owner_user_id=42, profile_public_url='https://xass.example')
        handler = TelegramUpdateHandler(settings, bot)
        config = SimpleNamespace(service_base_url='https://xass.example')
        with patch('app.telegram_handler.get_or_create_app_config', AsyncMock(return_value=config)):
            await handler._handle_command(None, {'from': {'id':42}, 'chat': {'id':42}}, '/start')
            self.assertEqual(bot.send_message.call_args.kwargs['reply_markup'], {'remove_keyboard': True})
            self.assertIn('/miniapp.php', bot.send_message.call_args.args[1])
            await handler._handle_callback(None, {'id':'query', 'from': {'id':42}, 'message': {'chat': {'id':42}, 'message_id':1}, 'data':'panel:update'})
            self.assertEqual(bot.edit_message_text.call_args.kwargs['reply_markup'], {'inline_keyboard': []})

    async def test_business_deletion_still_logs_and_notifies_before_commands(self):
        handler = TelegramUpdateHandler(Settings(_env_file=None), AsyncMock())
        handler._cache_recent_message = AsyncMock(); handler._notify_edit_events = AsyncMock(); handler._notify_deleted_events = AsyncMock()
        update = {'deleted_business_messages': {'business_connection_id':'business', 'chat': {'id':12}, 'message_ids':[1]}}
        with patch('app.telegram_handler.get_or_create_app_config', AsyncMock(return_value=SimpleNamespace())), patch('app.telegram_handler.handle_update_logging', AsyncMock()) as log:
            await handler.handle_update(None, update)
            log.assert_awaited_once(); handler._notify_deleted_events.assert_awaited_once()


if __name__ == '__main__': unittest.main()
