"""Frozen Python companion for WinUI, with a fixed role allowlist.

This is a console-subsystem executable so redirected JSON stdin/stdout work.
WinUI starts it hidden. It never evaluates a user-supplied Python script, installs
packages, downloads models, or starts a background agent at import/health-check.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
from pathlib import Path
import sys

ROLES = {
    "agent-bridge": "bridge",
    "desktop-music": "desktop_music_service",
    "assistant": "assistant_bridge",
    "listener": "background_voice_bridge",
    "background-agent": "background_agent",
    "native-updater": "native_updater",
}
HEALTH_IMPORTS = ("httpx", "psutil", "cryptography", "miniaudio", "numpy", "faster_whisper")


def health_check() -> dict:
    for module in HEALTH_IMPORTS:
        importlib.import_module(module)
    for module in ROLES.values():
        # Check code is present without importing an agent, model, microphone or config.
        if importlib.util.find_spec(module) is None:
            raise RuntimeError("Missing companion role")
    return {"ok": True, "frozen": bool(getattr(sys, "frozen", False)),
            "python": ".".join(map(str, sys.version_info[:3])),
            "roles": sorted(ROLES), "whisper_model_bundled": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="XASS Native companion")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--role", choices=tuple(ROLES))
    selection.add_argument("--health-check", action="store_true")
    args, remaining = parser.parse_known_args(argv)
    if args.health_check:
        if remaining:
            parser.error("Health check takes no other arguments")
        try:
            print(json.dumps(health_check(), ensure_ascii=True), flush=True)
            return 0
        except Exception:
            print('{"ok":false,"error":"Required frozen dependency or role is missing"}', flush=True)
            return 1
    if args.role in {"assistant", "listener"} and remaining:
        parser.error("Assistant roles accept their request through standard input only")
    sys.argv = [sys.argv[0], *remaining]
    module = importlib.import_module(ROLES[args.role])
    module.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
