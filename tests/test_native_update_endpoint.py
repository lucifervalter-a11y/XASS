from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
import app.main as main

class NativeUpdateEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(id=7, source_name='PC')))
        self.context = ExitStack(); self.addCleanup(self.context.close)
        self.proof = self.context.enter_context(patch.object(main, '_require_pwa_action_proof', new_callable=AsyncMock))
        self.enqueue = self.context.enter_context(patch.object(main, 'enqueue_agent_command', new_callable=AsyncMock,
            return_value=SimpleNamespace(id=1, source_name='PC', command='native_update', status='pending', created_at=datetime.now(timezone.utc))))
        self.context.enter_context(patch.object(main, 'log_admin_action', new_callable=AsyncMock))
        self.context.enter_context(patch.object(main, 'miniapp_authenticate', return_value=None))
    async def call(self, payload):
        return await main.mini_agent_command('PC', SimpleNamespace(command='native_update', payload=payload, action_proof='fixture-proof'),
            x_telegram_init_data='', user=SimpleNamespace(user_id=42), session=self.session)
    async def test_channel_and_operation_are_bound_to_approval(self):
        payload = {'operation': 'install', 'channel': 'native-test'}
        result = await self.call(payload); self.assertTrue(result['ok'])
        self.assertEqual(self.proof.call_args.kwargs['binding'], {'source_id': 7, 'command': 'native_update', 'payload': payload})
        self.assertEqual(self.enqueue.call_args.kwargs['payload'], payload)
    async def test_missing_approval_cannot_enqueue_intent(self):
        self.proof.side_effect = HTTPException(status_code=403, detail='not approved')
        with self.assertRaises(HTTPException) as caught: await self.call({'operation': 'install', 'channel': 'native'})
        self.assertEqual(caught.exception.status_code, 403); self.enqueue.assert_not_awaited()
    async def test_url_and_invalid_channel_are_rejected_before_proof(self):
        for payload in ({'operation': 'install', 'channel': 'unknown'}, {'operation': 'install', 'channel': 'native', 'url': 'https://evil.invalid'}):
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as caught: await self.call(payload)
            self.assertEqual(caught.exception.status_code, 400)
        self.enqueue.assert_not_awaited(); self.proof.assert_not_awaited()
