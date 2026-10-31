import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import Base
from app.models import AccessGrant, AppConfig, BusinessConnection, MessageLog
from app.services.access_grants import (
    find_user_by_username,
    get_business_connection,
    get_user_connection_ids,
    grant_user_access,
    has_active_grant,
    is_user_permitted,
    list_all_grants,
    revoke_user_access,
    upsert_business_connection,
)
from app.telegram_handler import TelegramUpdateHandler


class TestAccessDelegation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.session = self.session_factory()

        self.owner_id = 10001
        self.settings = Settings(
            _env_file=None,
            owner_user_id=self.owner_id,
            authorized_user_ids=[self.owner_id],
            admin_user_ids=[self.owner_id],
            bot_token="123456:TEST_TOKEN",
            telegram_management_buttons=True,
        )
        self.bot_client = AsyncMock()
        self.handler = TelegramUpdateHandler(self.settings, self.bot_client)

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_grant_user_access_and_permissions(self):
        # 1. Initially user 20002 has no grant
        self.assertFalse(await has_active_grant(self.session, 20002))
        self.assertFalse(await is_user_permitted(self.session, 20002, "business", self.settings))

        # 2. Grant access
        grant = await grant_user_access(
            self.session,
            user_id=20002,
            granted_by=self.owner_id,
            username="alice",
            first_name="Alice",
            can_business=True,
            can_view_deleted=True,
            can_music=False,
            note="Team member",
        )
        self.assertEqual(grant.user_id, 20002)
        self.assertTrue(grant.is_active)
        self.assertTrue(grant.can_business)
        self.assertTrue(grant.can_view_deleted)
        self.assertFalse(grant.can_music)

        # 3. Check permission helpers
        self.assertTrue(await has_active_grant(self.session, 20002))
        self.assertTrue(await is_user_permitted(self.session, 20002, "business", self.settings))
        self.assertTrue(await is_user_permitted(self.session, 20002, "view_deleted", self.settings))
        self.assertFalse(await is_user_permitted(self.session, 20002, "music", self.settings))

        # 4. Find by username
        found_id = await find_user_by_username(self.session, "@alice")
        self.assertEqual(found_id, 20002)

        # 5. Revoke access
        revoked = await revoke_user_access(self.session, 20002)
        self.assertTrue(revoked)
        self.assertFalse(await has_active_grant(self.session, 20002))
        self.assertFalse(await is_user_permitted(self.session, 20002, "business", self.settings))

    async def test_owner_grant_command_by_id(self):
        msg = {
            "message_id": 1,
            "from": {"id": self.owner_id, "first_name": "Owner"},
            "chat": {"id": self.owner_id},
            "text": "/grant 30003 all",
        }
        await self.handler._handle_grant_command(self.session, msg, msg["text"], self.owner_id, self.owner_id)

        # Verify AccessGrant in DB
        grant = await self.session.scalar(
            select(AccessGrant).where(AccessGrant.user_id == 30003)
        )
        self.assertIsNotNone(grant)
        self.assertTrue(await is_user_permitted(self.session, 30003, "business", self.settings))
        self.assertTrue(await is_user_permitted(self.session, 30003, "music", self.settings))

        # Verify confirmation message to owner
        owner_calls = [
            c for c in self.bot_client.send_message.call_args_list
            if c.args[0] == self.owner_id and "Доступ успешно выдан" in str(c)
        ]
        self.assertEqual(len(owner_calls), 1)

    async def test_owner_grant_command_by_reply_to_forward(self):
        msg = {
            "message_id": 2,
            "from": {"id": self.owner_id},
            "chat": {"id": self.owner_id},
            "text": "/grant",
            "reply_to_message": {
                "message_id": 1,
                "forward_origin": {
                    "type": "user",
                    "sender_user": {"id": 40004, "first_name": "Bob", "username": "bob_music"},
                },
            },
        }
        await self.handler._handle_grant_command(self.session, msg, msg["text"], self.owner_id, self.owner_id)

        grant = await self.session.scalar(
            select(AccessGrant).where(AccessGrant.user_id == 40004)
        )
        self.assertIsNotNone(grant)
        self.assertTrue(await is_user_permitted(self.session, 40004, "business", self.settings))

    async def test_unauthorized_user_business_connection_rejected(self):
        config = AppConfig(id=1, save_mode="SAVE_BASIC", notify_chat_id=self.owner_id)
        self.session.add(config)
        await self.session.commit()

        update = {
            "update_id": 101,
            "business_connection": {
                "id": "bc_unauth_999",
                "user": {"id": 99999, "first_name": "Stranger", "username": "stranger"},
                "user_chat_id": 99999,
                "can_reply": True,
                "is_enabled": True,
            },
        }
        await self.handler.handle_update(self.session, update)

        # Connection should NOT be active in DB
        conn = await get_business_connection(self.session, "bc_unauth_999")
        self.assertIsNone(conn)

        # User 99999 receives restriction notice
        user_calls = [
            c for c in self.bot_client.send_message.call_args_list
            if c.args[0] == 99999 and "Доступ ограничен" in str(c)
        ]
        self.assertEqual(len(user_calls), 1)

        # Owner receives unauthorized attempt alert
        owner_calls = [
            call for call in self.bot_client.send_message.call_args_list
            if call.args[0] == self.owner_id and "Попытка подключения без доступа" in str(call)
        ]
        self.assertEqual(len(owner_calls), 1)

    async def test_delegated_user_business_connection_and_deleted_message_routing(self):
        # 1. Grant user 50005 access
        await grant_user_access(
            self.session,
            user_id=50005,
            granted_by=self.owner_id,
            username="charlie",
            can_business=True,
            can_view_deleted=True,
        )

        config = AppConfig(id=1, save_mode="SAVE_BASIC", notify_chat_id=self.owner_id)
        self.session.add(config)
        await self.session.commit()

        # 2. Telegram delivers business_connection
        conn_update = {
            "update_id": 102,
            "business_connection": {
                "id": "bc_charlie_500",
                "user": {"id": 50005, "first_name": "Charlie", "username": "charlie"},
                "user_chat_id": 50005,
                "can_reply": True,
                "is_enabled": True,
            },
        }
        await self.handler.handle_update(self.session, conn_update)

        conn = await get_business_connection(self.session, "bc_charlie_500")
        self.assertIsNotNone(conn)
        self.assertTrue(conn.is_enabled)
        self.assertEqual(conn.user_id, 50005)

        # 3. Message arrives in Charlie's business chat
        msg_update = {
            "update_id": 103,
            "business_message": {
                "message_id": 888,
                "business_connection_id": "bc_charlie_500",
                "chat": {"id": -100555, "type": "supergroup", "title": "Charlie's Client"},
                "from": {"id": 77777, "first_name": "Client", "username": "client"},
                "date": int(datetime.now(timezone.utc).timestamp()),
                "text": "Confidential proposal details",
            },
        }
        await self.handler.handle_update(self.session, msg_update)

        # Verify message is logged with business_connection_id
        log = await self.session.scalar(
            select(MessageLog).where(MessageLog.telegram_message_id == 888)
        )
        self.assertIsNotNone(log)
        self.assertEqual(log.business_connection_id, "bc_charlie_500")

        # 4. Message is deleted
        self.bot_client.send_message.reset_mock()
        del_update = {
            "update_id": 104,
            "deleted_business_messages": {
                "business_connection_id": "bc_charlie_500",
                "chat": {"id": -100555, "type": "supergroup", "title": "Charlie's Client"},
                "message_ids": [888],
            },
        }
        await self.handler.handle_update(self.session, del_update)

        # CRITICAL ASSERTION: The deletion alert was sent to CHARLIE (50005), NOT to the owner!
        charlie_del_calls = [
            c for c in self.bot_client.send_message.call_args_list
            if c.args[0] == 50005 and "🗑" in str(c)
        ]
        self.assertEqual(len(charlie_del_calls), 1)
        self.assertIn("Confidential proposal details", charlie_del_calls[0].args[1])

        owner_del_calls = [
            c for c in self.bot_client.send_message.call_args_list
            if c.args[0] == self.owner_id and "🗑" in str(c)
        ]
        self.assertEqual(len(owner_del_calls), 0, "Owner must NOT receive Charlie's business chat deletions")

    async def test_delegated_user_deleted_and_chats_queries_isolation(self):
        # Create Owner message (should be isolated)
        owner_msg = MessageLog(
            chat_id=999001,
            chat_type="private",
            chat_title="Owner Secret Chat",
            telegram_message_id=10,
            from_user_id=self.owner_id,
            direction="outgoing",
            text_content="Owner top secret message",
            deleted=True,
            business_connection_id=None,
        )
        self.session.add(owner_msg)

        # Create Charlie message and connection
        await grant_user_access(self.session, user_id=60006, username="charlie2")
        await upsert_business_connection(
            self.session, connection_id="bc_user_6", user_id=60006, is_enabled=True
        )
        charlie_msg = MessageLog(
            chat_id=888002,
            chat_type="private",
            chat_title="Charlie Work Chat",
            telegram_message_id=20,
            from_user_id=77777,
            direction="incoming",
            text_content="Charlie visible deleted message",
            deleted=True,
            business_connection_id="bc_user_6",
        )
        self.session.add(charlie_msg)
        await self.session.commit()

        # 1. Charlie runs /deleted
        self.bot_client.send_message.reset_mock()
        await self.handler._handle_deleted_history_command(self.session, 60006, user_id=60006)

        sent_text = self.bot_client.send_message.call_args.args[1]
        self.assertIn("Charlie visible deleted message", sent_text)
        self.assertNotIn("Owner top secret message", sent_text)

        # 2. Charlie runs /chats
        self.bot_client.send_message.reset_mock()
        await self.handler._handle_chats_command(self.session, 60006, user_id=60006)

        chats_text = self.bot_client.send_message.call_args.args[1]
        self.assertIn("Charlie Work Chat", chats_text)
        self.assertNotIn("Owner Secret Chat", chats_text)

        # 3. Charlie attempts to view Owner chat history via /chat 999001
        self.bot_client.send_message.reset_mock()
        await self.handler._handle_chat_history_command(self.session, 60006, "/chat 999001", user_id=60006)
        denied_text = self.bot_client.send_message.call_args.args[1]
        self.assertIn("нет к нему доступа", denied_text)

    async def test_delegated_user_blocked_from_owner_commands(self):
        await grant_user_access(self.session, user_id=70007, username="viewer")

        # Try /server
        self.bot_client.send_message.reset_mock()
        msg = {
            "message_id": 99,
            "from": {"id": 70007},
            "chat": {"id": 70007},
            "text": "/server",
        }
        await self.handler._handle_command(self.session, msg, "/server")
        resp = self.bot_client.send_message.call_args.args[1]
        self.assertIn("только владельцу", resp)

        # Try /start -> receives delegated guide
        self.bot_client.send_message.reset_mock()
        await self.handler._handle_command(self.session, msg, "/start")
        guide = self.bot_client.send_message.call_args.args[1]
        self.assertIn("Telegram Business", guide)
        self.assertIn("/deleted", guide)

    async def test_access_list_and_callback_revocation(self):
        await grant_user_access(self.session, user_id=80008, username="dave", can_business=True)
        await upsert_business_connection(self.session, connection_id="bc_dave", user_id=80008, is_enabled=True)

        # View /access
        self.bot_client.send_message.reset_mock()
        await self.handler._handle_access_command(self.session, self.owner_id, self.owner_id)
        access_text = self.bot_client.send_message.call_args.args[1]
        self.assertIn("dave", access_text)
        self.assertIn("80008", access_text)

        # Revoke via callback query
        callback = {
            "id": "cb_1",
            "from": {"id": self.owner_id},
            "message": {"chat": {"id": self.owner_id}, "message_id": 50},
            "data": "grant:revoke:80008",
        }
        await self.handler._handle_callback(self.session, callback)

        # Check DB that grant and connection are inactive
        self.assertFalse(await has_active_grant(self.session, 80008))
        conn = await get_business_connection(self.session, "bc_dave")
        self.assertFalse(conn.is_enabled)


if __name__ == "__main__":
    unittest.main()
