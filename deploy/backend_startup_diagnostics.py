#!/usr/bin/env python3
"""Read-only startup diagnosis: raw journal data never leaves bounded RAM.

Run from stdin using the existing backend virtualenv. This script does not
import the application, inspect configuration/DB contents, or change services.
Only fixed enums, known exception names, tracked app frames, dependency versions
and a verified git object ID may reach stdout.
"""
from __future__ import annotations

import importlib.metadata
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading

ROOT = Path("/home/red/serverredus")
SERVICE = "serverredus-backend.service"
SINCE = "2026-10-30 19:40:50 UTC"
UNTIL = "2026-10-30 19:42:40 UTC"
FAILED_OVERLAY = "a4aa5913d59f21d07f55b5778eca3d6b64ebe79b"
MAX_BYTES = 1024 * 1024
START_US = int(datetime(2026, 10, 30, 19, 40, 50, tzinfo=timezone.utc).timestamp()) * 1_000_000
END_US = START_US + 110_000_000
DEPENDENCIES = (
    "fastapi", "starlette", "uvicorn", "sqlalchemy", "aiosqlite", "asyncpg",
    "pydantic", "pydantic-settings", "httpx", "cryptography", "python-multipart",
    "mutagen", "Pillow", "psutil", "websockets", "httptools", "watchfiles",
    "PyYAML", "PyNaCl",
)
EXCEPTIONS = frozenset((
    "ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError",
    "TypeError", "ValueError", "AttributeError", "NameError", "KeyError",
    "RuntimeError", "AssertionError", "OSError", "PermissionError",
    "FileNotFoundError", "TimeoutError", "ConnectionError", "ValidationError",
    "SettingsError", "OperationalError", "IntegrityError", "ProgrammingError",
    "InvalidRequestError", "ArgumentError", "InvalidSignature",
))
MODULE_ROOTS = frozenset((
    "fastapi", "starlette", "uvicorn", "sqlalchemy", "aiosqlite", "asyncpg",
    "pydantic", "pydantic_settings", "pydantic_core", "httpx", "httpcore",
    "cryptography", "multipart", "python_multipart", "mutagen", "PIL",
    "psutil", "websockets", "httptools", "watchfiles", "yaml", "nacl",
    "anyio", "h11", "uvloop", "dotenv", "typing_extensions", "greenlet",
    "webauthn", "segno", "pywebpush", "tzdata", "email_validator",
))
APP_PATH = re.compile(r"app/(?:[a-zA-Z_][a-zA-Z_0-9]{0,63}/){0,8}[a-zA-Z_][a-zA-Z_0-9]{0,63}\.py\Z")
FRAME = re.compile(r'^\s*File "/home/red/serverredus/(app/[^"\r\n]{1,512})", line ([1-9][0-9]{0,5})(?:, in [A-Za-z_][A-Za-z_0-9]*|, in <module>)?\s*$')
EXCEPTION = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z_0-9]*\.)*([A-Za-z_][A-Za-z_0-9]*)(?::|\s*$)")
MISSING = re.compile(r"^\s*ModuleNotFoundError: No module named '([A-Za-z_][A-Za-z_0-9]{0,63}(?:\.[A-Za-z_][A-Za-z_0-9]{0,63}){0,5})'\s*$")
VERSION = re.compile(r"[0-9]{1,5}(?:\.[0-9]{1,5}){0,4}(?:(?:a|b|rc|post|dev)[0-9]{1,5})?\Z")
HTTP_ACCESS = re.compile(r'[^"\r\n]{1,160} - "(GET /health|POST /api/discord/interactions) HTTP/(?:1\.[01]|2(?:\.0)?)" ([1-5][0-9]{2})(?: [A-Za-z ]{1,40})?\Z')
DURATION = re.compile(r"([0-9]{1,6}(?:\.[0-9]{1,6})?)(us|ms|s|min|h|d)")


def bounded_command(command, *, root=ROOT, timeout=15, limit=MAX_BYTES):
    """Bound both process lifetime and retained output, including stderr."""
    output = bytearray()
    flags = set()
    try:
        child = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=os.name == "posix")
    except (OSError, ValueError):
        return "COMMAND_UNAVAILABLE", b""

    def stop():
        try:
            if os.name == "posix":
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
        except ProcessLookupError:
            pass

    def read():
        try:
            while True:
                block = child.stdout.read(min(8192, limit - len(output) + 1))
                if not block:
                    return
                if len(output) + len(block) > limit:
                    flags.add("OUTPUT_LIMIT")
                    stop()
                    return
                output.extend(block)
        except (OSError, ValueError):
            flags.add("READ_FAILED")

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        flags.add("TIMEOUT")
        stop()
        child.wait(timeout=5)
    reader.join(timeout=2)
    if reader.is_alive():
        # No output from an incomplete reader is ever used.
        return "READ_FAILED", b""
    child.stdout.close()
    for status in ("TIMEOUT", "OUTPUT_LIMIT", "READ_FAILED"):
        if status in flags:
            return status, b""
    return ("OK" if child.returncode == 0 else "COMMAND_FAILED"), bytes(output)


def tracked_paths(raw):
    return {line for line in raw.decode("utf-8", errors="replace").splitlines()
            if APP_PATH.fullmatch(line)}


def event_context(record):
    """Only journal timestamps inside this exact incident window are exported."""
    timestamp = record.get("__REALTIME_TIMESTAMP")
    if not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{16}", timestamp):
        return None
    timestamp = int(timestamp)
    if not START_US <= timestamp <= END_US:
        return None
    result = {"offset_ms": (timestamp - START_US) // 1000}
    pid = record.get("_PID")
    if isinstance(pid, str) and re.fullmatch(r"[1-9][0-9]{0,9}", pid) and int(pid) <= 2_147_483_647:
        result["pid"] = int(pid)
    return result


def timeout_milliseconds(value):
    """Accept only systemd's bounded numeric duration format, never arbitrary text."""
    if not isinstance(value, str) or len(value) > 100:
        return "TIMEOUT_UNAVAILABLE"
    parts = value.split(" ")
    if not 1 <= len(parts) <= 5:
        return "TIMEOUT_UNAVAILABLE"
    scales = {"us": Decimal(".001"), "ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000, "d": 86_400_000}
    total = Decimal(0)
    for part in parts:
        match = DURATION.fullmatch(part)
        if not match:
            return "TIMEOUT_UNAVAILABLE"
        total += Decimal(match[1]) * scales[match[2]]
    return int(total) if 0 <= total <= 86_400_000 else "TIMEOUT_UNAVAILABLE"


def parse_journal(raw, source_paths):
    exceptions, frames, missing, reasons = set(), set(), set(), set()
    events = []
    if len(raw) > MAX_BYTES:
        return {"reasons": ["OUTPUT_LIMIT"], "exceptions": [], "frames": [], "missing_modules": [], "events": []}
    allowed_modules = {path[:-3].replace("/", ".") for path in source_paths}
    for entry in raw.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(entry)
        except (ValueError, TypeError, RecursionError):
            reasons.add("MALFORMED_JOURNAL_ENTRY")
            continue
        if not isinstance(record, dict) or not isinstance(record.get("MESSAGE"), str):
            reasons.add("MALFORMED_JOURNAL_ENTRY")
            continue
        units = [record[key] for key in ("_SYSTEMD_UNIT", "UNIT", "OBJECT_SYSTEMD_UNIT") if key in record]
        if units and SERVICE not in units:
            continue
        context = event_context(record)

        def add_event(kind, **fields):
            if context is None:
                return
            events.append({**context, "kind": kind, **fields})
            if len(events) > 40:
                # Keep the beginning and end, including a later rollback startup.
                del events[20]
                reasons.add("EVENT_LIMIT_REACHED")

        for line in record["MESSAGE"].splitlines():
            # Match known startup messages, never interpolate their contents.
            normalized = re.sub(r"^\s*(?:INFO|ERROR|WARNING):\s*", "", line).strip()
            if normalized == "Application startup complete.":
                reasons.add("STARTUP_COMPLETE")
                add_event("STARTUP_COMPLETE")
            if normalized == "Application startup failed. Exiting.":
                reasons.add("STARTUP_FAILED")
                add_event("STARTUP_FAILED")
            if re.search(r"\[Errno (?:98|48|10048)\].*address already in use", line, re.IGNORECASE):
                reasons.add("ADDRESS_IN_USE")
            if re.fullmatch(r"serverredus-backend\.service: (?:start|stop|start-pre|stop-sigterm) operation timed out\..{0,200}", normalized):
                reasons.add("SERVICE_TIMEOUT")
                add_event("SERVICE_TIMEOUT")
            if re.fullmatch(r"serverredus-backend\.service: Scheduled restart job, restart counter is at [0-9]+\.", normalized):
                reasons.add("SERVICE_RESTART")
                add_event("SERVICE_RESTART")
            if SERVICE in units and normalized.startswith("Stopping "):
                reasons.add("SERVICE_STOPPING")
                add_event("SERVICE_STOPPING")
            if SERVICE in units and normalized.startswith("Stopped "):
                reasons.add("SERVICE_STOPPED")
                add_event("SERVICE_STOPPED")
            access = HTTP_ACCESS.fullmatch(normalized)
            if access:
                method, route = access[1].split(" ", 1)
                add_event("HTTP_ACCESS", method=method, route=route, status=int(access[2]))
            match = EXCEPTION.match(line)
            if match and match[1] in EXCEPTIONS:
                exceptions.add(match[1])
            match = FRAME.fullmatch(line)
            if match and match[1] in source_paths and APP_PATH.fullmatch(match[1]):
                if len(frames) < 32:
                    frames.add((match[1], int(match[2])))
            match = MISSING.fullmatch(line)
            if match:
                module = match[1]
                # Unknown identifiers can be attacker-controlled secrets. Keep
                # only known package roots or an actual tracked app module.
                if module in MODULE_ROOTS or module in allowed_modules:
                    missing.add(module)
                else:
                    reasons.add("MISSING_MODULE_UNLISTED")
    if not (exceptions or frames or missing or reasons or events):
        reasons.add("NO_RECOGNIZED_DIAGNOSTICS")
    return {"reasons": sorted(reasons), "exceptions": sorted(exceptions),
            "frames": [{"path": path, "line": line} for path, line in sorted(frames)],
            "missing_modules": sorted(missing)[:16], "events": events}


def dependency_versions():
    result = {}
    for dependency in DEPENDENCIES:
        try:
            value = importlib.metadata.version(dependency)
            result[dependency] = value if VERSION.fullmatch(value) else "VERSION_UNAVAILABLE"
        except importlib.metadata.PackageNotFoundError:
            result[dependency] = "NOT_INSTALLED"
        except Exception:
            result[dependency] = "VERSION_UNAVAILABLE"
    return result


def collect():
    report = {"schema": 1, "dependencies": dependency_versions(),
              "python": ".".join(str(int(part)) for part in sys.version_info[:3])}
    status, raw = bounded_command(["git", "rev-parse", "HEAD"], limit=256)
    value = raw.decode("ascii", errors="replace").strip()
    report["head"] = value if status == "OK" and re.fullmatch(r"[0-9a-f]{40}", value) else "HEAD_UNAVAILABLE"
    status, raw = bounded_command(["git", "ls-files", "--", "app"], limit=256 * 1024)
    sources = tracked_paths(raw) if status == "OK" else set()
    report["source_inventory"] = "OK" if status == "OK" else status
    status, raw = bounded_command(["git", "ls-tree", "-r", "--name-only", FAILED_OVERLAY, "--", "app"], limit=256 * 1024)
    if status == "OK":
        sources.update(tracked_paths(raw))
    report["failed_source_inventory"] = status
    status, raw = bounded_command(["systemctl", "show", SERVICE, "--property=ActiveState,SubState,TimeoutStartUSec,TimeoutStopUSec", "--no-pager"], limit=4096)
    allowed = {"ActiveState": {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance"},
               "SubState": {"running", "dead", "failed", "exited", "start", "start-pre", "start-post", "stop", "stop-sigterm", "stop-sigkill", "auto-restart"}}
    report["service_query"] = status
    report["service"] = {}
    report["service_timeouts_ms"] = {"start": "TIMEOUT_UNAVAILABLE", "stop": "TIMEOUT_UNAVAILABLE"}
    if status == "OK":
        for line in raw.decode("utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key in allowed and value in allowed[key]:
                report["service"][key] = value
            if key in {"TimeoutStartUSec", "TimeoutStopUSec"}:
                report["service_timeouts_ms"]["start" if key == "TimeoutStartUSec" else "stop"] = timeout_milliseconds(value)
    status, raw = bounded_command(["journalctl", "-u", SERVICE, "--since", SINCE,
                                   "--until", UNTIL, "--output=json", "--no-pager", "--quiet"])
    if any(phrase in raw.lower() for phrase in (b"permission denied", b"insufficient permissions", b"not permitted")):
        report["journal_status"] = "JOURNAL_PERMISSION_DENIED"
    elif status != "OK":
        report["journal_status"] = status
    else:
        report["journal_status"] = "OK" if raw.strip() else "NO_ACCESSIBLE_ENTRIES"
        report["journal"] = parse_journal(raw, sources)
    return report


def main():
    try:
        report = collect()
    except Exception:
        # No traceback/exception text is safe to export from a production host.
        report = {"schema": 1, "diagnosis_status": "DIAGNOSTIC_FAILED"}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
