from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx
import psutil


_DOH_ENDPOINTS = ("https://1.1.1.1/dns-query", "https://1.0.0.1/dns-query")
_CACHE_TTL_SECONDS = 60 * 60
_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()
_preference_lock = threading.Lock()
_preferred_local_address = ""
_preferred_signature: tuple[tuple[str, str, bool], ...] = ()
_preferred_origin: tuple[str, str, int] = ("", "", 0)
_preferred_system_dns = False
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_VIRTUAL_ADAPTER_MARKERS = (
    "vpn", "tunnel", "tun", "tap", "wintun", "wireguard", "happ",
    "virtual", "vethernet", "hyper-v", "default switch", "wsl", "docker",
    "container", "vmware", "vbox", "tailscale", "zerotier", "hamachi",
    "openvpn", "proton", "warp", "loopback", "bluetooth",
)


def require_secure_transport(url: str, *, allow_insecure_http: bool = False) -> str:
    """Validate an endpoint before any XASS credential is attached."""

    value = str(url or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Адрес XASS должен быть HTTP(S) URL без логина и пароля")
    if parsed.scheme.casefold() == "https":
        return value
    env = os.getenv("XASS_ALLOW_INSECURE_HTTP", "").strip().casefold()
    allow = bool(allow_insecure_http) or env in {"1", "true", "yes", "on"}
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.casefold() in {"localhost", "localhost.localdomain"}
    if loopback or allow:
        return value
    raise RuntimeError(
        "XASS не передаёт код привязки или ключ агента по HTTP. "
        "Укажите HTTPS; для изолированной разработки явно включите --allow-insecure-http "
        "или XASS_ALLOW_INSECURE_HTTP=1"
    )


def _is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def resolve_https_ipv4(host: str, *, timeout: float = 4.0) -> str:
    """Resolve a public hostname over verified HTTPS without using system DNS.

    Cloudflare's IP endpoint has a certificate valid for the IP itself. The
    returned address is validated before it can be used by the transport.
    Normal DNS remains the fallback for local/private names and when DoH is not
    reachable.
    """

    normalized = str(host or "").strip().rstrip(".").casefold()
    if not normalized or normalized in {"localhost", "localhost.localdomain"}:
        return ""
    if _is_ip_address(normalized):
        return normalized

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(normalized)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

    for endpoint in _DOH_ENDPOINTS:
        try:
            response = httpx.get(
                endpoint,
                params={"name": normalized, "type": "A"},
                headers={"Accept": "application/dns-json", "Host": "cloudflare-dns.com"},
                timeout=timeout,
                trust_env=False,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            continue
        answers = payload.get("Answer") if isinstance(payload, dict) else None
        if not isinstance(answers, list):
            continue
        for answer in answers:
            if not isinstance(answer, dict) or int(answer.get("type") or 0) != 1:
                continue
            value = str(answer.get("data") or "").strip()
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.version != 4:
                continue
            with _cache_lock:
                _cache[normalized] = (now, value)
            return value
    return ""


def invalidate_network_state(host: str = "") -> None:
    """Forget DNS fallbacks after Windows changes route, VPN or adapter.

    A DoH answer is deliberately cached because a few installations use XASS
    behind a resolver that intermittently returns SERVFAIL.  The same cached
    address must not, however, pin a running agent to the route that existed
    before a VPN or Wi-Fi change.  The heartbeat loop calls this after a
    transport failure and creates a fresh connection pool.
    """

    normalized = str(host or "").strip().rstrip(".").casefold()
    with _cache_lock:
        if normalized:
            _cache.pop(normalized, None)
        else:
            _cache.clear()


def _private_physical_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value or ""))
    except ValueError:
        return False
    return address.version == 4 and any(address in network for network in _RFC1918)


def network_signature() -> tuple[tuple[str, str, bool], ...]:
    """Bounded adapter fingerprint used only to discard stale pools."""

    try:
        stats = psutil.net_if_stats()
        rows = []
        for name, addresses in psutil.net_if_addrs().items():
            up = bool(stats.get(name) and stats[name].isup)
            for item in addresses:
                if item.family == socket.AF_INET:
                    rows.append((str(name).casefold()[:128], str(item.address)[:64], up))
        return tuple(sorted(rows))
    except Exception:
        return ()


def physical_ipv4_candidates() -> tuple[str, ...]:
    """Return active RFC1918 addresses, excluding known tunnels/virtual NICs."""

    try:
        stats = psutil.net_if_stats()
        rows: list[tuple[int, str, str]] = []
        for name, addresses in psutil.net_if_addrs().items():
            folded = str(name).casefold()
            if any(marker in folded for marker in _VIRTUAL_ADAPTER_MARKERS):
                continue
            state = stats.get(name)
            if state is None or not state.isup:
                continue
            priority = 0 if any(marker in folded for marker in ("wi-fi", "wifi", "wlan", "ethernet", "беспровод")) else 1
            for item in addresses:
                value = str(item.address or "")
                if item.family == socket.AF_INET and _private_physical_ipv4(value):
                    rows.append((priority, folded, value))
        return tuple(dict.fromkeys(value for _priority, _name, value in sorted(rows)))
    except Exception:
        return ()


def _probe_https_candidate(url: str, local_address: str, *, resolved_address: str = "",
                           timeout: float = 2.5) -> bool:
    """Verify one source NIC against the real HTTPS origin.

    The URL keeps the configured hostname, so httpx performs normal certificate
    validation and sends that hostname as TLS SNI. The probe can use either the
    verified DoH IPv4 already used by XASS or normal system DNS; neither mode
    rewrites the URL or weakens TLS.
    """
    parsed = urlsplit(str(url or ""))
    host = str(parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or not host or not _private_physical_ipv4(local_address):
        return False
    port = parsed.port
    netloc = host if port in (None, 443) else f"{host}:{port}"
    probe_url = urlunsplit(("https", netloc, "/", "", ""))
    address = str(resolved_address or "").strip()
    transport = (_ResolvedHTTPTransport({host: address}, retries=0, local_address=local_address)
                 if address else httpx.HTTPTransport(retries=0, trust_env=False,
                                                     local_address=local_address))
    try:
        with httpx.Client(transport=transport, timeout=timeout, trust_env=False,
                          follow_redirects=False) as client:
            # Any syntactically valid HTTP response proves TCP + TLS + SNI. A
            # 401/404/405 is enough; the probe never needs credentials or body.
            with client.stream("HEAD", probe_url, headers={"Connection": "close"}) as response:
                _ = response.status_code
        return True
    except (httpx.HTTPError, OSError, ValueError):
        return False


def activate_physical_fallback(url: str, *, trust_env: bool = False) -> str:
    """Prefer one verified physical NIC for future XASS clients only."""

    global _preferred_local_address, _preferred_signature, _preferred_origin, _preferred_system_dns
    parsed = urlsplit(str(url or ""))
    if trust_env or parsed.scheme.casefold() != "https" or not parsed.hostname:
        return ""
    invalidate_network_state(str(parsed.hostname))
    resolved = resolve_https_ipv4(str(parsed.hostname))
    selected, system_dns = "", False
    for candidate in physical_ipv4_candidates():
        # Prefer the same verified DoH answer used by the ordinary XASS
        # transport. This survives a local resolver returning SERVFAIL while
        # preserving the configured hostname for SNI/certificate validation.
        if resolved and _probe_https_candidate(url, candidate, resolved_address=resolved):
            selected = candidate
            break
        if _probe_https_candidate(url, candidate):
            selected, system_dns = candidate, True
            break
    with _preference_lock:
        _preferred_local_address = selected
        _preferred_signature = network_signature()
        _preferred_origin = _origin(url) if selected else ("", "", 0)
        _preferred_system_dns = system_dns if selected else False
    return selected


def preferred_connection(url: str, *, trust_env: bool = False) -> tuple[bool, str]:
    """Return shared DNS/bind preference, refreshing it after adapter changes."""

    global _preferred_local_address, _preferred_signature, _preferred_origin, _preferred_system_dns
    parsed = urlsplit(str(url or ""))
    if trust_env or parsed.scheme.casefold() != "https":
        return False, ""
    signature = network_signature()
    with _preference_lock:
        if _preferred_local_address and (signature and signature != _preferred_signature):
            # A previously verified address says nothing about the replacement
            # adapter. Fall back to an unbound pool until a real failed request
            # triggers a fresh origin probe.
            _preferred_local_address = ""
            _preferred_signature = signature
            _preferred_origin = ("", "", 0)
            _preferred_system_dns = False
        selected = _preferred_local_address if _preferred_origin == _origin(url) else ""
        system_dns = _preferred_system_dns if selected else False
    return system_dns, selected


class _HostOverrideBackend(httpcore.NetworkBackend):
    def __init__(self, overrides: dict[str, str]) -> None:
        self._overrides = {str(host).casefold(): str(address) for host, address in overrides.items()}
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        target = self._overrides.get(str(host).casefold(), host)
        return self._backend.connect_tcp(
            target,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        return self._backend.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _ResolvedHTTPTransport(httpx.HTTPTransport):
    def __init__(self, overrides: dict[str, str], *, retries: int = 1, local_address: str = "") -> None:
        super().__init__(retries=retries, trust_env=False, local_address=local_address or None)
        # httpx does not expose httpcore's network backend in its public
        # constructor. Replacing only this pool dependency keeps normal HTTP,
        # redirect and TLS verification behaviour; TLS still receives the
        # original hostname for SNI and certificate validation.
        self._pool._network_backend = _HostOverrideBackend(overrides)  # type: ignore[attr-defined]


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(str(value or ""))
    scheme = parsed.scheme.casefold()
    return scheme, str(parsed.hostname or "").casefold(), parsed.port or (443 if scheme == "https" else 80)


class _AdaptiveTransport(httpx.BaseTransport):
    """Keep TLS intact while surviving a Windows route/VPN transition."""

    def __init__(
        self,
        url: str,
        *,
        resolved_address: str = "",
        prefer_system_dns: bool = False,
        local_address: str = "",
        pinned: bool = False,
    ) -> None:
        self.url = str(url)
        self.origin = _origin(url)
        self.resolved_address = str(resolved_address or "")
        self.prefer_system_dns = bool(prefer_system_dns)
        self.local_address = str(local_address or "")
        self.pinned = bool(pinned)
        self._signature = network_signature()
        self._lock = threading.Lock()
        self._transport = self._build()

    def _build(self) -> httpx.BaseTransport:
        if self.prefer_system_dns or not self.resolved_address:
            return httpx.HTTPTransport(
                retries=1, trust_env=False, local_address=self.local_address or None,
            )
        return _ResolvedHTTPTransport(
            {self.origin[1]: self.resolved_address}, local_address=self.local_address,
        )

    def _replace(self, *, prefer_system_dns: bool, local_address: str, force: bool = False) -> None:
        with self._lock:
            if not force and (self.prefer_system_dns, self.local_address) == (prefer_system_dns, local_address):
                return
            old = self._transport
            self.prefer_system_dns, self.local_address = prefer_system_dns, local_address
            self._transport = self._build()
            old.close()

    def _refresh_shared_preference(self) -> None:
        signature = network_signature()
        changed = bool(signature and signature != self._signature)
        if signature:
            self._signature = signature
        if self.pinned:
            # Even an explicitly selected route needs a fresh connection pool
            # after Windows replaces an adapter.  Keep the caller's verified
            # bind choice; _HeartbeatClient owns re-selection for pinned binds.
            if changed:
                self._replace(
                    prefer_system_dns=self.prefer_system_dns,
                    local_address=self.local_address,
                    force=True,
                )
            return
        prefer, bind = preferred_connection(self.url, trust_env=False)
        if bind and not prefer:
            # activate_physical_fallback refreshed the DoH cache while proving
            # this NIC. Rebuild with that exact verified destination address.
            self.resolved_address = resolve_https_ipv4(self.origin[1])
        self._replace(prefer_system_dns=prefer, local_address=bind, force=changed)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._refresh_shared_preference()
        transport = self._transport
        try:
            return transport.handle_request(request)
        except httpx.TransportError:
            # Never rewrite the target URL: SNI and certificate checks continue
            # to use the configured hostname.  Only this client's source socket
            # changes, and only to a verified active physical RFC1918 address.
            if _origin(str(request.url)) != self.origin:
                raise
            bind = activate_physical_fallback(self.url, trust_env=False)
            if not bind:
                raise
            prefer_system_dns, bind = preferred_connection(self.url, trust_env=False)
            if not prefer_system_dns:
                self.resolved_address = resolve_https_ipv4(self.origin[1])
            self.pinned = False
            self._replace(prefer_system_dns=prefer_system_dns, local_address=bind)
            if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                raise
            return self._transport.handle_request(request)

    def close(self) -> None:
        self._transport.close()


def create_http_client(
    url: str,
    *,
    timeout: Any,
    trust_env: bool = False,
    follow_redirects: bool = False,
    prefer_system_dns: bool = False,
    local_address: str = "",
) -> httpx.Client:
    """Create an HTTP client with a safe DNS fallback for the target host."""

    host = str(urlsplit(str(url or "")).hostname or "").casefold()
    pinned = bool(local_address or prefer_system_dns)
    if not local_address and not prefer_system_dns:
        preferred_dns, preferred_bind = preferred_connection(url, trust_env=trust_env)
        prefer_system_dns, local_address = preferred_dns, preferred_bind
    bind = str(local_address or "").strip()
    if bind and (trust_env or not _private_physical_ipv4(bind) or bind not in physical_ipv4_candidates()):
        raise ValueError("local_address must be an active private physical IPv4 address")
    if trust_env or not host or _is_ip_address(host) or host in {"localhost", "localhost.localdomain"}:
        options = {}
        if bind:
            options["transport"] = httpx.HTTPTransport(retries=1, trust_env=False, local_address=bind)
        return httpx.Client(timeout=timeout, trust_env=trust_env, follow_redirects=follow_redirects, **options)
    address = "" if prefer_system_dns else resolve_https_ipv4(host)
    return httpx.Client(
        timeout=timeout,
        trust_env=False,
        follow_redirects=follow_redirects,
        transport=_AdaptiveTransport(
            url,
            resolved_address=address,
            prefer_system_dns=prefer_system_dns,
            local_address=bind,
            pinned=pinned,
        ),
    )
