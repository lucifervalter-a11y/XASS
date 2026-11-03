"""Bounded native-update intents; never an installer URL or install authority.

The authenticated command channel can request review. Only the interactive native
UI chooses an official channel/package and authorizes shutdown. This module has
no network, credentials or process-launch dependencies.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import time

CHANNELS = {"selected", "native", "native-test"}
OPERATIONS = {"check", "install", "rollback"}
TTL = 24 * 60 * 60


def normalize_request(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"operation", "channel"}:
        raise ValueError("Choose operation and channel; package URLs are not accepted")
    operation, channel = payload["operation"], payload["channel"]
    if not isinstance(operation, str) or operation not in OPERATIONS or not isinstance(channel, str) or channel not in CHANNELS:
        raise ValueError("Unknown native update operation/channel")
    if operation == "rollback" and channel != "selected":
        raise ValueError("Rollback uses the previous verified installation, not a release channel")
    return {"operation": operation, "channel": channel}


def _root(data: Path) -> Path:
    root = data / ".native-update-requests"
    root.mkdir(exist_ok=True)
    if root.is_symlink() or getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("Unsafe update request folder")
    return root


def enqueue(data: Path, command_id: int, payload: dict, *, now=None) -> dict:
    payload = normalize_request(payload)
    if type(command_id) is not int or not 0 < command_id < 2**63:
        raise ValueError("Invalid command identity")
    root = _root(data); target = root / f"{command_id}.json"
    if target.exists() or (root / f"{command_id}.handled").exists():
        return {"queued": False, "already_received": True, "requires_pc_confirmation": True}
    if len(list(root.glob("*.json"))) >= 16:
        raise ValueError("Too many requests; review existing requests on the PC first")
    value = {"schema": 1, "id": command_id, **payload, "created_at": time.time() if now is None else now}
    # Publish a complete record atomically. A crash cannot expose half a JSON DTO.
    temporary = root / f".{command_id}-{os.getpid()}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"queued": True, "requires_pc_confirmation": True, **payload}


def pending(data: Path, *, now=None) -> list[dict]:
    root = _root(data); now = time.time() if now is None else now
    result = []
    for path in sorted(root.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0)[:16]:
        if not re.fullmatch(r"[1-9][0-9]{0,18}\.json", path.name) or path.is_symlink() or path.stat().st_size > 4096:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (set(value) != {"schema", "id", "operation", "channel", "created_at"} or type(value["schema"]) is not int or value["schema"] != 1
                    or type(value["id"]) is not int or value["id"] != int(path.stem)
                    or type(value["created_at"]) not in (int, float)):
                continue
            normalize_request({k: value[k] for k in ("operation", "channel")})
            if not 0 <= now - value["created_at"] <= TTL:
                # Expired/future intents never install and never clog the bounded inbox.
                path.rename(path.with_suffix(".handled")); continue
            result.append(value)
        except (ValueError, TypeError, KeyError):
            continue
    return result


def acknowledge(data: Path, command_id: int) -> None:
    if type(command_id) is not int or not 0 < command_id < 2**63:
        raise ValueError("Invalid command identity")
    root = _root(data); path = root / f"{command_id}.json"
    if path.is_symlink(): raise ValueError("Unsafe update request")
    path.rename(path.with_suffix(".handled"))
