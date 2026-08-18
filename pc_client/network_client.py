from __future__ import annotations

import ipaddress
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx


_DOH_ENDPOINTS = ("https://1.1.1.1/dns-query", "https://1.0.0.1/dns-query")
_CACHE_TTL_SECONDS = 60 * 60
_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


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
    def __init__(self, overrides: dict[str, str], *, retries: int = 1) -> None:
        super().__init__(retries=retries, trust_env=False)
        # httpx does not expose httpcore's network backend in its public
        # constructor. Replacing only this pool dependency keeps normal HTTP,
        # redirect and TLS verification behaviour; TLS still receives the
        # original hostname for SNI and certificate validation.
        self._pool._network_backend = _HostOverrideBackend(overrides)  # type: ignore[attr-defined]


def create_http_client(
    url: str,
    *,
    timeout: Any,
    trust_env: bool = False,
    follow_redirects: bool = False,
) -> httpx.Client:
    """Create an HTTP client with a safe DNS fallback for the target host."""

    host = str(urlsplit(str(url or "")).hostname or "").casefold()
    if trust_env or not host or _is_ip_address(host) or host in {"localhost", "localhost.localdomain"}:
        return httpx.Client(timeout=timeout, trust_env=trust_env, follow_redirects=follow_redirects)
    address = resolve_https_ipv4(host)
    if not address:
        return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=follow_redirects)
    return httpx.Client(
        timeout=timeout,
        trust_env=False,
        follow_redirects=follow_redirects,
        transport=_ResolvedHTTPTransport({host: address}),
    )
