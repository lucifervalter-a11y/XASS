"""Hermes Agent integration service for XASS.

Provides connection management, health checks, session management,
chat prompt execution, and skill dispatch with graceful offline mock fallbacks.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import secrets
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)

HERMES_CONFIG_PATH = Path("./data/hermes_config.json")


def _now_iso() -> str:
    """Return current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _default_sessions() -> list[dict[str, Any]]:
    """Return seed sessions for mock fallback."""
    now = _now_iso()
    return [
        {
            "id": "hermes-sess-001",
            "title": "Системная диагностика и мониторинг",
            "message_count": 6,
            "created_at": now,
            "updated_at": now,
            "model": "hermes-3-llama-3.1-8b",
        },
        {
            "id": "hermes-sess-002",
            "title": "Управление медиатекой и сценариями",
            "message_count": 2,
            "created_at": now,
            "updated_at": now,
            "model": "hermes-3-llama-3.1-8b",
        },
    ]


def _default_skills() -> list[dict[str, Any]]:
    """Return available Hermes skills / tools catalog."""
    return [
        {
            "name": "system_metrics",
            "description": "Сбор метрик CPU, RAM, дискового пространства и сети",
            "parameters": {
                "top_processes": {"type": "integer", "description": "Количество процессов в топе", "default": 5}
            },
            "category": "system",
            "enabled": True,
        },
        {
            "name": "service_control",
            "description": "Проверка статуса или перезапуск служб сервера",
            "parameters": {
                "service": {"type": "string", "description": "Имя службы", "required": True},
                "action": {"type": "string", "description": "status или restart", "default": "status"},
            },
            "category": "system",
            "enabled": True,
        },
        {
            "name": "music_diagnostics",
            "description": "Проверка очереди воспроизведения и аудио-потока",
            "parameters": {
                "action": {"type": "string", "description": "status, next, prev, toggle", "default": "status"}
            },
            "category": "media",
            "enabled": True,
        },
        {
            "name": "database_health",
            "description": "Проверка доступности и целостности базы данных",
            "parameters": {
                "deep": {"type": "boolean", "description": "Глубокая проверка целостности", "default": False}
            },
            "category": "database",
            "enabled": True,
        },
        {
            "name": "web_search",
            "description": "Поиск по базе знаний и веб-источникам",
            "parameters": {
                "query": {"type": "string", "description": "Поисковый запрос", "required": True},
                "max_results": {"type": "integer", "description": "Количество результатов", "default": 5},
            },
            "category": "knowledge",
            "enabled": True,
        },
        {
            "name": "log_inspector",
            "description": "Просмотр последних строк системных журналов",
            "parameters": {
                "log_name": {"type": "string", "description": "update, audit, server", "default": "server"},
                "lines": {"type": "integer", "description": "Количество строк", "default": 50},
            },
            "category": "system",
            "enabled": True,
        },
    ]


def load_hermes_config(path: Path = HERMES_CONFIG_PATH) -> dict[str, Any]:
    """Load persistent Hermes config from JSON if present."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception as exc:
        logger.warning("Could not read hermes config from %s: %s", path, exc)
    return {}


def save_hermes_config(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Persist Hermes config atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return config


class HermesService:
    """Service client for Hermes WebUI and Hermes Agent.
    
    Provides connection to a local or remote Hermes Agent instance with
    automatic fallback to simulated responses when unreachable.
    """

    def __init__(
        self,
        url: str = "http://127.0.0.1:8787",
        api_key: str = "",
        enabled: bool = True,
        client: httpx.AsyncClient | None = None,
        default_model: str = "hermes-3-llama-3.1-8b",
    ) -> None:
        self.url = url.strip().rstrip("/") if url else "http://127.0.0.1:8787"
        self.api_key = (api_key or "").strip()
        self.enabled = bool(enabled)
        self.client = client
        self.default_model = default_model
        self._mock_sessions: list[dict[str, Any]] = _default_sessions()
        self._mock_skills: list[dict[str, Any]] = _default_skills()

    def _headers(self) -> dict[str, str]:
        """Construct request headers including authorization if api_key is configured."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @asynccontextmanager
    async def _client_session(self, timeout: float = 5.0) -> AsyncIterator[httpx.AsyncClient]:
        """Provide an active httpx.AsyncClient instance."""
        if self.client is not None:
            yield self.client
        else:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(2.5, timeout)),
                trust_env=False,
            ) as client:
                yield client

    async def check_health(self) -> dict[str, Any]:
        """Check if Hermes is online and healthy."""
        return await self.get_status()

    async def get_status(self) -> dict[str, Any]:
        """Retrieve Hermes status: online flag, version, active model, and session count.
        
        Falls back to graceful mock status if Hermes is unreachable or disabled.
        """
        if not self.enabled:
            return {
                "online": False,
                "enabled": False,
                "version": "offline",
                "active_model": "none",
                "session_count": len(self._mock_sessions),
                "mock": True,
                "status": "disabled",
                "details": "Hermes integration is disabled in configuration",
            }

        try:
            async with self._client_session(timeout=3.0) as client:
                resp = None
                for endpoint in ("/api/status", "/status", "/health"):
                    try:
                        resp = await client.get(f"{self.url}{endpoint}", headers=self._headers())
                        if resp.status_code == 200:
                            break
                    except httpx.HTTPError:
                        continue

                if resp is not None and resp.status_code == 200:
                    data = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
                    version = str(data.get("version") or data.get("app_version") or "v0.4.2")
                    active_model = str(data.get("active_model") or data.get("model") or self.default_model)
                    session_count = data.get("session_count")
                    if session_count is None:
                        sessions = data.get("sessions")
                        session_count = len(sessions) if isinstance(sessions, list) else len(self._mock_sessions)

                    return {
                        "online": True,
                        "enabled": True,
                        "version": version,
                        "active_model": active_model,
                        "session_count": int(session_count),
                        "mock": False,
                        "status": "online",
                        "details": "Connected to Hermes server",
                    }
        except Exception as exc:
            logger.debug("Hermes status query failed, falling back to mock: %s", exc)

        # Clean mock fallback
        return {
            "online": False,
            "enabled": True,
            "version": "hermes-agent-v0.4.0 (fallback)",
            "active_model": self.default_model,
            "session_count": len(self._mock_sessions),
            "mock": True,
            "status": "offline_fallback",
            "details": f"Hermes server unreachable at {self.url}. Operating in local fallback mode.",
        }

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List recent agent sessions with message counts and titles.
        
        Falls back to local mock sessions when unreachable or disabled.
        """
        if self.enabled:
            try:
                async with self._client_session(timeout=4.0) as client:
                    for endpoint in ("/api/sessions", "/sessions"):
                        try:
                            resp = await client.get(f"{self.url}{endpoint}", headers=self._headers())
                            if resp.status_code == 200:
                                data = resp.json()
                                raw_list = data if isinstance(data, list) else data.get("sessions", [])
                                if isinstance(raw_list, list):
                                    sessions: list[dict[str, Any]] = []
                                    for item in raw_list:
                                        if isinstance(item, dict):
                                            sessions.append({
                                                "id": str(item.get("id") or ""),
                                                "title": str(item.get("title") or "Без названия"),
                                                "message_count": int(
                                                    item.get("message_count") or len(item.get("messages", []))
                                                ),
                                                "created_at": item.get("created_at") or _now_iso(),
                                                "updated_at": item.get("updated_at") or _now_iso(),
                                                "model": item.get("model", self.default_model),
                                            })
                                    return sessions
                        except httpx.HTTPError:
                            continue
            except Exception as exc:
                logger.debug("Hermes list_sessions failed, falling back to mock: %s", exc)

        return [dict(s) for s in self._mock_sessions]

    async def create_session(self, title: str | None = None) -> dict[str, Any]:
        """Create a new agent session.
        
        Falls back to creating a local mock session if unreachable or disabled.
        """
        payload_data = {"title": title} if title else {}
        if self.enabled:
            try:
                async with self._client_session(timeout=5.0) as client:
                    for endpoint in ("/api/sessions", "/sessions"):
                        try:
                            resp = await client.post(
                                f"{self.url}{endpoint}",
                                json=payload_data,
                                headers=self._headers(),
                            )
                            if resp.status_code in (200, 201):
                                data = resp.json()
                                session = data.get("session") if isinstance(data, dict) and "session" in data else data
                                if isinstance(session, dict) and "id" in session:
                                    return {
                                        "id": str(session["id"]),
                                        "title": str(session.get("title") or title or "Новая сессия"),
                                        "message_count": int(session.get("message_count") or 0),
                                        "created_at": session.get("created_at") or _now_iso(),
                                        "updated_at": session.get("updated_at") or _now_iso(),
                                        "model": session.get("model", self.default_model),
                                        "mock": False,
                                    }
                        except httpx.HTTPError:
                            continue
            except Exception as exc:
                logger.debug("Hermes create_session failed, falling back to mock: %s", exc)

        # Local mock session creation
        session_id = f"hermes-{secrets.token_hex(6)}"
        now = _now_iso()
        session = {
            "id": session_id,
            "title": title or f"Сессия {datetime.now(timezone.utc).strftime('%d.%m %H:%M')}",
            "message_count": 0,
            "created_at": now,
            "updated_at": now,
            "model": self.default_model,
            "mock": True,
        }
        self._mock_sessions.insert(0, session)
        return session

    async def send_prompt(
        self,
        session_id: str,
        prompt: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Send user prompt to Hermes agent, returning response text, tokens used, and tool calls.
        
        Falls back to local mock response generation if unreachable or disabled.
        """
        target_model = model or self.default_model
        if self.enabled:
            try:
                async with self._client_session(timeout=30.0) as client:
                    payload = {
                        "session_id": session_id,
                        "prompt": prompt,
                        "model": target_model,
                    }
                    for endpoint in ("/api/chat", f"/api/sessions/{session_id}/chat", "/chat"):
                        try:
                            resp = await client.post(
                                f"{self.url}{endpoint}",
                                json=payload,
                                headers=self._headers(),
                            )
                            if resp.status_code == 200:
                                data = resp.json()
                                response_text = str(
                                    data.get("response") or data.get("text") or data.get("content") or ""
                                )
                                tokens_used = data.get("tokens_used")
                                if tokens_used is None:
                                    usage = data.get("usage", {})
                                    tokens_used = usage.get("total_tokens") or (
                                        len(prompt.split()) + len(response_text.split())
                                    )
                                tool_calls = data.get("tool_calls") or []
                                return {
                                    "response": response_text,
                                    "tokens_used": int(tokens_used),
                                    "tool_calls": tool_calls,
                                    "session_id": session_id,
                                    "model": data.get("model") or target_model,
                                    "mock": False,
                                }
                        except httpx.HTTPError:
                            continue
            except Exception as exc:
                logger.debug("Hermes send_prompt failed, falling back to mock: %s", exc)

        return self._generate_mock_chat_response(session_id, prompt, target_model)

    def _generate_mock_chat_response(
        self,
        session_id: str,
        prompt: str,
        model: str,
    ) -> dict[str, Any]:
        """Generate a contextual mock agent response with tool calls and token counting."""
        prompt_lower = prompt.lower()
        tool_calls: list[dict[str, Any]] = []

        if any(k in prompt_lower for k in ("статус", "health", "состояние", "сервер", "status")):
            response_text = (
                "Hermes Agent: Все службы сервера XASS функционируют в штатном режиме. "
                "Критических ошибок и предупреждений не обнаружено."
            )
            tool_calls.append({"tool": "system_metrics", "status": "executed", "result": {"status": "healthy"}})
        elif any(k in prompt_lower for k in ("память", "cpu", "нагрузка", "процесс", "metrics", "метрик")):
            response_text = (
                "Hermes Agent: Нагрузка на центральный процессор и оперативную память находится в пределах нормы. "
                "Активных узких мест в производительности не обнаружено."
            )
            tool_calls.append({
                "tool": "system_metrics",
                "status": "executed",
                "result": {"cpu_percent": 14.2, "ram_percent": 42.8},
            })
        elif any(k in prompt_lower for k in ("музык", "трек", "плейлист", "music", "audio")):
            response_text = (
                "Hermes Agent: Медиа-подсистема XASS готова к приёму команд и воспроизведению треков. "
                "Очередь воспроизведения доступна."
            )
            tool_calls.append({"tool": "music_diagnostics", "status": "executed", "result": {"state": "ready"}})
        elif any(k in prompt_lower for k in ("навык", "скилл", "skill", "tool", "инструмент")):
            response_text = (
                "Hermes Agent: Доступно 6 встроенных навыков (system_metrics, service_control, music_diagnostics, "
                "database_health, web_search, log_inspector). Вы можете вызвать любой из них."
            )
        else:
            response_text = (
                f"Hermes Agent: Запрос «{prompt[:120]}» успешно обработан. "
                f"Агент работает в режиме локального fallback (сервер Hermes {self.url} оффлайн)."
            )

        prompt_tokens = max(1, len(prompt.split()))
        completion_tokens = max(10, len(response_text.split()))
        tokens_used = prompt_tokens + completion_tokens

        now_iso = _now_iso()
        session_found = False
        for sess in self._mock_sessions:
            if sess["id"] == session_id:
                sess["message_count"] = sess.get("message_count", 0) + 2
                sess["updated_at"] = now_iso
                session_found = True
                break
        if not session_found:
            self._mock_sessions.append({
                "id": session_id,
                "title": prompt[:30] + ("..." if len(prompt) > 30 else ""),
                "message_count": 2,
                "created_at": now_iso,
                "updated_at": now_iso,
                "model": model,
                "mock": True,
            })

        return {
            "response": response_text,
            "tokens_used": tokens_used,
            "tool_calls": tool_calls,
            "session_id": session_id,
            "model": model,
            "mock": True,
        }

    async def list_skills(self) -> list[dict[str, Any]]:
        """List available Hermes agent skills/tools.
        
        Falls back to local mock skills when unreachable or disabled.
        """
        if self.enabled:
            try:
                async with self._client_session(timeout=4.0) as client:
                    for endpoint in ("/api/skills", "/skills", "/api/tools"):
                        try:
                            resp = await client.get(f"{self.url}{endpoint}", headers=self._headers())
                            if resp.status_code == 200:
                                data = resp.json()
                                raw_list = (
                                    data
                                    if isinstance(data, list)
                                    else data.get("skills", data.get("tools", []))
                                )
                                if isinstance(raw_list, list) and raw_list:
                                    return raw_list
                        except httpx.HTTPError:
                            continue
            except Exception as exc:
                logger.debug("Hermes list_skills failed, falling back to mock: %s", exc)

        return [dict(s) for s in self._mock_skills]

    async def run_skill(self, skill_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Trigger a specific Hermes agent skill.
        
        Falls back to local mock execution if unreachable or disabled.
        """
        params = params or {}
        if self.enabled:
            try:
                async with self._client_session(timeout=20.0) as client:
                    payload = {"skill": skill_name, "params": params}
                    for endpoint in (f"/api/skills/{skill_name}/run", "/api/skills/run", "/api/skill-run"):
                        try:
                            resp = await client.post(
                                f"{self.url}{endpoint}",
                                json=payload,
                                headers=self._headers(),
                            )
                            if resp.status_code == 200:
                                data = resp.json()
                                return {
                                    "ok": True,
                                    "skill": skill_name,
                                    "result": data.get("result", data),
                                    "mock": False,
                                }
                        except httpx.HTTPError:
                            continue
            except Exception as exc:
                logger.debug("Hermes run_skill failed, falling back to mock: %s", exc)

        return self._execute_mock_skill(skill_name, params)

    def _execute_mock_skill(self, skill_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute mock skill locally."""
        now_iso = _now_iso()
        if skill_name == "system_metrics":
            try:
                from app.services.monitoring import collect_server_metrics
                metrics = collect_server_metrics(top_processes_limit=int(params.get("top_processes", 5)))
                return {
                    "ok": True,
                    "skill": skill_name,
                    "executed_at": now_iso,
                    "result": {"status": "success", "metrics": metrics},
                    "mock": False,
                }
            except Exception:
                return {
                    "ok": True,
                    "skill": skill_name,
                    "executed_at": now_iso,
                    "result": {
                        "status": "success",
                        "cpu_percent": 12.4,
                        "ram_used_percent": 38.6,
                        "disk_used_percent": 45.1,
                    },
                    "mock": True,
                }

        elif skill_name == "service_control":
            svc = str(params.get("service") or "serverredus-backend")
            action = str(params.get("action") or "status")
            return {
                "ok": True,
                "skill": skill_name,
                "executed_at": now_iso,
                "result": {
                    "service": svc,
                    "action": action,
                    "status": "active",
                    "message": f"Service '{svc}' is active and operational.",
                },
                "mock": True,
            }

        elif skill_name == "music_diagnostics":
            return {
                "ok": True,
                "skill": skill_name,
                "executed_at": now_iso,
                "result": {
                    "playback_state": "idle",
                    "queue_length": 0,
                    "volume": 80,
                    "message": "Media diagnostics check completed.",
                },
                "mock": True,
            }

        elif skill_name == "database_health":
            return {
                "ok": True,
                "skill": skill_name,
                "executed_at": now_iso,
                "result": {
                    "status": "healthy",
                    "latency_ms": 1.2,
                    "open_connections": 1,
                    "message": "Database connectivity confirmed.",
                },
                "mock": True,
            }

        elif skill_name == "web_search":
            query = str(params.get("query") or "")
            return {
                "ok": True,
                "skill": skill_name,
                "executed_at": now_iso,
                "result": {
                    "query": query,
                    "results": [
                        {
                            "title": f"Hermes Agent Knowledge: {query}",
                            "snippet": f"Overview and references for {query}",
                        }
                    ],
                },
                "mock": True,
            }

        return {
            "ok": True,
            "skill": skill_name,
            "executed_at": now_iso,
            "result": {
                "message": f"Skill '{skill_name}' processed successfully.",
                "params": params,
            },
            "mock": True,
        }


_hermes_service_instance: HermesService | None = None


def get_hermes_service(settings: Any = None) -> HermesService:
    """Retrieve or create the shared HermesService instance."""
    global _hermes_service_instance
    if _hermes_service_instance is None:
        url = getattr(settings, "hermes_url", "http://127.0.0.1:8787") if settings else "http://127.0.0.1:8787"
        api_key = getattr(settings, "hermes_api_key", "") if settings else ""
        enabled = getattr(settings, "hermes_enabled", True) if settings else True
        _hermes_service_instance = HermesService(url=url, api_key=api_key, enabled=enabled)
    elif settings is not None:
        if hasattr(settings, "hermes_url"):
            _hermes_service_instance.url = getattr(settings, "hermes_url", _hermes_service_instance.url)
        if hasattr(settings, "hermes_api_key"):
            _hermes_service_instance.api_key = getattr(settings, "hermes_api_key", _hermes_service_instance.api_key)
        if hasattr(settings, "hermes_enabled"):
            _hermes_service_instance.enabled = getattr(settings, "hermes_enabled", _hermes_service_instance.enabled)
    return _hermes_service_instance


async def check_health(settings: Any = None) -> dict[str, Any]:
    """Helper function to check Hermes health."""
    return await get_hermes_service(settings).check_health()


async def get_status(settings: Any = None) -> dict[str, Any]:
    """Helper function to get Hermes status."""
    return await get_hermes_service(settings).get_status()


async def list_sessions(settings: Any = None) -> list[dict[str, Any]]:
    """Helper function to list Hermes sessions."""
    return await get_hermes_service(settings).list_sessions()


async def create_session(title: str | None = None, settings: Any = None) -> dict[str, Any]:
    """Helper function to create a Hermes session."""
    return await get_hermes_service(settings).create_session(title=title)


async def send_prompt(
    session_id: str,
    prompt: str,
    model: str | None = None,
    settings: Any = None,
) -> dict[str, Any]:
    """Helper function to send a prompt to Hermes agent."""
    return await get_hermes_service(settings).send_prompt(session_id=session_id, prompt=prompt, model=model)


async def list_skills(settings: Any = None) -> list[dict[str, Any]]:
    """Helper function to list available Hermes skills."""
    return await get_hermes_service(settings).list_skills()


async def run_skill(
    skill_name: str,
    params: dict[str, Any] | None = None,
    settings: Any = None,
) -> dict[str, Any]:
    """Helper function to execute a Hermes skill."""
    return await get_hermes_service(settings).run_skill(skill_name=skill_name, params=params)
