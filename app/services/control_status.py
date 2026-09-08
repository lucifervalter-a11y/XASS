"""Current operational status, kept separate from the notification inbox."""
from __future__ import annotations

import asyncio
import errno
import math
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.services.agent_updates import update_is_available

CORE_SERVICES = ("backend", "database", "telegram_bot", "public_site")


def normalize_site_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(character.isspace() or ord(character) < 32 for character in raw):
        raise ValueError("Invalid public site URL")
    if raw.startswith("//"):
        raw = "https:" + raw
    elif "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("Invalid public site URL")
    _ = parts.port  # Reject invalid/out-of-range ports before issuing a request.
    return str(httpx.URL(urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))))


def canonical_web_app_url(*candidates: str) -> str:
    for candidate in candidates:
        try:
            normalized = normalize_site_url(candidate)
        except (ValueError, httpx.InvalidURL):
            continue
        if normalized:
            parts = urlsplit(normalized)
            return urlunsplit((parts.scheme, parts.netloc, "/miniapp.php", "standalone=1", ""))
    return ""


def _probe_error(exc: BaseException) -> tuple[str, bool | None, str]:
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            return "error", False, "tls_error"
        if isinstance(cause, socket.gaierror):
            return "unknown", None, "dns_error"
        if isinstance(cause, ConnectionRefusedError) or (
            isinstance(cause, OSError) and cause.errno == errno.ECONNREFUSED
        ):
            return "offline", False, "connection_refused"
        cause = cause.__cause__ or cause.__context__
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "unknown", None, "timeout"
    if isinstance(exc, httpx.TooManyRedirects):
        return "error", False, "redirect_loop"
    return "unknown", None, "network_error"


class PublicSiteProbe:
    """Bounded, shared checks; a slow DNS lookup does not mean the site is down."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._pending: dict[str, asyncio.Task] = {}

    async def __call__(self, public_url: str) -> dict[str, Any]:
        try:
            url = normalize_site_url(public_url)
        except (ValueError, httpx.InvalidURL):
            return {"status": "invalid_config", "available": False, "url": "", "latency_ms": None, "reason": "invalid_url"}
        if not url:
            return {"status": "not_configured", "available": None, "url": "", "latency_ms": None, "reason": "not_configured"}
        cached = self._cache.get(url)
        if cached and cached[0] > time.monotonic():
            return {**cached[1], "cached": True}
        pending = self._pending.get(url)
        if pending is None:
            pending = asyncio.create_task(self._check(url))
            self._pending[url] = pending
            pending.add_done_callback(lambda task: self._pending.pop(url, None) if self._pending.get(url) is task else None)
        # Cancelling one bootstrap must not cancel another caller's shared check.
        return dict(await asyncio.shield(pending))

    async def _check(self, url: str) -> dict[str, Any]:
        started = time.monotonic()
        parts = urlsplit(url)
        result: dict[str, Any] = {
            "url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
            "checked_at": datetime.now(timezone.utc).isoformat(), "cached": False,
        }
        try:
            async with asyncio.timeout(4.0):
                async with httpx.AsyncClient(timeout=3.0, follow_redirects=True, trust_env=False) as client:
                    # Status is sufficient; do not download an arbitrary large page.
                    async with client.stream("GET", url) as response:
                        code = response.status_code
            result.update(status="online" if 200 <= code < 400 else "error", available=200 <= code < 400,
                          http_status=code, reason="" if 200 <= code < 400 else "http_error")
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            state, available, reason = _probe_error(exc)
            result.update(status=state, available=available, reason=reason, error=type(exc).__name__)
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        if len(self._cache) >= 16:
            self._cache.pop(next(iter(self._cache)))
        self._cache[url] = (time.monotonic() + (15 if result["available"] is True else 5), dict(result))
        return result


public_site_status = PublicSiteProbe()


def source_is_online(source: Any, timeout_minutes: int, *, now: datetime | None = None) -> bool:
    if not source.is_online or source.last_seen_at is None:
        return False
    last_seen = source.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - last_seen).total_seconds()
    return -60 <= age <= max(1, int(timeout_minutes)) * 60


def agent_attention(payload: dict[str, Any], *, is_online: bool, latest_version: str) -> tuple[list[str], bool]:
    reasons: list[str] = []
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    archive = payload.get("archive_status") if isinstance(payload.get("archive_status"), dict) else {}
    version = str(payload.get("agent_version") or "0.0.0")
    requires_update = bool(latest_version and latest_version != "0.0.0" and update_is_available(
        current_version=version, current_revision="", published_version=latest_version, published_revision=""
    ))
    if not is_online:
        reasons.append("offline")
    # Old telemetry describes the last session, not current CPU/RAM/disk pressure.
    if is_online:
        for key, threshold, label in (("cpu_percent", 95, "high_cpu"), ("ram_used_percent", 95, "high_ram"), ("disk_used_percent", 92, "low_disk")):
            try:
                if float(metrics.get(key) or 0) >= threshold:
                    reasons.append(label)
            except (TypeError, ValueError):
                continue
        if str(payload.get("last_error") or "").strip():
            reasons.append("agent_error")
        if str(archive.get("last_error") or "").strip():
            reasons.append("archive_error")
    if requires_update:
        reasons.append("update_available")
    return reasons, requires_update


def summarize_control_status(
    system_status: dict[str, Any], sources: list[dict[str, Any]], *,
    metrics: dict[str, Any] | None = None, review_groups: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    checks = [system_status.get(key) or {} for key in CORE_SERVICES]
    healthy = sum(check.get("available") is True for check in checks)
    failed = sum(check.get("available") is False for check in checks)
    unknown = len(checks) - healthy - failed
    health = {
        "status": "degraded" if failed else "unknown" if unknown else "healthy",
        "score": None if unknown else round(healthy / len(checks) * 100),
        "healthy_services": healthy, "checked_services": healthy + failed,
        "unknown_services": unknown, "total_services": len(checks),
    }
    issues: list[dict[str, Any]] = []
    for key in CORE_SERVICES:
        check = system_status.get(key) or {}
        if check.get("available") is False:
            reason_code = str(check.get("reason") or check.get("status") or "unavailable")
            reason = {
                "http_error": f"Сайт вернул HTTP {check.get('http_status') or 'ошибку'}",
                "invalid_url": "Проверьте адрес сайта",
                "not_configured": "Сервис не настроен",
                "connection_refused": "Сайт отклонил соединение",
                "tls_error": "Ошибка сертификата HTTPS",
                "redirect_loop": "Цикл перенаправлений сайта",
            }.get(reason_code, reason_code)
            issues.append({"key": f"service:{key}", "category": "service", "severity": "critical",
                           "reason": reason, "reason_code": reason_code, "service": key})
    affected = updates = 0
    for source in sources:
        updates += bool(source.get("requires_update"))
        reasons = list(dict.fromkeys(str(reason) for reason in source.get("attention_reasons", []) if reason != "update_available"))
        if reasons:
            affected += 1
            name = str(source.get("source_name") or "")
            issues.append({"key": f"device:{name}", "category": "device", "severity": "warning",
                           "reason": reasons[0], "reasons": reasons, "source_name": name})
    resource_reasons = []
    for key, threshold, reason in (("cpu_percent", 95, "high_cpu"), ("ram_used_percent", 95, "high_ram"), ("disk_used_percent", 92, "low_disk")):
        try:
            value = float((metrics or {}).get(key) or 0)
            if math.isfinite(value) and value >= threshold:
                resource_reasons.append(reason)
        except (TypeError, ValueError):
            continue
    if resource_reasons:
        issues.append({"key": "resource:server", "category": "resource", "severity": "warning",
                       "reason": resource_reasons[0], "reasons": resource_reasons})
    attention = {"active_count": len(issues), "active_issues": issues, "affected_sources": affected,
                 "update_count": updates, "review_count": len(review_groups or []), "review_groups": review_groups or []}
    return health, attention
