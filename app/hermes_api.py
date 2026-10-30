"""Owner-only MiniApp API for the local XASS AI integration."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings
from app.db import get_session
from app.services.hermes import (
    DEFAULT_HERMES_URL,
    HERMES_CONFIG_PATH,
    HermesDependencyError,
    HermesUnavailableError,
    get_hermes_service,
    load_hermes_config,
    normalize_hermes_url,
    save_hermes_config,
)

logger = logging.getLogger(__name__)


class SessionCreatePayload(BaseModel):
    """Payload for creating a new Hermes agent session."""
    title: str | None = Field(default=None, max_length=256)


class ChatPayload(BaseModel):
    """Payload for sending a prompt to Hermes agent."""
    session_id: str = Field(..., min_length=1, max_length=256)
    prompt: str = Field(..., min_length=1, max_length=65536)
    model: str | None = Field(default=None, max_length=128)


class SkillRunPayload(BaseModel):
    """Payload for executing a specific Hermes skill."""
    skill_name: str = Field(..., min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)


class HermesConfigPayload(BaseModel):
    """Payload for updating Hermes integration settings."""
    hermes_url: str | None = Field(default=None, max_length=512)
    hermes_api_key: str | None = Field(default=None, max_length=512)
    hermes_enabled: bool | None = None


def build_router(
    settings: Settings,
    require_mini_owner: Any,
    require_action_proof: Any,
) -> APIRouter:
    """Build and return the owner-authenticated XASS AI router."""
    router = APIRouter()

    # Sync persistent saved config if available. Invalid historical URLs are
    # ignored so they can never trigger an outbound request.
    saved_cfg = load_hermes_config(HERMES_CONFIG_PATH)
    if saved_cfg:
        if "hermes_url" in saved_cfg and saved_cfg["hermes_url"]:
            try:
                settings.hermes_url = normalize_hermes_url(str(saved_cfg["hermes_url"]))
            except ValueError:
                logger.warning("Ignoring invalid persisted XASS AI URL")
        if "hermes_enabled" in saved_cfg:
            settings.hermes_enabled = bool(saved_cfg["hermes_enabled"])
        if "hermes_api_key" in saved_cfg:
            # Legacy builds persisted a reusable key. The verified runtime uses
            # a process-lifetime WebSocket token instead, so scrub the old value.
            sanitized_cfg = {
                "hermes_url": settings.hermes_url,
                "hermes_enabled": settings.hermes_enabled,
            }
            try:
                save_hermes_config(HERMES_CONFIG_PATH, sanitized_cfg)
            except OSError:
                logger.warning("Could not scrub legacy XASS AI key from config")

    # Never use environment or persisted legacy API keys as the runtime process token.
    settings.hermes_api_key = ""

    service = get_hermes_service(settings)

    def sync_service() -> None:
        service.url = settings.hermes_url
        service.api_key = settings.hermes_api_key
        service.enabled = settings.hermes_enabled

    async def service_call(operation: Awaitable[Any]) -> Any:
        try:
            return await operation
        except HermesUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": exc.reason, "message": str(exc)},
            ) from exc
        except HermesDependencyError as exc:
            raise HTTPException(
                status_code=status.HTTP_424_FAILED_DEPENDENCY,
                detail={"code": exc.reason, "message": str(exc)},
            ) from exc

    def public_config() -> dict[str, Any]:
        return {
            "hermes_url": settings.hermes_url,
            # Keep the old field for clients that already render it, but never
            # return credential material to a browser or phone.
            "hermes_api_key": "",
            "hermes_api_key_configured": False,
            "hermes_enabled": settings.hermes_enabled,
        }

    @router.get("/api/mini/hermes/status")
    async def get_status_endpoint(user: Any = Depends(require_mini_owner)) -> dict[str, Any]:
        """Return an honest XASS AI health snapshot."""
        sync_service()
        status_data = await service.get_status()
        return {"ok": True, "status": status_data}

    @router.get("/api/mini/hermes/sessions")
    async def list_sessions_endpoint(user: Any = Depends(require_mini_owner)) -> dict[str, Any]:
        """List actual XASS AI sessions."""
        sync_service()
        sessions = await service_call(service.list_sessions())
        return {"ok": True, "sessions": sessions}

    @router.post("/api/mini/hermes/sessions")
    async def create_session_endpoint(
        payload: SessionCreatePayload = Body(default_factory=SessionCreatePayload),
        x_telegram_init_data: str | None = Header(default=None),
        x_xass_action_proof: str | None = Header(default=None),
        user: Any = Depends(require_mini_owner),
        session: Any = Depends(get_session),
    ) -> dict[str, Any]:
        """Create an actual XASS AI session."""
        await require_action_proof(
            session=session,
            user=user,
            telegram_init_data=x_telegram_init_data or "",
            action_proof=x_xass_action_proof or "",
            purpose="xass-ai:session-create",
            binding={"title": payload.title},
        )
        sync_service()
        session = await service_call(service.create_session(title=payload.title))
        return {"ok": True, "session": session}

    @router.post("/api/mini/hermes/chat")
    async def chat_endpoint(
        payload: ChatPayload,
        x_telegram_init_data: str | None = Header(default=None),
        x_xass_action_proof: str | None = Header(default=None),
        user: Any = Depends(require_mini_owner),
        session: Any = Depends(get_session),
    ) -> dict[str, Any]:
        """Send a prompt to XASS AI without fallback generation."""
        await require_action_proof(
            session=session,
            user=user,
            telegram_init_data=x_telegram_init_data or "",
            action_proof=x_xass_action_proof or "",
            purpose="xass-ai:prompt",
            binding={
                "session_id": payload.session_id,
                "prompt": payload.prompt,
                "model": payload.model,
            },
        )
        sync_service()
        result = await service_call(
            service.send_prompt(
                session_id=payload.session_id,
                prompt=payload.prompt,
                model=payload.model,
            )
        )
        return {"ok": True, **result}

    @router.get("/api/mini/hermes/skills")
    async def list_skills_endpoint(user: Any = Depends(require_mini_owner)) -> dict[str, Any]:
        """List actual XASS AI skills/tools."""
        sync_service()
        skills = await service_call(service.list_skills())
        return {"ok": True, "skills": skills}

    @router.post("/api/mini/hermes/skill-run")
    async def skill_run_endpoint(
        payload: SkillRunPayload,
        x_telegram_init_data: str | None = Header(default=None),
        x_xass_action_proof: str | None = Header(default=None),
        user: Any = Depends(require_mini_owner),
        session: Any = Depends(get_session),
    ) -> dict[str, Any]:
        """Execute a specific XASS AI skill."""
        await require_action_proof(
            session=session,
            user=user,
            telegram_init_data=x_telegram_init_data or "",
            action_proof=x_xass_action_proof or "",
            purpose="xass-ai:skill-run",
            binding={"skill_name": payload.skill_name, "params": payload.params},
        )
        sync_service()
        result = await service_call(
            service.run_skill(
                skill_name=payload.skill_name,
                params=payload.params,
            )
        )
        return {"ok": True, **result}

    @router.get("/api/mini/hermes/config")
    async def get_config_endpoint(user: Any = Depends(require_mini_owner)) -> dict[str, Any]:
        """Retrieve redacted XASS AI integration configuration."""
        return {"ok": True, "config": public_config()}

    @router.post("/api/mini/hermes/config")
    async def update_config_endpoint(
        payload: HermesConfigPayload,
        x_telegram_init_data: str | None = Header(default=None),
        x_xass_action_proof: str | None = Header(default=None),
        user: Any = Depends(require_mini_owner),
        session: Any = Depends(get_session),
    ) -> dict[str, Any]:
        """Update and persist loopback-only XASS AI configuration."""
        next_url = settings.hermes_url
        if payload.hermes_url is not None:
            try:
                next_url = normalize_hermes_url(payload.hermes_url or DEFAULT_HERMES_URL)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(exc),
                ) from exc
        if payload.hermes_api_key is not None and payload.hermes_api_key.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reusable XASS AI API keys are not supported",
            )
        next_enabled = settings.hermes_enabled
        if payload.hermes_enabled is not None:
            next_enabled = bool(payload.hermes_enabled)

        await require_action_proof(
            session=session,
            user=user,
            telegram_init_data=x_telegram_init_data or "",
            action_proof=x_xass_action_proof or "",
            purpose="xass-ai:config",
            binding={"hermes_url": next_url, "hermes_enabled": next_enabled},
        )

        persisted = {
            "hermes_url": next_url,
            "hermes_enabled": next_enabled,
        }
        save_hermes_config(HERMES_CONFIG_PATH, persisted)

        settings.hermes_url = next_url
        settings.hermes_api_key = ""
        settings.hermes_enabled = next_enabled
        sync_service()

        return {"ok": True, "config": public_config()}

    return router


build_hermes_router = build_router
