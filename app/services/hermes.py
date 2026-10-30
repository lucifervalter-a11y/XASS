"""Strict local XASS AI service adapter.

The adapter deliberately fails closed: it only connects to an explicitly configured
loopback HTTP origin, verifies the exact installed runtime version and commit, and
never fabricates sessions, answers, skills, or tool results.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import ipaddress
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, NoReturn
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_HERMES_URL = "http://127.0.0.1:9119"
HEALTH_PATH = "/api/health"
SUPPORTED_HERMES_VERSION = "0.22.2"
SUPPORTED_HERMES_COMMIT = "d923a5eb487a17c0a68801bba9ef0f56c81040a8"
_HEALTH_KEYS = frozenset({"ok", "version", "displayVersion", "commit", "auth_required"})
MAX_HEALTH_BODY_BYTES = 64 * 1024
HERMES_CONFIG_PATH = Path("./data/hermes_config.json")


class HermesServiceError(RuntimeError):
    """Base class for a safe, user-facing XASS AI dependency error."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class HermesUnavailableError(HermesServiceError):
    """The configured XASS AI dependency is disabled, unhealthy, or unreachable."""


class HermesDependencyError(HermesServiceError):
    """The XASS AI dependency returned an incompatible or rejected response."""


def normalize_hermes_url(value: str | None) -> str:
    """Return the canonical HTTP origin for a literal loopback address."""

    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_HERMES_URL
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parsed = urlsplit(raw)
        port = 9119 if parsed.port is None else parsed.port
    except ValueError as exc:
        raise ValueError("XASS AI URL is invalid") from exc

    if parsed.scheme.lower() != "http":
        raise ValueError("XASS AI supports only a local HTTP origin")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("XASS AI URL must be a loopback origin without credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("XASS AI URL must not contain a path, query, or fragment")
    if not 1 <= port <= 65535:
        raise ValueError("XASS AI URL has an invalid port")

    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost":
        canonical_host = "localhost"
    else:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise ValueError("XASS AI URL must use a loopback address") from exc
        if not address.is_loopback:
            raise ValueError("XASS AI URL must use a loopback address")
        canonical_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"http://{canonical_host}:{port}"


def load_hermes_config(path: Path = HERMES_CONFIG_PATH) -> dict[str, Any]:
    """Load persisted XASS AI settings without exposing their values in logs."""

    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.warning("Could not read XASS AI config from %s", path)
        return {}


def save_hermes_config(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Persist XASS AI settings atomically and restrict the file where supported."""

    safe_config = {
        key: value
        for key, value in config.items()
        if key != "hermes_api_key"
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(safe_config, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp_path.chmod(0o600)
    except OSError:
        logger.debug("Could not restrict temporary XASS AI config permissions")
    tmp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        logger.debug("Could not restrict XASS AI config permissions")
    return safe_config


class HermesService:
    """Fail-closed HTTP adapter for the locally installed XASS AI runtime."""

    def __init__(
        self,
        url: str = DEFAULT_HERMES_URL,
        api_key: str = "",
        enabled: bool = True,
        client: httpx.AsyncClient | None = None,
        default_model: str = "",
    ) -> None:
        self.url = (url or DEFAULT_HERMES_URL).strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.enabled = bool(enabled)
        self.client = client
        self.default_model = default_model

    def _validated_url(self) -> str:
        try:
            return normalize_hermes_url(self.url)
        except ValueError as exc:
            raise HermesUnavailableError(
                "XASS AI configuration is invalid.",
                reason="invalid_config",
            ) from exc

    @asynccontextmanager
    async def _client_session(self, timeout: float = 5.0) -> AsyncIterator[httpx.AsyncClient]:
        if self.client is not None:
            yield self.client
            return
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(2.5, timeout)),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            yield client

    @staticmethod
    def _offline_status(
        status_name: str,
        details: str,
        *,
        enabled: bool = True,
    ) -> dict[str, Any]:
        return {
            "online": False,
            "enabled": enabled,
            "interactive_available": False,
            "interactive_status": status_name,
            "version": "",
            "active_model": "",
            "session_count": 0,
            "mock": False,
            "status": status_name,
            "details": details,
        }

    async def check_health(self) -> dict[str, Any]:
        return await self.get_status()

    async def get_status(self) -> dict[str, Any]:
        """Verify the exact installed XASS AI 0.22.2 health contract."""

        if not self.enabled:
            return self._offline_status(
                "disabled",
                "XASS AI integration is disabled.",
                enabled=False,
            )

        try:
            base_url = self._validated_url()
        except HermesUnavailableError:
            return self._offline_status(
                "invalid_config",
                "XASS AI configuration is invalid.",
            )

        try:
            async with self._client_session(timeout=3.0) as client:
                async with client.stream(
                    "GET",
                    f"{base_url}{HEALTH_PATH}",
                    headers={"Accept": "application/json"},
                ) as response:
                    response_status = response.status_code
                    health_body = bytearray()
                    if response_status == 200:
                        content_length = response.headers.get("content-length")
                        if content_length is not None:
                            try:
                                if int(content_length) > MAX_HEALTH_BODY_BYTES:
                                    return self._offline_status(
                                        "health_too_large",
                                        "XASS AI health response exceeded the size limit.",
                                    )
                            except ValueError:
                                pass
                        async for chunk in response.aiter_bytes():
                            if len(health_body) + len(chunk) > MAX_HEALTH_BODY_BYTES:
                                return self._offline_status(
                                    "health_too_large",
                                    "XASS AI health response exceeded the size limit.",
                                )
                            health_body.extend(chunk)
        except httpx.HTTPError:
            return self._offline_status(
                "unavailable",
                "XASS AI is not reachable.",
            )
        except Exception:
            logger.exception("Unexpected XASS AI health-check failure")
            return self._offline_status(
                "unavailable",
                "XASS AI health check failed.",
            )

        if response_status in {401, 403}:
            return self._offline_status(
                "unauthorized",
                "XASS AI rejected the health check.",
            )
        if response_status == 404:
            return self._offline_status(
                "not_found",
                "XASS AI health endpoint is unavailable.",
            )
        if response_status >= 500:
            return self._offline_status(
                "upstream_error",
                "XASS AI reported an internal error.",
            )
        if response_status != 200:
            return self._offline_status(
                "unhealthy",
                "XASS AI health check was rejected.",
            )

        try:
            data = json.loads(health_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            data = None
        if not isinstance(data, dict) or frozenset(data) != _HEALTH_KEYS:
            return self._offline_status(
                "incompatible_health",
                f"Unsupported local service; XASS AI {SUPPORTED_HERMES_VERSION} is required.",
            )

        display_version = data.get("displayVersion")
        commit = data.get("commit")
        auth_required = data.get("auth_required")
        if (
            data.get("ok") is not True
            or data.get("version") != SUPPORTED_HERMES_VERSION
            or not isinstance(display_version, str)
            or not display_version.strip()
            or not isinstance(commit, str)
            or commit.strip().casefold() != SUPPORTED_HERMES_COMMIT
            or auth_required is not True
        ):
            return self._offline_status(
                "incompatible_health",
                f"Unsupported local service; XASS AI {SUPPORTED_HERMES_VERSION} is required.",
            )

        return {
            "online": True,
            "enabled": True,
            "interactive_available": False,
            "interactive_status": "unsupported_transport",
            "version": SUPPORTED_HERMES_VERSION,
            "display_version": display_version.strip(),
            "runtime_commit": commit.strip(),
            "auth_required": auth_required,
            "active_model": "",
            "session_count": 0,
            "mock": False,
            "status": "online",
            "details": "Verified local XASS AI runtime.",
        }

    async def _require_available(self) -> None:
        status_data = await self.get_status()
        if not status_data["online"]:
            raise HermesUnavailableError(
                str(status_data["details"]),
                reason=str(status_data["status"]),
            )

    async def _unsupported_interactive_transport(self) -> NoReturn:
        await self._require_available()
        raise HermesDependencyError(
            "The verified runtime uses a token-authenticated WebSocket. "
            "This backend has no approved process token or phone-to-PC transport.",
            reason="unsupported_transport",
        )

    async def list_sessions(self) -> list[dict[str, Any]]:
        await self._unsupported_interactive_transport()

    async def create_session(self, title: str | None = None) -> dict[str, Any]:
        await self._unsupported_interactive_transport()

    async def send_prompt(
        self,
        session_id: str,
        prompt: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        await self._unsupported_interactive_transport()

    async def list_skills(self) -> list[dict[str, Any]]:
        await self._unsupported_interactive_transport()

    async def run_skill(
        self,
        skill_name: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._unsupported_interactive_transport()


_hermes_service_instance: HermesService | None = None


def get_hermes_service(settings: Any = None) -> HermesService:
    """Retrieve or update the shared strict XASS AI adapter."""

    global _hermes_service_instance
    if _hermes_service_instance is None:
        url = getattr(settings, "hermes_url", DEFAULT_HERMES_URL) if settings else DEFAULT_HERMES_URL
        api_key = getattr(settings, "hermes_api_key", "") if settings else ""
        enabled = getattr(settings, "hermes_enabled", True) if settings else True
        _hermes_service_instance = HermesService(url=url, api_key=api_key, enabled=enabled)
    elif settings is not None:
        if hasattr(settings, "hermes_url"):
            _hermes_service_instance.url = str(getattr(settings, "hermes_url") or DEFAULT_HERMES_URL)
        if hasattr(settings, "hermes_api_key"):
            _hermes_service_instance.api_key = str(getattr(settings, "hermes_api_key") or "")
        if hasattr(settings, "hermes_enabled"):
            _hermes_service_instance.enabled = bool(getattr(settings, "hermes_enabled"))
    return _hermes_service_instance


async def check_health(settings: Any = None) -> dict[str, Any]:
    return await get_hermes_service(settings).check_health()


async def get_status(settings: Any = None) -> dict[str, Any]:
    return await get_hermes_service(settings).get_status()


async def list_sessions(settings: Any = None) -> list[dict[str, Any]]:
    return await get_hermes_service(settings).list_sessions()


async def create_session(title: str | None = None, settings: Any = None) -> dict[str, Any]:
    return await get_hermes_service(settings).create_session(title=title)


async def send_prompt(
    session_id: str,
    prompt: str,
    model: str | None = None,
    settings: Any = None,
) -> dict[str, Any]:
    return await get_hermes_service(settings).send_prompt(
        session_id=session_id,
        prompt=prompt,
        model=model,
    )


async def list_skills(settings: Any = None) -> list[dict[str, Any]]:
    return await get_hermes_service(settings).list_skills()


async def run_skill(
    skill_name: str,
    params: dict[str, Any] | None = None,
    settings: Any = None,
) -> dict[str, Any]:
    return await get_hermes_service(settings).run_skill(
        skill_name=skill_name,
        params=params,
    )
