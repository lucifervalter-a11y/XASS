"""Fail-closed client for the locally installed XASS AI runtime.

The supported runtime exposes a read-only health document over HTTP. Interactive
prompts use an authenticated WebSocket session whose process-lifetime token is
not available to this desktop client. This module therefore never guesses REST
or OpenAI-compatible endpoints and never reports an unverified prompt as sent.
"""
from __future__ import annotations

import ipaddress
import json
import math
import time
from typing import Any
from urllib.parse import urlsplit

import httpx


DEFAULT_HERMES_URL = "http://127.0.0.1:9119"
HEALTH_PATH = "/api/health"
SUPPORTED_HERMES_VERSION = "0.22.2"
SUPPORTED_HERMES_COMMIT = "d923a5eb487a17c0a68801bba9ef0f56c81040a8"
MAX_HEALTH_BYTES = 64 * 1024
_HEALTH_KEYS = frozenset({"ok", "version", "displayVersion", "commit", "auth_required"})


def normalize_hermes_url(candidate: str | None) -> str:
    """Return a canonical HTTP origin for a loopback-only XASS AI daemon."""
    raw = str(candidate or "").strip()
    if not raw:
        return DEFAULT_HERMES_URL
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parts = urlsplit(raw)
        port = 9119 if parts.port is None else parts.port
    except ValueError as exc:
        raise ValueError("Некорректный локальный адрес XASS AI") from exc
    if parts.scheme.lower() != "http" or parts.username is not None or parts.password is not None:
        raise ValueError("XASS AI поддерживает только локальный HTTP-адрес")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("В адресе XASS AI нужен только локальный origin без пути")
    hostname = (parts.hostname or "").rstrip(".").casefold()
    if not hostname:
        raise ValueError("Не указан локальный адрес XASS AI")
    if hostname == "localhost":
        canonical_host = "localhost"
    else:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise ValueError("XASS AI разрешён только на loopback-адресе") from exc
        if not address.is_loopback:
            raise ValueError("XASS AI разрешён только на loopback-адресе")
        canonical_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    if not 1 <= port <= 65535:
        raise ValueError("Некорректный локальный порт XASS AI")
    return f"http://{canonical_host}:{port}"


def get_hermes_webui_url(config: dict[str, Any] | None = None) -> str:
    """Return the validated loopback URL for the XASS AI Web UI."""
    cfg = config or {}
    custom = cfg.get("hermes_webui_url") or cfg.get("hermes_url")
    return normalize_hermes_url(custom)


def _offline(
    base_url: str,
    error: str,
    *,
    code: str,
    status_code: int | None = None,
    latency_ms: float = 0.0,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "online": False,
        "ok": False,
        "status": "offline",
        "status_label": "Не в сети",
        "url": base_url,
        "endpoint": HEALTH_PATH,
        "error": error,
        "error_code": code,
        "latency_ms": round(latency_ms, 1),
    }
    if status_code is not None:
        result["status_code"] = status_code
    return result


def _validated_health(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or frozenset(payload) != _HEALTH_KEYS:
        return None
    if payload.get("ok") is not True or payload.get("version") != SUPPORTED_HERMES_VERSION:
        return None
    display_version = payload.get("displayVersion")
    commit = payload.get("commit")
    auth_required = payload.get("auth_required")
    if (
        not isinstance(display_version, str)
        or not display_version.strip()
        or not isinstance(commit, str)
        or not commit.strip()
        or commit.strip() != SUPPORTED_HERMES_COMMIT
        or len(commit) > 128
        or not isinstance(auth_required, bool)
    ):
        return None
    return {
        "version": SUPPORTED_HERMES_VERSION,
        "display_version": display_version.strip(),
        "commit": commit.strip(),
        "auth_required": auth_required,
    }


def check_hermes_status(hermes_url: str = DEFAULT_HERMES_URL, timeout: float = 2.5) -> dict[str, Any]:
    """Verify the exact XASS AI 0.22.2 health contract on loopback."""
    try:
        base_url = normalize_hermes_url(hermes_url)
    except ValueError as exc:
        return _offline(DEFAULT_HERMES_URL, str(exc), code="invalid_url")
    try:
        requested_timeout = float(timeout)
    except (TypeError, ValueError):
        requested_timeout = 2.5
    if not math.isfinite(requested_timeout):
        requested_timeout = 2.5
    requested_timeout = min(10.0, max(0.1, requested_timeout))
    started = time.perf_counter()
    body = bytearray()
    status_code = 0
    try:
        with httpx.Client(timeout=requested_timeout, trust_env=False, follow_redirects=False) as client:
            with client.stream(
                "GET",
                f"{base_url}{HEALTH_PATH}",
                headers={"Accept": "application/json"},
            ) as response:
                status_code = response.status_code
                latency_ms = (time.perf_counter() - started) * 1000
                if status_code != 200:
                    return _offline(
                        base_url,
                        f"XASS AI не прошёл проверку состояния (HTTP {status_code})",
                        code="http_status",
                        status_code=status_code,
                        latency_ms=latency_ms,
                    )
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > MAX_HEALTH_BYTES:
                        return _offline(
                            base_url,
                            "Ответ локальной службы XASS AI превышает допустимый размер",
                            code="health_too_large",
                            status_code=status_code,
                            latency_ms=latency_ms,
                        )
                    body.extend(chunk)
    except httpx.HTTPError:
        return _offline(base_url, "Локальная служба XASS AI не отвечает", code="transport_error")
    latency_ms = (time.perf_counter() - started) * 1000
    try:
        health = _validated_health(json.loads(bytes(body).decode("utf-8")))
    except (UnicodeError, ValueError, TypeError):
        health = None
    if health is None:
        return _offline(
            base_url,
            f"Обнаружена неподдерживаемая локальная служба; требуется XASS AI {SUPPORTED_HERMES_VERSION}",
            code="incompatible_health",
            status_code=status_code,
            latency_ms=latency_ms,
        )
    return {
        "online": True,
        "ok": True,
        "status": "online",
        "status_label": "В сети",
        "url": base_url,
        "endpoint": HEALTH_PATH,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 1),
        "details": health,
    }


def send_hermes_prompt(
    prompt: str,
    hermes_url: str = DEFAULT_HERMES_URL,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Fail closed until a verified WebSocket session token is available."""
    prompt_text = str(prompt or "").strip()
    try:
        base_url = normalize_hermes_url(hermes_url)
    except ValueError as exc:
        return {
            "ok": False,
            "online": False,
            "error": str(exc),
            "error_code": "invalid_url",
            "prompt": prompt_text,
            "url": DEFAULT_HERMES_URL,
        }
    if not prompt_text:
        return {
            "ok": False,
            "online": False,
            "error": "Введите запрос для XASS AI",
            "error_code": "empty_prompt",
            "prompt": "",
            "url": base_url,
        }
    try:
        prompt_timeout = float(timeout)
    except (TypeError, ValueError):
        prompt_timeout = 60.0
    if not math.isfinite(prompt_timeout):
        prompt_timeout = 60.0
    status = check_hermes_status(base_url, timeout=min(3.0, max(0.1, prompt_timeout)))
    if not status.get("online"):
        return {
            "ok": False,
            "online": False,
            "error": status.get("error") or "Локальная служба XASS AI недоступна",
            "error_code": status.get("error_code") or "service_unavailable",
            "prompt": prompt_text,
            "url": base_url,
        }
    return {
        "ok": False,
        "online": True,
        "supported": False,
        "error": (
            "XASS AI работает, но защищённый канал выполнения "
            "сейчас недоступен в этом клиенте."
        ),
        "error_code": "websocket_token_unavailable",
        "prompt": prompt_text,
        "url": base_url,
    }
