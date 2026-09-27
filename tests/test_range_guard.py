from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import FileResponse

import app.main as main
from app.range_guard import SingleRangeGuard


class RangeGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "fixture.bin"
        self.content = b"0123456789abcdef"
        self.path.write_bytes(self.content)
        self.entered = 0
        app = FastAPI()
        app.add_middleware(SingleRangeGuard)

        @app.get("/file")
        @app.head("/file")
        async def protected(request: Request):
            self.entered += 1
            if request.headers.get("x-fixture-owner") != "42":
                raise HTTPException(401)
            return FileResponse(self.path)

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def test_single_ranges_and_no_range_preserve_bytes_206_head_and_auth(self):
        for value, expected in [(None, self.content), ("bytes=0-3", b"0123"), ("bytes=12-", b"cdef"), ("bytes=-4", b"cdef")]:
            headers = {"x-fixture-owner": "42"}
            if value:
                headers["Range"] = value
            response = await self.client.get("/file", headers=headers)
            self.assertEqual(response.status_code, 206 if value else 200)
            self.assertEqual(response.content, expected)
            denied = await self.client.get("/file", headers={"Range": value} if value else {})
            self.assertEqual(denied.status_code, 401)
        head = await self.client.head("/file", headers={"x-fixture-owner": "42", "Range": "bytes=0-3"})
        self.assertEqual(head.status_code, 206)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["content-length"], "4")

    async def test_if_range_and_unsatisfiable_range_keep_file_response_semantics(self):
        initial = await self.client.get("/file", headers={"x-fixture-owner": "42"})
        for validator, status, expected in [(initial.headers["etag"], 206, b"01"), ('"changed"', 200, self.content)]:
            response = await self.client.get("/file", headers={"x-fixture-owner": "42", "Range": "bytes=0-1", "If-Range": validator})
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.content, expected)
        response = await self.client.get("/file", headers={"x-fixture-owner": "42", "Range": "bytes=99999999999999999999-"})
        self.assertEqual(response.status_code, 416)

    async def test_invalid_multi_duplicate_and_long_ranges_reject_before_route_or_parser(self):
        fixtures = [[("Range", value)] for value in ["", "bytes=", "bytes=-", "bytes=0-1,3-4", "items=0-1",
            "bytes=" + "0" * 21 + "-", "bytes=-" + "0" * 21, "bytes=0-" + "9" * 21,
            "bytes=" + "0" * 10000, "bytes=1-2 trailing", "bytes=+1-3"]]
        fixtures.append([("Range", "bytes=0-1"), ("range", "bytes=2-3")])
        with patch.object(FileResponse, "_parse_range_header", side_effect=AssertionError("unsafe parser reached")):
            for headers in fixtures:
                response = await self.client.get("/file", headers=headers)
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.entered, 0)

    async def test_non_http_scopes_are_passed_through(self):
        app = AsyncMock()
        scope, receive, send = {"type": "websocket", "headers": [(b"range", b"bad")]}, AsyncMock(), AsyncMock()
        await SingleRangeGuard(app)(scope, receive, send)
        app.assert_awaited_once_with(scope, receive, send)

    async def test_full_app_ticketed_installer_preserves_partial_download(self):
        artifact = SimpleNamespace(path=self.path, version="fixture", revision="fixture", sha256="a" * 64)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://fixture.invalid") as client:
            with patch.object(main, "verify_installer_ticket", return_value=True) as verify, \
                 patch.object(main, "get_agent_installer", return_value=artifact):
                response = await client.get("/api/agent-installer/download?ticket=fixture", headers={"Range": "bytes=0-1"})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, b"01")
                verify.assert_called_once()
                verify.reset_mock()
                response = await client.get("/api/agent-installer/download?ticket=fixture", headers={"Range": "bytes=0-1,3-4"})
                self.assertEqual(response.status_code, 416)
                verify.assert_not_called()
            with patch.object(main, "verify_installer_ticket", return_value=False):
                response = await client.get("/api/agent-installer/download?ticket=invalid", headers={"Range": "bytes=0-1"})
                self.assertEqual(response.status_code, 401)
