from __future__ import annotations

import io
import json
from typing import Any

import segno


def connection_profile_svg(profile: dict[str, Any]) -> str:
    """Create an offline-scannable QR for the existing .xass connection payload."""
    payload = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
    qr = segno.make(payload, error="m", micro=False)
    output = io.BytesIO()
    qr.save(
        output,
        kind="svg",
        scale=5,
        border=2,
        dark="#080b10",
        light="#ffffff",
        xmldecl=False,
        svgns=True,
    )
    return output.getvalue().decode("utf-8")
