"""Bound Range parsing before the pinned Starlette FileResponse implementation."""
import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


MAX_RANGE_BYTES = 48
_SINGLE_RANGE = re.compile(rb"bytes=(?:[0-9]{1,20}-[0-9]{0,20}|-[0-9]{1,20})")


class SingleRangeGuard:
    """Support normal seeking/resume, without multi-range regex/merge workloads."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            ranges = [value for name, value in scope.get("headers", ()) if name.lower() == b"range"]
            if ranges and (len(ranges) != 1 or len(ranges[0]) > MAX_RANGE_BYTES
                           or _SINGLE_RANGE.fullmatch(ranges[0].strip(b" \t")) is None):
                # No body, filesystem, auth, route or FileResponse work is needed.
                response = JSONResponse(status_code=416, content={"detail": "Only one bounded bytes range is supported"},
                                        headers={"Cache-Control": "no-store"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
