#!/usr/bin/env python3
"""Read-only, bounded Telegram diagnostics. Never emits secret URLs or log text."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time

ROOT = Path("/home/red/serverredus")
BASE = "822ac05d4a53dd5475312834065501b9af063b1a"
SERVICE = "serverredus-backend"
HOST = "api.telegram.org"
NETWORK_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"})


def emit(stage, **data):
    print(json.dumps({"diagnostic_only": True, "stage": stage, **data}), flush=True)


def selected_environment(raw, names):
    selected = {}
    for entry in raw.split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if separator and key in {name.encode("ascii") for name in names}:
            selected[key.decode("ascii")] = os.fsdecode(value)
    return selected


def error_fields(exc):
    result = {"error_type": type(exc).__name__}
    if type(getattr(exc, "errno", None)) is int:
        result["errno"] = exc.errno
    if type(getattr(exc, "verify_code", None)) is int:
        result["tls_verify_code"] = exc.verify_code
    return result


def capture(args, *, timeout=10, env=None):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, env=env)
    if result.returncode:
        raise RuntimeError("Private subprocess failed")
    return result.stdout


def dns(host):
    # libc resolution is not bounded by socket timeout; isolate it in a child.
    worker = '''
import json, socket, sys
values = socket.getaddrinfo(sys.argv[1], 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
print(json.dumps(sorted(set((int(v[0]), v[4][0]) for v in values))))
'''
    started = time.monotonic()
    try:
        records = json.loads(capture([sys.executable, "-B", "-c", worker, host], timeout=8))
        records = [record for record in records if record[0] in {socket.AF_INET, socket.AF_INET6}][:4]
        emit("dns", host=host, ok=bool(records), records=records, elapsed_ms=round((time.monotonic()-started)*1000))
        return records
    except Exception as exc:
        emit("dns", host=host, ok=False, **error_fields(exc))
        return []


def probe(host, family, address, *, timeout=4):
    """Test a DNS-provided address with the original hostname/SNI and full TLS verification."""
    result = {"host": host, "family": "ipv6" if family == socket.AF_INET6 else "ipv4", "address": address}
    stage = "tcp_connect"
    started = time.monotonic()
    connection = None
    try:
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        connection.connect((address, 443))
        result["tcp_ms"] = round((time.monotonic()-started)*1000)
        stage = "tls_handshake"
        started = time.monotonic()
        context = ssl.create_default_context()
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        connection = context.wrap_socket(connection, server_hostname=host)
        result.update(tls_verified=True, tls_version=connection.version(), tls_ms=round((time.monotonic()-started)*1000))
        stage = "http_write"
        connection.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nUser-Agent: XASS-network-diagnostic\r\n\r\n".encode("ascii"))
        stage = "http_read_headers"
        started = time.monotonic()
        header = b""
        while b"\r\n" not in header and len(header) < 8192:
            part = connection.recv(1024)
            if not part:
                break
            header += part
        match = re.match(rb"HTTP/1\.[01] ([1-5][0-9]{2})\b", header)
        if not match:
            raise ValueError("No valid HTTP status line")
        result.update(ok=True, http_status=int(match[1]), read_ms=round((time.monotonic()-started)*1000))
    except Exception as exc:
        result.update(ok=False, failed_stage=stage, failed_stage_ms=round((time.monotonic()-started)*1000), **error_fields(exc))
    finally:
        if connection is not None:
            connection.close()
    emit("public_transport_probe", **result)
    return result


def journal_summary(entries):
    summary = {"entries_checked": len(entries), "error_entries": 0, "telegram_related_entries": 0,
               "polling_error_entries": 0, "polling_start_entries": 0, "webhook_start_entries": 0,
               "identity_discovery_failures": 0, "identity_discovery_successes": 0,
               "telegram_http_success_entries": 0, "last_telegram_event_utc_microseconds": None}
    for entry in entries:
        message = str(entry.get("MESSAGE", ""))
        summary["error_entries"] += int(int(entry.get("PRIORITY", 6)) <= 3 or bool(re.search(r"\bERROR\b|Traceback \(most recent call last\)", message)))
        related = bool(re.search(r"telegram|api\.telegram\.org|polling|getMe", message, re.I))
        if related:
            summary["telegram_related_entries"] += 1
            timestamp = str(entry.get("__REALTIME_TIMESTAMP", ""))
            if timestamp.isdigit():
                summary["last_telegram_event_utc_microseconds"] = max(int(timestamp), summary["last_telegram_event_utc_microseconds"] or 0)
        summary["polling_error_entries"] += int("telegram_polling_loop" in message or "Polling conflict" in message)
        summary["polling_start_entries"] += int("Startup mode: polling" in message or "Polling mode enabled" in message)
        summary["webhook_start_entries"] += int("Startup mode: webhook" in message)
        summary["identity_discovery_failures"] += int("Failed to discover Telegram bot username" in message)
        summary["identity_discovery_successes"] += int("Telegram bot username discovered automatically" in message)
        summary["telegram_http_success_entries"] += int("api.telegram.org" in message and bool(re.search(r"HTTP/[12](?:\.1)? 200", message)))
    return summary


GET_ME_WORKER = '''
import asyncio, json, time
from app.config import get_settings
from app.bot_api import TelegramBotClient
events=[]
started=time.monotonic()
async def trace(name, info):
    # Event names are httpcore constants; info may contain secrets and is never emitted.
    if len(events)<32:
        events.append({"event":name,"elapsed_ms":round((time.monotonic()-started)*1000)})
async def request_hook(request):
    request.extensions["trace"] = trace
async def check():
    settings=get_settings()
    client=TelegramBotClient(settings.bot_token)
    client.client.event_hooks["request"]=[request_hook]
    result={"method":"getMe","messages_sent":0,"updates_consumed":0}
    try:
        identity=await asyncio.wait_for(client.get_me(), timeout=12)
        result["ok"]=identity.get("is_bot") is True and type(identity.get("id")) is int
    except Exception as exc:
        result.update(ok=False,error_type=type(exc).__name__)
        status=getattr(exc,"status_code",None)
        if type(status) is int:result["http_status"]=status
    finally:
        await client.close()
    result["events"]=events
    print(json.dumps(result))
asyncio.run(check())
'''


def backend_context():
    if Path.cwd() != ROOT or ROOT.resolve() != ROOT:
        raise RuntimeError("Unexpected checkout")
    sys.path.insert(0, str(ROOT))
    from app.config import get_settings
    import psutil
    show = capture(["systemctl", "show", SERVICE, "--property=MainPID", "--property=InvocationID",
                    "--property=WorkingDirectory", "--property=ActiveState", "--property=PrivateNetwork"])
    service = dict(line.split("=", 1) for line in show.decode().splitlines() if "=" in line)
    pid = service["MainPID"]
    if not pid.isdigit() or int(pid) <= 0 or service["WorkingDirectory"] != str(ROOT):
        raise RuntimeError("Invalid backend process")
    invocation = service["InvocationID"]
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise RuntimeError("Invalid invocation")
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    network = selected_environment(raw, NETWORK_KEYS)
    # Only compare/use this existing application credential in memory, never serialize it.
    credential = selected_environment(raw, {"BOT_TOKEN", "bot_token"})
    settings = get_settings()
    actual_token = credential.get("BOT_TOKEN", credential.get("bot_token", settings.bot_token))
    effective = {k: v for k, v in os.environ.items() if k not in NETWORK_KEYS and k.lower() != "bot_token"}
    effective.update(network)
    effective["BOT_TOKEN"] = actual_token
    same_namespace = os.readlink(f"/proc/{pid}/ns/net") == os.readlink("/proc/self/ns/net")
    emit("backend_context", backend_active=service["ActiveState"] == "active",
         baseline_matches=capture(["git", "rev-parse", "HEAD"]).decode().strip() == BASE,
         tracked_clean=not capture(["git", "status", "--porcelain", "--untracked-files=no"]).strip(),
         process_checkout_matches=Path(f"/proc/{pid}/cwd").resolve() == ROOT,
         same_network_namespace=same_namespace, private_network=service.get("PrivateNetwork"),
         service_network_setting_names=sorted(network),
         ssh_network_setting_names=sorted(k for k in NETWORK_KEYS if k in os.environ),
         network_environment_matches_ssh=all(os.environ.get(k) == network.get(k) for k in NETWORK_KEYS),
         process_bot_token_override_present=bool(credential), token_matches_previous_diagnostic=actual_token == settings.bot_token,
         token_configured=bool(actual_token), configured_mode="polling" if settings.use_polling else "webhook",
         env_file_modified_after_process_start=(ROOT / ".env").stat().st_mtime > psutil.Process(int(pid)).create_time())
    entries = [json.loads(line) for line in capture(["journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}", "--no-pager", "-o", "json", "-n", "2000"]).splitlines() if line.strip()]
    emit("backend_journal", **journal_summary(entries))
    return effective, int(pid), same_namespace


def main():
    public_only = sys.argv[1:] == ["--public-only"]
    if not public_only and sys.argv[1:]:
        return 2
    try:
        emit("start", location="github_runner" if public_only else "server", deployment_attempted=False)
        environment, pid, same_namespace = (dict(os.environ), None, True) if public_only else backend_context()
        # Network values stay inside the child/server process; never display them.
        for name in NETWORK_KEYS:
            os.environ.pop(name, None)
        os.environ.update({name: value for name, value in environment.items() if name in NETWORK_KEYS})
        results = [probe(HOST, family, address) for family, address in dns(HOST)]
        if not public_only:
            import psutil
            addresses = {item["address"] for item in results}
            counts = {}
            for connection in psutil.Process(pid).net_connections(kind="inet"):
                if connection.raddr and connection.raddr.ip in addresses and connection.raddr.port == 443:
                    counts[connection.status] = counts.get(connection.status, 0) + 1
            emit("backend_telegram_sockets", states=counts, note="Connections only; not proof of successful Bot API calls")
            if same_namespace and any(item.get("ok") for item in results):
                emit("get_me_trace", **json.loads(capture([sys.executable, "-B", "-c", GET_ME_WORKER], timeout=18, env=environment)))
            else:
                emit("get_me_trace", attempted=False, reason="No verified Telegram TLS/HTTP path or network namespace differs")
            # Independent HTTPS control: distinguish a general outbound failure.
            control = dns("redvps.site")
            if control:
                probe("redvps.site", *control[0])
        emit("complete", deployment_attempted=False)
        return 0
    except Exception as exc:
        emit("diagnostic_failed", **error_fields(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
