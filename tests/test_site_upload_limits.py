from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
import httpx
from starlette.requests import Request

import app.main as main


def streamed(chunks, headers=()):
    chunks = list(chunks)
    calls = []

    async def receive():
        index = len(calls)
        calls.append(index)
        if index >= len(chunks):
            raise AssertionError("Read beyond bounded chunks")
        return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}

    return Request({"type": "http", "headers": list(headers)}, receive), calls


class SiteUploadLimitTests(unittest.IsolatedAsyncioTestCase):
    endpoints = (("avatar", 8 * 1024 * 1024), ("cover", 10 * 1024 * 1024))

    async def upload(self, kind, request):
        with patch.object(main, "load_projects", return_value=[{"id": "fixture"}]), \
             patch.object(main, "save_profile_with_backup") as profile_write, \
             patch.object(main, "save_projects") as project_write, \
             patch.object(main.Path, "write_bytes") as file_write:
            try:
                if kind == "avatar":
                    return await main.mini_site_avatar_upload(request, user=SimpleNamespace(user_id=42))
                return await main.mini_site_project_cover_upload("fixture", request, user=SimpleNamespace(user_id=42))
            finally:
                profile_write.assert_not_called()
                project_write.assert_not_called()
                file_write.assert_not_called()

    async def test_announced_oversize_refused_before_reading_or_writing(self):
        for kind, limit in self.endpoints:
            with self.subTest(kind=kind):
                request, calls = streamed([b"unread"], [(b"content-length", str(limit + 1).encode())])
                with self.assertRaises(HTTPException) as error:
                    await self.upload(kind, request)
                self.assertEqual(error.exception.status_code, 413)
                self.assertEqual(calls, [])

    async def test_chunked_and_forged_small_length_stop_at_first_excess_chunk(self):
        for kind, limit in self.endpoints:
            for headers in ([], [(b"content-length", b"1")], [(b"content-length", b"invalid")],
                            [(b"content-type", b"application/x-xass-sealed")]):
                with self.subTest(kind=kind, headers=headers):
                    request, calls = streamed([b"a" * limit, b"x", b"must not read"], headers)
                    with self.assertRaises(HTTPException) as error:
                        await self.upload(kind, request)
                    self.assertEqual(error.exception.status_code, 413)
                    self.assertEqual(len(calls), 2)

    async def test_exact_limit_reaches_content_validation_and_empty_rules_unchanged(self):
        for kind, limit in self.endpoints:
            for body, code in ((b"a" * limit, 415), (b"", 400), (b"not an image", 415)):
                with self.subTest(kind=kind, size=len(body)):
                    request, _ = streamed([body], [(b"content-type", b"image/png")])
                    with self.assertRaises(HTTPException) as error:
                        await self.upload(kind, request)
                    self.assertEqual(error.exception.status_code, code)

    async def test_owner_auth_remains_before_body_consumption(self):
        app = FastAPI()
        app.add_api_route("/avatar", main.mini_site_avatar_upload, methods=["POST"])
        app.add_api_route("/projects/{project_id}/cover", main.mini_site_project_cover_upload, methods=["POST"])

        async def deny():
            raise HTTPException(status_code=403, detail="Owner required")

        app.dependency_overrides[main.require_mini_owner] = deny
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for path in ("/avatar", "/projects/fixture/cover"):
                reads = []

                async def body():
                    reads.append(True)
                    yield b"must not read"

                response = await client.post(path, content=body())
                self.assertEqual(response.status_code, 403)
                self.assertEqual(reads, [])
