from __future__ import annotations

import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

import app.main as main


class WorkspaceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_unicode_query_identity_and_filename_reach_the_workspace(self):
        auth = SimpleNamespace(source_name="Мой ПК")
        command = SimpleNamespace(id=7, command="file_download", source_name="Мой ПК", status="delivered")
        request = Request({"type": "http", "headers": [(b"content-type", b"application/octet-stream")]})
        request._body = b"example"
        session = SimpleNamespace(get=AsyncMock(return_value=command))
        with patch.object(main, "authenticate_agent_api_key", AsyncMock(return_value=auth)), patch.object(main, "store_workspace_asset", return_value={"token": "test"}) as store:
            result = await main.agent_workspace_asset_upload(
                7, request, kind="file_download", filename="Заметки.txt", source_name="Мой ПК",
                session=session, x_api_key="test", x_xass_source=None, x_xass_filename="xass-file.txt",
            )
            self.assertTrue(result["ok"])
            self.assertEqual(store.call_args.kwargs["filename"], "Заметки.txt")
            self.assertEqual(store.call_args.kwargs["source_name"], "Мой ПК")

    async def test_sealed_download_is_identified_without_exposing_plaintext_mime(self):
        metadata = {"kind": "file_upload", "cipher": "xass-sealed-v1", "content_type": "text/plain", "filename": "Заметки.txt"}
        with patch.object(main, "authenticate_agent_api_key", AsyncMock(return_value=SimpleNamespace(source_name="Мой ПК"))), patch.object(main, "load_workspace_asset", return_value=(metadata, Path("unused.bin"))) as load:
            response = await main.agent_workspace_asset_download("token", source_name="Мой ПК", session=AsyncMock(), x_api_key="test", x_xass_source=None)
            self.assertEqual(response.headers["content-type"], "application/x-xass-sealed")
            self.assertEqual(response.headers["x-xass-cipher"], "xass-sealed-v1")
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertEqual(load.call_args.kwargs["source_name"], "Мой ПК")

    async def test_bound_key_cannot_request_another_device_through_query(self):
        with patch.object(main, "authenticate_agent_api_key", AsyncMock(return_value=SimpleNamespace(source_name="Мой ПК"))), patch.object(main, "load_workspace_asset") as load:
            with self.assertRaises(HTTPException) as error:
                await main.agent_workspace_asset_download("token", source_name="Другой ПК", session=AsyncMock(), x_api_key="test", x_xass_source=None)
            self.assertEqual(error.exception.status_code, 403)
            load.assert_not_called()

    async def test_missing_key_is_rejected_before_reading_workspace(self):
        with patch.object(main, "authenticate_agent_api_key", AsyncMock(return_value=None)), patch.object(main, "load_workspace_asset") as load:
            with self.assertRaises(HTTPException) as error:
                await main.agent_workspace_asset_download("token", source_name="Мой ПК", session=AsyncMock(), x_api_key=None, x_xass_source=None)
            self.assertEqual(error.exception.status_code, 401)
            load.assert_not_called()
