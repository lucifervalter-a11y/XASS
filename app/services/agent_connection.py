from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

CONNECTION_FORMAT = "xass-connect"
CONNECTION_VERSION = 1


def normalize_server_origin(value: str) -> str | None:
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def build_connection_profile(
    *,
    server_url: str,
    pair_code: str,
    expires_at: datetime,
    source_name: str = "",
) -> dict[str, Any]:
    normalized = normalize_server_origin(server_url)
    if normalized is None:
        raise ValueError("Invalid XASS server URL")
    code = (pair_code or "").strip()
    if not code:
        raise ValueError("Pair code is empty")
    return {
        "format": CONNECTION_FORMAT,
        "version": CONNECTION_VERSION,
        "server_url": normalized,
        "pair_code": code,
        "source_name": (source_name or "").strip()[:128],
        "expires_at": expires_at.isoformat(),
        "auto_update": True,
    }
