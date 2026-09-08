from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.db import Base
from app.enums import SourceType
from app.models import AdminAction, AgentArchiveTarget, AgentCommand, AgentCredential, AgentStateSnapshot, HeartbeatSource
from app.schemas import HeartbeatPayload
from app.services.agent_commands import acknowledge_agent_commands, deliver_agent_commands, enqueue_agent_command
from app.services.agent_lifecycle import AgentDetachedError, detach_agent, ensure_agent_attached
from app.services.agent_pairing import authenticate_agent_api_key, claim_pair_code_and_issue_key, issue_pair_code
from app.services.heartbeat import delete_source_by_id, process_heartbeat
from app.services.miniapp import MiniAppUser


class AgentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + (Path(self.temp.name) / 'test.db').as_posix())
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            self.source = HeartbeatSource(source_name='ПК', source_type='PC_AGENT')
            self.other = HeartbeatSource(source_name='Other', source_type='PC_AGENT')
            session.add_all([self.source, self.other, AgentCredential(
                source_name='ПК', api_key_hash=hashlib.sha256(b'old-key').hexdigest(), key_hint='test', is_active=True,
            ), AgentArchiveTarget(source_name='ПК', enabled=True), AgentStateSnapshot(source_name='ПК', is_online=True)])
            self.commands = [AgentCommand(source_name='ПК', command='ping', status=s) for s in ['pending', 'delivered', 'completed', 'failed']]
            self.commands.append(AgentCommand(source_name='ПК', command='ping', status='pending', not_before_at=datetime.now(timezone.utc) + timedelta(days=1)))
            self.commands.append(AgentCommand(source_name='Other', command='ping', status='pending'))
            session.add_all(self.commands)
            await session.commit()

        async def get_session():
            async with self.sessions() as session:
                yield session
        self.owner = MiniAppUser(42, 'Owner', '', '', True)
        self.auth_user = self.owner
        self.context = ExitStack()
        self.context.enter_context(patch.dict(main.app.dependency_overrides, {main.get_session: get_session}))
        self.context.enter_context(patch.object(main, 'settings', SimpleNamespace(agent_api_key='global-key')))
        self.context.enter_context(patch.object(main, 'miniapp_authenticate', side_effect=lambda token, settings: self.auth_user if token == 'telegram' else None))
        self.context.enter_context(patch.object(main, 'pwa_authenticate_session', return_value=None))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://localhost')
        self.headers = {'X-Telegram-Init-Data': 'telegram'}
        self.body = {'source_id': self.source.id, 'confirm_name': 'ПК'}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.context.close()
        await self.engine.dispose()
        self.temp.cleanup()

    async def detach(self):
        return await self.client.request('DELETE', '/api/mini/agents/ПК', headers=self.headers, json=self.body)

    async def test_owner_confirmation_required_and_wrong_identity_does_not_mutate(self):
        for headers, body, status in [({}, self.body, 401), (self.headers, {}, 422),
                                      (self.headers, {**self.body, 'confirm_name': 'Other'}, 409),
                                      (self.headers, {**self.body, 'source_id': self.other.id}, 409)]:
            response = await self.client.request('DELETE', '/api/mini/agents/ПК', headers=headers, json=body)
            self.assertEqual(response.status_code, status, response.text)
        self.auth_user = MiniAppUser(77, 'Guest', '', '', False)
        self.assertEqual((await self.detach()).status_code, 403)
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(HeartbeatSource, self.source.id))
            self.assertTrue(await session.scalar(select(AgentCredential.is_active)))

    async def test_detach_is_atomic_idempotent_cancels_queue_and_preserves_history(self):
        response = await self.detach()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'ok': True, 'detached': True, 'already_detached': False, 'cancelled_commands': 3, 'archives_preserved': True})
        again = await self.detach()
        self.assertTrue(again.json()['already_detached'])
        async with self.sessions() as session:
            self.assertIsNone(await session.get(HeartbeatSource, self.source.id))
            self.assertIsNotNone(await session.get(HeartbeatSource, self.other.id))
            self.assertFalse(await session.scalar(select(AgentCredential.is_active)))
            self.assertFalse(await session.scalar(select(AgentArchiveTarget.enabled)))
            self.assertEqual(await session.scalar(select(func.count(AgentStateSnapshot.id))), 1)
            self.assertEqual(await session.scalar(select(func.count(AdminAction.id))), 1)
            commands = list(await session.scalars(select(AgentCommand).order_by(AgentCommand.id)))
            self.assertEqual([x.status for x in commands], ['cancelled', 'cancelled', 'completed', 'failed', 'cancelled', 'pending'])
            self.assertEqual(commands[0].result['details']['reason'], 'agent_detached')
            self.assertIsNotNone(commands[1].completed_at)

    async def test_stale_issued_and_shared_key_heartbeats_cannot_recreate_agent(self):
        await self.detach()
        for key, status in [('old-key', 401), ('global-key', 410)]:
            response = await self.client.post('/agent/heartbeat', headers={'X-Api-Key': key}, json={'source_name': 'ПК'})
            self.assertEqual(response.status_code, status, response.text)
        async with self.sessions() as session:
            self.assertIsNone(await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == 'ПК')))

    async def test_inflight_authenticated_session_rechecks_tombstone(self):
        async with self.sessions() as stale:
            auth = await authenticate_agent_api_key(stale, api_key='old-key', global_agent_api_key='global-key')
            self.assertIsNotNone(auth)
            # Keep the active credential in the identity map: stale ORM cache must not win.
            cached = await stale.get(AgentCredential, auth.credential_id)
            await self.detach()
            self.assertTrue(cached.is_active)
            with self.assertRaises(AgentDetachedError):
                await process_heartbeat(stale, HeartbeatPayload(source_name=auth.source_name))

    async def test_commands_and_late_ack_are_blocked_after_detach(self):
        await self.detach()
        for operation in [
            lambda s: enqueue_agent_command(s, source_name='ПК', command='ping', payload={}, actor_user_id=42),
            lambda s: deliver_agent_commands(s, source_name='ПК'),
            lambda s: acknowledge_agent_commands(s, source_name='ПК', results=[{'id': self.commands[1].id, 'ok': True}]),
        ]:
            async with self.sessions() as session:
                with self.assertRaises(AgentDetachedError):
                    await operation(session)
        async with self.sessions() as session:
            self.assertEqual((await session.get(AgentCommand, self.commands[1].id)).status, 'cancelled')

    async def test_legacy_bot_delete_reserves_name_but_does_not_revoke_other_agents(self):
        async with self.sessions() as session:
            await delete_source_by_id(session, self.other.id)
            self.assertFalse(await session.scalar(select(AgentCredential.is_active).where(AgentCredential.source_name == 'Other')))
            self.assertIsNotNone(await authenticate_agent_api_key(session, api_key='old-key', global_agent_api_key='global-key'))
            with self.assertRaises(AgentDetachedError):
                await process_heartbeat(session, HeartbeatPayload(source_name='Other'))

    async def test_explicit_repair_gets_new_identity_and_never_old_commands(self):
        await self.detach()
        async with self.sessions() as session:
            issued = await issue_pair_code(session, actor_user_id=42)
            pair = await claim_pair_code_and_issue_key(session, pair_code=issued.code, source_name='ПК', source_type=SourceType.PC_AGENT)
            self.assertEqual(pair.source_name, 'ПК-2')
            await process_heartbeat(session, HeartbeatPayload(source_name=pair.source_name))
            self.assertEqual(await deliver_agent_commands(session, source_name=pair.source_name), [])
            self.assertIsNone(await authenticate_agent_api_key(session, api_key='old-key', global_agent_api_key='global-key'))

    async def test_source_lock_orders_inflight_heartbeat_before_detach(self):
        async with self.sessions() as heartbeat:
            await ensure_agent_attached(heartbeat, 'ПК')
            started = asyncio.Event()
            async def remove():
                async with self.sessions() as session:
                    started.set()
                    return await detach_agent(session, source_name='ПК', source_id=self.source.id)
            task = asyncio.create_task(remove())
            await started.wait()
            await process_heartbeat(heartbeat, HeartbeatPayload(source_name='ПК', metrics={'cpu_percent': 20}))
            await asyncio.wait_for(task, 5)
        async with self.sessions() as session:
            with self.assertRaises(AgentDetachedError):
                await process_heartbeat(session, HeartbeatPayload(source_name='ПК'))
            self.assertIsNone(await session.get(HeartbeatSource, self.source.id))

    async def test_revoked_legacy_source_cannot_download_or_upload_workspace_files(self):
        await self.detach()
        with patch.object(main, 'load_workspace_asset') as loader, patch.object(main, 'store_workspace_asset') as store:
            for method, path in [('GET', '/agent/workspace/assets/token'), ('POST', '/agent/workspace/assets/1'), ('GET', '/agent/archive/media/1')]:
                response = await self.client.request(method, path, headers={'X-Api-Key': 'global-key'}, params={'source_name': 'ПК'})
                self.assertEqual(response.status_code, 410, response.text)
            loader.assert_not_called()
            store.assert_not_called()

    async def test_pwa_requires_passkey_proof_bound_to_source_id_and_name(self):
        with patch.object(main, 'pwa_authenticate_session', return_value=self.owner), \
             patch.object(main, 'passkey_count_credentials', new=AsyncMock(return_value=1)), \
             patch.object(main, 'verify_action_proof', return_value=False) as verify:
            denied = await self.client.request('DELETE', '/api/mini/agents/ПК', json=self.body)
            self.assertEqual(denied.status_code, 428)
            verify.assert_called_once_with('', 42, f'agent:detach:{self.source.id}:ПК', main.settings)
            verify.return_value = True
            accepted = await self.client.request('DELETE', '/api/mini/agents/ПК', json={**self.body, 'action_proof': 'approved'})
            self.assertEqual(accepted.status_code, 200, accepted.text)


if __name__ == '__main__':
    unittest.main()
