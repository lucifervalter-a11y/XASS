#!/usr/bin/env python3
"""Read-only, bounded Telegram diagnostics. Never emits secret URLs or log text."""
from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

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


def journal_summary(entries, now=None):
    cutoff = int(((time.time() if now is None else now) - 1800) * 1000000)
    summary = {"entries_checked": len(entries), "error_entries": 0, "telegram_related_entries": 0,
               "polling_error_entries": 0, "polling_start_entries": 0, "webhook_start_entries": 0,
               "identity_discovery_failures": 0, "identity_discovery_successes": 0,
               "telegram_http_success_entries": 0, "last_telegram_event_utc_microseconds": None,
               "telegram_transport_errors": 0, "telegram_conflicts_409": 0,
               "telegram_transport_errors_last_30_minutes": 0, "telegram_conflicts_last_30_minutes": 0,
               "observed_journal_first_utc_microseconds": None, "observed_journal_last_utc_microseconds": None}
    for entry in entries:
        message = str(entry.get("MESSAGE", ""))
        timestamp = str(entry.get("__REALTIME_TIMESTAMP", ""))
        timestamp = int(timestamp) if timestamp.isdigit() else 0
        if timestamp:
            summary["observed_journal_first_utc_microseconds"] = min(timestamp, summary["observed_journal_first_utc_microseconds"] or timestamp)
            summary["observed_journal_last_utc_microseconds"] = max(timestamp, summary["observed_journal_last_utc_microseconds"] or 0)
        summary["error_entries"] += int(int(entry.get("PRIORITY", 6)) <= 3 or bool(re.search(r"\bERROR\b|Traceback \(most recent call last\)", message)))
        related = bool(re.search(r"telegram|api\.telegram\.org|polling|getMe", message, re.I))
        if related:
            summary["telegram_related_entries"] += 1
            if timestamp:
                summary["last_telegram_event_utc_microseconds"] = max(timestamp, summary["last_telegram_event_utc_microseconds"] or 0)
        transport = related and bool(re.search(r"Telegram API request failed|ConnectTimeout|ConnectError|ReadTimeout|ReadError|NetworkError", message))
        conflict = related and ("Polling conflict (409)" in message or "http=409" in message)
        summary["telegram_transport_errors"] += int(transport)
        summary["telegram_conflicts_409"] += int(conflict)
        summary["telegram_transport_errors_last_30_minutes"] += int(transport and timestamp >= cutoff)
        summary["telegram_conflicts_last_30_minutes"] += int(conflict and timestamp >= cutoff)
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


def check_health():
    from dotenv import dotenv_values
    port = str(dotenv_values(ROOT / ".env").get("PORT") or "8000")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise RuntimeError("Invalid port")
    for label, url in [("local", f"http://127.0.0.1:{port}/health"), ("public", "https://redvps.site/health")]:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                data = json.loads(response.read(65536))
                emit("backend_health", location=label, http_status=response.status,
                     ok=response.status == 200 and data.get("status") == "ok")
        except Exception as exc:
            emit("backend_health", location=label, ok=False, **error_fields(exc))


def policy_read(arguments, *, privileged=False):
    """Run fixed read commands, using sudo only if already permitted for that exact read."""
    executable = shutil.which(arguments[0])
    if not executable:
        return None, "not_installed"
    command = [executable, *arguments[1:]]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=12)
        permission_hint = any(hint in result.stderr.lower() for hint in (b"permission denied", b"not seeing messages", b"no journal files"))
        if (result.returncode or permission_hint) and privileged and os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if not sudo:
                return None, "read_access_unavailable"
            permission = subprocess.run([sudo, "-n", "-l", *command], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            if permission.returncode:
                return None, "existing_sudo_permission_unavailable"
            result = subprocess.run([sudo, "-n", *command], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
        if result.returncode:
            return None, "read_failed"
        if len(result.stdout) > 4 * 1024 * 1024:
            return None, "output_exceeds_private_parse_limit"
        return result.stdout, "ok"
    except Exception as exc:
        return None, type(exc).__name__


def summarize_iptables(raw):
    """Keep rule mechanics/counters, excluding comments, log prefixes and payload strings."""
    tables = []
    current = None
    arity = {k: 1 for k in ["-A", "-p", "-s", "-d", "-j", "-g", "-i", "-o", "-m",
        "--dport", "--sport", "--dports", "--sports", "--ctstate", "--state", "--uid-owner",
        "--gid-owner", "--mark", "--set-mark", "--set-xmark", "--reject-with", "--to-destination",
        "--to-source", "--match-set", "--set", "--tcp-option", "--ctdir", "--ctstatus"]}
    arity["--tcp-flags"] = 2
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith("*"):
            current = {"table": line[1:], "chains": [], "rules": []}
            tables.append(current)
        elif current is not None and line.startswith(":"):
            parts = line[1:].split()
            current["chains"].append({"chain": parts[0], "policy": parts[1], "counters": parts[2] if len(parts)>2 else None})
        elif current is not None and (line.startswith("-A ") or re.match(r"\[\d+:\d+\] -A ", line)):
            tokens = shlex.split(line)
            clean, omitted = [], []
            index = 0
            if tokens and re.fullmatch(r"\[\d+:\d+\]", tokens[0]):
                clean.append(tokens[0]); index = 1
            while index < len(tokens):
                token = tokens[index]
                if token == "!":
                    clean.append(token); index += 1
                elif token in arity:
                    count = arity[token]
                    clean.extend(tokens[index:index+count+1]); index += count+1
                elif token in {"--syn", "--random", "--random-fully", "--notrack"}:
                    clean.append(token); index += 1
                else:
                    if token.startswith("-"):
                        omitted.append(token)
                    index += 1
            current["rules"].append({"mechanics": clean, "omitted_options": omitted})
    return tables


def nft_summary(value):
    """Summarize hooks/verdicts/sets without emitting arbitrary rule payloads."""
    result = {"tables": [], "chains": [], "rules": [], "sets": []}
    for item in value.get("nftables", []):
        if "table" in item:
            result["tables"].append({key: item["table"].get(key) for key in ("family", "name")})
        if "chain" in item:
            result["chains"].append({key: item["chain"].get(key) for key in ("family", "table", "name", "type", "hook", "prio", "policy")})
        if "rule" in item:
            rule = item["rule"]
            verdicts, counters, expression_types = [], [], []
            for expression in rule.get("expr", []):
                expression_types.extend(expression.keys())
                for key in ("accept", "drop", "reject", "return", "jump", "goto", "dnat", "snat", "redirect"):
                    if key in expression:
                        detail = expression[key]
                        verdicts.append({"kind": key, "target": detail.get("target") if isinstance(detail, dict) else None})
                if "counter" in expression:
                    counters.append(expression["counter"])
            result["rules"].append({"family": rule.get("family"), "table": rule.get("table"), "chain": rule.get("chain"),
                "handle": rule.get("handle"), "expression_types": expression_types, "verdicts": verdicts, "counters": counters,
                "matches_withheld": any("match" in expression for expression in rule.get("expr", []))})
        if "set" in item:
            definition = item["set"]
            result["sets"].append({key: definition.get(key) for key in ("family", "table", "name", "type")})
    return result


def kernel_filter_summary(entries, destination):
    target, drops, route_faults = 0, 0, 0
    for entry in entries:
        message = str(entry.get("MESSAGE", ""))
        matching = f"DST={destination}" in message and "DPT=443" in message
        target += int(matching)
        drops += int(matching and bool(re.search(r"BLOCK|DROP|REJECT|DENY", message, re.I)))
        route_faults += int(bool(re.search(r"martian source|NETDEV WATCHDOG|link is down|unreachable", message, re.I)))
    return {"entries_checked": len(entries), "target_tcp443_entries": target,
            "target_block_entries": drops, "network_fault_entries": route_faults}


def egress_policy_checks(pid):
    target = "149.154.166.110"
    uid = os.stat(f"/proc/{pid}").st_uid
    route_device = None
    for label, address in [("telegram", target), ("github_control", "140.82.121.3")]:
        raw, access = policy_read(["ip", "-j", "-4", "route", "get", address, "uid", str(uid), "ipproto", "tcp", "dport", "443"])
        routes = json.loads(raw) if raw is not None else []
        if label == "telegram" and routes:
            route_device = routes[0].get("dev")
        emit("route_lookup", target=label, access=access, routes=routes)
    raw, access = policy_read(["ip", "-j", "-4", "rule", "show"])
    emit("policy_routing_rules", access=access, rules=json.loads(raw) if raw is not None else [])
    raw, access = policy_read(["ip", "-j", "-4", "route", "show", "table", "all"])
    routes = json.loads(raw) if raw is not None else []
    relevant = []
    for route in routes:
        destination = route.get("dst", "default")
        try:
            match = destination == "default" or ipaddress.ip_address(target) in ipaddress.ip_network(destination, strict=False)
        except ValueError:
            match = False
        if match:
            relevant.append(route)
    emit("routes_covering_telegram", access=access, routes=relevant)
    for tool in ("iptables-save", "ip6tables-save"):
        raw, access = policy_read([tool, "-c"], privileged=True)
        emit("packet_filter", backend=tool, access=access, tables=summarize_iptables(raw) if raw is not None else [])
    raw, access = policy_read(["nft", "-j", "list", "ruleset"], privileged=True)
    emit("nftables", access=access, summary=nft_summary(json.loads(raw)) if raw is not None else None)
    # Legacy and nft backends may coexist. Read both explicitly if installed.
    for tool in ("iptables-legacy-save", "iptables-nft-save"):
        raw, access = policy_read([tool, "-c"], privileged=True)
        emit("packet_filter", backend=tool, access=access, tables=summarize_iptables(raw) if raw is not None else [])
    if route_device and re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", route_device):
        for direction in ("ingress", "egress"):
            raw, access = policy_read(["tc", "-j", "filter", "show", "dev", route_device, direction], privileged=True)
            filters = json.loads(raw) if raw is not None else []
            emit("traffic_control_filters", direction=direction, access=access, count=len(filters), kinds=[item.get("kind") for item in filters])
    raw, access = policy_read(["systemctl", "show", SERVICE, "--property=IPAddressDeny", "--property=IPAddressAllow", "--property=RestrictAddressFamilies", "--property=PrivateNetwork"])
    restrictions = dict(line.split("=", 1) for line in raw.decode().splitlines() if "=" in line) if raw is not None else {}
    emit("service_network_restrictions", access=access, properties=restrictions)
    raw, access = policy_read(["journalctl", "-k", "--since=-30min", "--no-pager", "-o", "json", "-n", "2000"], privileged=True)
    entries = [json.loads(line) for line in raw.splitlines() if line.strip()] if raw is not None else []
    emit("kernel_filter_log", access=access, **kernel_filter_summary(entries, target))
    yc = shutil.which("yc")
    if not yc:
        candidate = Path.home() / "yandex-cloud/bin/yc"
        yc = str(candidate) if candidate.is_file() else None
    if yc:
        raw, access = policy_read([yc, "config", "profile", "list"])
        profiles = raw.decode().splitlines() if raw is not None else []
        emit("yandex_cli_existing_profiles", installed=True, access=access, nonempty_lines=len([line for line in profiles if line.strip()]), metadata_accessed=False)
    else:
        emit("yandex_cli_existing_profiles", installed=False, metadata_accessed=False)
    check_health()


def main():
    public_only = sys.argv[1:] == ["--public-only"]
    logs_and_control = sys.argv[1:] == ["--logs-and-control-only"]
    policy_only = sys.argv[1:] == ["--egress-policy-only"]
    if not public_only and not logs_and_control and not policy_only and sys.argv[1:]:
        return 2
    try:
        emit("start", location="github_runner" if public_only else "server", deployment_attempted=False)
        if policy_only:
            if Path.cwd() != ROOT or ROOT.resolve() != ROOT:
                raise RuntimeError("Unexpected checkout")
            show = capture(["systemctl", "show", SERVICE, "--property=MainPID", "--property=ActiveState", "--property=WorkingDirectory"])
            service = dict(line.split("=", 1) for line in show.decode().splitlines() if "=" in line)
            pid = service["MainPID"]
            if not pid.isdigit() or int(pid) <= 0 or service["WorkingDirectory"] != str(ROOT):
                raise RuntimeError("Invalid backend process")
            emit("egress_backend_context", backend_active=service["ActiveState"] == "active",
                 baseline_matches=capture(["git", "rev-parse", "HEAD"]).decode().strip() == BASE,
                 tracked_clean=not capture(["git", "status", "--porcelain", "--untracked-files=no"]).strip(),
                 process_checkout_matches=Path(f"/proc/{pid}/cwd").resolve() == ROOT,
                 credential_environment_read=False, metadata_accessed=False)
            egress_policy_checks(int(pid))
            emit("complete", deployment_attempted=False, telegram_connections_attempted=0, metadata_accessed=False)
            return 0
        environment, pid, same_namespace = (dict(os.environ), None, True) if public_only else backend_context()
        # Network values stay inside the child/server process; never display them.
        for name in NETWORK_KEYS:
            os.environ.pop(name, None)
        os.environ.update({name: value for name, value in environment.items() if name in NETWORK_KEYS})
        records = dns(HOST)
        results = [] if logs_and_control else [probe(HOST, family, address) for family, address in records]
        if not public_only:
            import psutil
            addresses = {item[1] for item in records}
            counts = {}
            for connection in psutil.Process(pid).net_connections(kind="inet"):
                if connection.raddr and connection.raddr.ip in addresses and connection.raddr.port == 443:
                    counts[connection.status] = counts.get(connection.status, 0) + 1
            emit("backend_telegram_sockets", states=counts, note="Connections only; not proof of successful Bot API calls")
            if logs_and_control:
                emit("get_me_trace", attempted=False, reason="Follow-up reads logs and socket state only; no repeated Telegram connection attempt")
            elif same_namespace and any(item.get("ok") for item in results):
                emit("get_me_trace", **json.loads(capture([sys.executable, "-B", "-c", GET_ME_WORKER], timeout=18, env=environment)))
            else:
                emit("get_me_trace", attempted=False, reason="No verified Telegram TLS/HTTP path or network namespace differs")
            # Independent HTTPS control: distinguish a general outbound failure.
            control_host = "github.com" if logs_and_control else "redvps.site"
            control = dns(control_host)
            if control:
                probe(control_host, *control[0])
            check_health()
        emit("complete", deployment_attempted=False)
        return 0
    except Exception as exc:
        emit("diagnostic_failed", **error_fields(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
