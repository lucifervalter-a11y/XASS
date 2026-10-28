"""Client library for local Hermes Agent AI integration.

Communicates with the local Hermes daemon (default: http://127.0.0.1:8787).
Supports health/status checks, prompt and task dispatch, and WebUI navigation.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import httpx

DEFAULT_HERMES_URL = "http://127.0.0.1:8787"


def normalize_hermes_url(candidate: str | None) -> str:
    """Normalize and validate local/remote Hermes instance URL."""
    raw = str(candidate or "").strip()
    if not raw:
        return DEFAULT_HERMES_URL
    if not (raw.startswith("http://") or raw.startswith("https://")):
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    scheme = parts.scheme.lower() or "http"
    netloc = parts.netloc.rstrip("/")
    if not netloc:
        return DEFAULT_HERMES_URL
    return f"{scheme}://{netloc}"


def get_hermes_webui_url(config: dict[str, Any] | None = None) -> str:
    """Return URL for the Hermes WebUI interface."""
    cfg = config or {}
    custom = cfg.get("hermes_webui_url") or cfg.get("hermes_url")
    return normalize_hermes_url(custom)


def check_hermes_status(hermes_url: str = DEFAULT_HERMES_URL, timeout: float = 2.5) -> dict[str, Any]:
    """Check connectivity and state of the local Hermes daemon."""
    base_url = normalize_hermes_url(hermes_url)
    started = time.perf_counter()

    # Try common health check endpoints
    probe_paths = ("/health", "/status", "/api/status", "/")
    last_error: str | None = None

    for path in probe_paths:
        target = f"{base_url}{path}"
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                response = client.get(target, headers={"Accept": "application/json, text/plain, */*"})
                latency_ms = (time.perf_counter() - started) * 1000

                # Any 2xx or 3xx or 4xx (except transport failure) indicates the HTTP service is alive
                if response.status_code < 500:
                    details: dict[str, Any] = {}
                    try:
                        parsed = response.json()
                        if isinstance(parsed, dict):
                            details = parsed
                    except Exception:
                        details = {"content_type": response.headers.get("content-type", "")}

                    return {
                        "online": True,
                        "status": "online",
                        "status_label": "В сети",
                        "url": base_url,
                        "endpoint": path,
                        "status_code": response.status_code,
                        "latency_ms": round(latency_ms, 1),
                        "details": details,
                    }
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = f"Соединение отклонено: {exc}"
            # No point in probing other paths if the port itself is not reachable
            break
        except httpx.HTTPError as exc:
            last_error = str(exc)
            continue
        except Exception as exc:
            last_error = str(exc)
            break

    return {
        "online": False,
        "status": "offline",
        "status_label": "Не в сети",
        "url": base_url,
        "error": last_error or "Служба не отвечает",
        "latency_ms": 0.0,
    }


def send_hermes_prompt(
    prompt: str,
    hermes_url: str = DEFAULT_HERMES_URL,
    timeout: float = 60.0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Forward a prompt or task to the local Hermes daemon and return response."""
    base_url = normalize_hermes_url(hermes_url)
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return {
            "ok": False,
            "online": True,
            "error": "Запрос не может быть пустым",
            "prompt": "",
            "url": base_url,
        }

    # Verify daemon is reachable first
    status = check_hermes_status(base_url, timeout=min(3.0, timeout))
    if not status.get("online"):
        return {
            "ok": False,
            "online": False,
            "error": f"Hermes Agent AI недоступен на {base_url} ({status.get('error', 'offline')})",
            "prompt": prompt_text,
            "url": base_url,
        }

    started = time.perf_counter()

    # Candidate endpoints for Hermes AI agent dispatch:
    # 1) /api/prompt (custom agent daemon)
    # 2) /prompt
    # 3) /v1/chat/completions (OpenAI compatible)
    # 4) /run (generic task runner)
    attempts = [
        (
            f"{base_url}/api/prompt",
            {"prompt": prompt_text, **kwargs},
        ),
        (
            f"{base_url}/prompt",
            {"prompt": prompt_text, **kwargs},
        ),
        (
            f"{base_url}/v1/chat/completions",
            {
                "model": kwargs.get("model", "hermes"),
                "messages": [{"role": "user", "content": prompt_text}],
                **{k: v for k, v in kwargs.items() if k != "model"},
            },
        ),
        (
            f"{base_url}/run",
            {"input": prompt_text, "task": prompt_text, **kwargs},
        ),
    ]

    last_error: str | None = None
    last_response: httpx.Response | None = None

    for target_url, payload in attempts:
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                response = client.post(
                    target_url,
                    json=payload,
                    headers={"Content-Type": "application/json", "Accept": "application/json, text/plain, */*"},
                )
                last_response = response

                if response.status_code == 404:
                    # Endpoint doesn't exist on this Hermes server, try next candidate
                    continue

                if response.is_success:
                    latency_ms = (time.perf_counter() - started) * 1000
                    response_text = ""
                    raw_data: Any = None

                    try:
                        raw_data = response.json()
                        if isinstance(raw_data, dict):
                            # OpenAI format
                            choices = raw_data.get("choices")
                            if isinstance(choices, list) and choices:
                                msg = choices[0].get("message") or {}
                                response_text = str(msg.get("content") or choices[0].get("text") or "")
                            elif "response" in raw_data:
                                response_text = str(raw_data["response"])
                            elif "output" in raw_data:
                                response_text = str(raw_data["output"])
                            elif "result" in raw_data:
                                response_text = str(raw_data["result"])
                            elif "text" in raw_data:
                                response_text = str(raw_data["text"])
                            else:
                                response_text = str(raw_data)
                        elif isinstance(raw_data, str):
                            response_text = raw_data
                        else:
                            response_text = str(raw_data)
                    except Exception:
                        response_text = response.text

                    return {
                        "ok": True,
                        "online": True,
                        "prompt": prompt_text,
                        "response": response_text.strip(),
                        "url": base_url,
                        "endpoint": target_url,
                        "status_code": response.status_code,
                        "latency_ms": round(latency_ms, 1),
                        "details": raw_data,
                    }
                else:
                    last_error = f"Ошибка HTTP {response.status_code}: {response.text[:200]}"
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return {
                "ok": False,
                "online": False,
                "error": f"Потеряна связь с Hermes Agent AI: {exc}",
                "prompt": prompt_text,
                "url": base_url,
            }
        except Exception as exc:
            last_error = str(exc)

    return {
        "ok": False,
        "online": True,
        "error": last_error or "Ни один из стандартных эндпоинтов Hermes (/api/prompt, /v1/chat/completions) не ответил успешно",
        "prompt": prompt_text,
        "url": base_url,
        "status_code": last_response.status_code if last_response else None,
    }
