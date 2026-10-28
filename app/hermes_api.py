"""MiniApp API router for Hermes Agent and WebUI integration."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings
from app.services.hermes import (
    HERMES_CONFIG_PATH,
    HermesService,
    get_hermes_service,
    load_hermes_config,
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


def build_router(settings: Settings, require_mini_owner: Any) -> APIRouter:
    """Build and return FastAPI router for Hermes MiniApp endpoints."""
    router = APIRouter()

    # Sync persistent saved config if available
    saved_cfg = load_hermes_config(HERMES_CONFIG_PATH)
    if saved_cfg:
        if "hermes_url" in saved_cfg and saved_cfg["hermes_url"]:
            settings.hermes_url = str(saved_cfg["hermes_url"]).rstrip("/")
        if "hermes_api_key" in saved_cfg:
            settings.hermes_api_key = str(saved_cfg["hermes_api_key"])
        if "hermes_enabled" in saved_cfg:
            settings.hermes_enabled = bool(saved_cfg["hermes_enabled"])

    service = get_hermes_service(settings)

    @router.get("/api/mini/hermes/status")
    async def get_status_endpoint(user: Any = Depends(require_mini_owner)) -> dict[str, Any]:
        """Check status of Hermes server and active model/sessions."""
        service.url = settings.hermes_url
        service.api_key = settings.hermes_api_key
        service.enabled = settings.hermes_enabled
        status_data = await service.get_status()
        return {"ok": True, "status": status_data}

    @router.get("/api/mini/hermes/sessions")
    async def list_sessions_endpoint(user: Any = Depends(require_mini_owner)) -> dict[str, Any]:
        """List active and recent Hermes agent sessions."""
        service.url = settings.hermes_url
        service.api_key = settings.hermes_api_key
        service.enabled = settings.hermes_enabled
        sessions = await service.list_sessions()
        return {"ok": True, "sessions": sessions}

    @router.post("/api/mini/hermes/sessions")
    async def create_session_endpoint(
        payload: SessionCreatePayload = Body(default_factory=SessionCreatePayload),
        user: Any = Depends(require_mini_owner),
    ) -> dict[str, Any]:
        """Create a new agent session."""
        service.url = settings.hermes_url
        service.api_key = settings.hermes_api_key
        service.enabled = settings.hermes_enabled
        session = await service.create_session(title=payload.title)
        return {"ok": True, "session": session}

    @router.post("/api/mini/hermes/chat")
    async def chat_endpoint(
        payload: ChatPayload,
        user: Any = Depends(require_mini_owner),
    ) -> dict[str, Any]:
        """Send user prompt to Hermes agent."""
        service.url = settings.hermes_url
        service.api_key = settings.hermes_api_key
        service.enabled = settings.hermes_enabled
        result = await service.send_prompt(
            session_id=payload.session_id,
            prompt=payload.prompt,
            model=payload.model,
        )
        return {"ok": True, **result}

    @router.get("/api/mini/hermes/skills")
    async def list_skills_endpoint(user: Any = Depends(require_mini_owner)) -> dict[str, Any]:
        """List available Hermes agent skills/tools."""
        service.url = settings.hermes_url
        service.api_key = settings.hermes_api_key
        service.enabled = settings.hermes_enabled
        skills = await service.list_skills()
        return {"ok": True, "skills": skills}

    @router.post("/api/mini/hermes/skill-run")
    async def skill_run_endpoint(
        payload: SkillRunPayload,
        user: Any = Depends(require_mini_owner),
    ) -> dict[str, Any]:
        """Execute a specific Hermes agent skill."""
        service.url = settings.hermes_url
        service.api_key = settings.hermes_api_key
        service.enabled = settings.hermes_enabled
        result = await service.run_skill(
            skill_name=payload.skill_name,
            params=payload.params,
        )
        return {"ok": True, **result}

    @router.get("/api/mini/hermes/config")
    async def get_config_endpoint(user: Any = Depends(require_mini_owner)) -> dict[str, Any]:
        """Retrieve Hermes integration configuration."""
        return {
            "ok": True,
            "config": {
                "hermes_url": settings.hermes_url,
                "hermes_api_key": settings.hermes_api_key,
                "hermes_enabled": settings.hermes_enabled,
            },
        }

    @router.post("/api/mini/hermes/config")
    async def update_config_endpoint(
        payload: HermesConfigPayload,
        user: Any = Depends(require_mini_owner),
    ) -> dict[str, Any]:
        """Update and persist Hermes integration configuration."""
        if payload.hermes_url is not None:
            url = payload.hermes_url.strip().rstrip("/")
            if url and not url.startswith(("http://", "https://")):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="hermes_url must start with http:// or https://",
                )
            settings.hermes_url = url or "http://127.0.0.1:8787"
            service.url = settings.hermes_url

        if payload.hermes_api_key is not None:
            settings.hermes_api_key = payload.hermes_api_key.strip()
            service.api_key = settings.hermes_api_key

        if payload.hermes_enabled is not None:
            settings.hermes_enabled = bool(payload.hermes_enabled)
            service.enabled = settings.hermes_enabled

        persisted = {
            "hermes_url": settings.hermes_url,
            "hermes_api_key": settings.hermes_api_key,
            "hermes_enabled": settings.hermes_enabled,
        }
        save_hermes_config(HERMES_CONFIG_PATH, persisted)

        return {"ok": True, "config": persisted}

    return router


build_hermes_router = build_router
