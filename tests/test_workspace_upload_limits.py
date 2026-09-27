from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

import app.main as main
from app.services.agent_workspace import AssetUploadTooLarge, read_asset_body


def streamed(chunks, headers=()):
    chunks = list(chunks)
    calls = []
    async def receive():
        index = len(calls)
        calls.append(index)
        if index >= len(chunks):
            raise AssertionError("Upload read beyond supplied bounded chunks")
        return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}
    return Request({"type": "http", "headers": list(headers)}, receive), calls


class WorkspaceUploadLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(agent_file_max_bytes=16, agent_screenshot_max_bytes=8, agent_api_key="fixture")

    async def test_announced_oversize_rejected_without_reading_body(self):
        request, calls = streamed([b"never read"], [(b"content-length", b"10000000000000000000000")])
        with self.assertRaises(AssetUploadTooLarge):
            await read_asset_body(request, self.settings, kind="file_upload")
        self.assertEqual(calls, [])

    async def test_chunked_and_forged_small_length_stop_at_first_excess_chunk(self):
        for headers in [[], [(b"content-length", b"1")], [(b"content-length", b"invalid")]]:
            request, calls = streamed([b"a" * 16, b"b", b"must not read"], headers)
            with self.assertRaises(AssetUploadTooLarge):
                await read_asset_body(request, self.settings, kind="file_upload")
            self.assertEqual(len(calls), 2)

    async def test_exact_file_and_screenshot_limits_and_encryption_overhead(self):
        for kind, limit in [("file_upload", 16), ("file_download", 16), ("screenshot", 8)]:
            request, _ = streamed([b"a" * limit])
            self.assertEqual(await read_asset_body(request, self.settings, kind=kind), b"a" * limit)
            for headers in [[(b"content-type", b"application/x-xass-sealed")], [(b"x-xass-cipher", b"xass-sealed-v1")]]:
                blob = b"XASS\x01" + b"a" * (limit + 28)
                request, _ = streamed([blob[:5], blob[5:]], headers)
                self.assertEqual(await read_asset_body(request, self.settings, kind=kind), blob)
                request, _ = streamed([blob + b"x"], headers)
                with self.assertRaises(AssetUploadTooLarge):
                    await read_asset_body(request, self.settings, kind=kind)

    async def test_agent_route_authorizes_before_stream_and_refuses_oversize_without_asset(self):
        command = SimpleNamespace(id=7, command="screenshot", source_name="PC", status="delivered")
        session = SimpleNamespace(get=AsyncMock(return_value=command))
        with patch.object(main, "settings", self.settings), \
             patch.object(main, "ensure_agent_attached", new=AsyncMock()), \
             patch.object(main, "authenticate_agent_api_key", new=AsyncMock(return_value=SimpleNamespace(source_name="PC"))) as auth, \
             patch.object(main, "store_workspace_asset") as store:
            for mode, code, reads in [("oversize", 413, 2), ("anonymous", 401, 0), ("wrong-command", 403, 0)]:
                request, calls = streamed([b"a" * 8, b"b", b"unread"])
                auth.return_value = None if mode == "anonymous" else SimpleNamespace(source_name="PC")
                command.status = "completed" if mode == "wrong-command" else "delivered"
                with self.assertRaises(HTTPException) as error:
                    await main.agent_workspace_asset_upload(7, request, kind="screenshot", source_name="PC",
                        session=session, x_api_key="fixture", x_xass_source=None, x_xass_filename=None)
                self.assertEqual(error.exception.status_code, code)
                self.assertEqual(len(calls), reads)
            store.assert_not_called()

    async def test_owner_upload_refuses_oversize_without_file_or_queued_command(self):
        session = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(id=7, source_name="PC")))
        request, calls = streamed([b"a" * 16, b"b", b"unread"])
        with patch.object(main, "settings", self.settings), patch.object(main, "store_workspace_asset") as store, \
             patch.object(main, "enqueue_agent_command", new=AsyncMock()) as enqueue:
            with self.assertRaises(HTTPException) as error:
                await main.mini_agent_file_upload("PC", request, root="documents", path="", filename="file.txt",
                    user=SimpleNamespace(user_id=42), session=session)
            self.assertEqual(error.exception.status_code, 413)
            self.assertEqual(len(calls), 2)
            store.assert_not_called()
            enqueue.assert_not_called()

    async def test_owner_upload_preserves_sealed_bytes_and_filename(self):
        source = SimpleNamespace(id=7, source_name="Мой ПК")
        session = SimpleNamespace(scalar=AsyncMock(return_value=source))
        blob = b"XASS\x01" + b"a" * 44
        request, _ = streamed([blob[:7], blob[7:]], [(b"content-type", b"application/x-xass-sealed")])
        with patch.object(main, "settings", self.settings), \
             patch.object(main, "store_workspace_asset", return_value={"token": "fixture", "filename": "мой.txt"}) as store, \
             patch.object(main, "enqueue_agent_command", new=AsyncMock(return_value=SimpleNamespace(id=9, status="pending"))) as enqueue, \
             patch.object(main, "log_admin_action", new=AsyncMock()):
            result = await main.mini_agent_file_upload("Мой ПК", request, root="documents", path="", filename="мой.txt",
                user=SimpleNamespace(user_id=42), session=session)
            self.assertTrue(result["ok"])
            self.assertEqual(store.call_args.kwargs["body"], blob)
            self.assertEqual(store.call_args.kwargs["filename"], "мой.txt")
            self.assertEqual(enqueue.await_args.kwargs["payload"]["asset_token"], "fixture")
