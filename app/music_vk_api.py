"""Owner VK audio import routes for the private XASS catalog."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import get_session


class VkImportBody(BaseModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=25)


def install_vk_import_routes(router, settings, require_owner):
    @router.get("/api/mini/music/vk/status")
    async def vk_status(user=Depends(require_owner)):
        from app.services.vk_music_import import VkImportError, list_vk_audio, resolve_vk_credentials
        try:
            token, user_id = resolve_vk_credentials(settings)
        except VkImportError as exc:
            return {"ok": True, "connected": False, "audio_ready": False, "detail": str(exc)}
        try:
            listing = await list_vk_audio(token, user_id, offset=0, limit=1)
        except VkImportError as exc:
            return {"ok": True, "connected": True, "audio_ready": False, "vk_user_id": user_id, "detail": str(exc)}
        return {"ok": True, "connected": True, "audio_ready": True, "vk_user_id": user_id,
                "total": listing.get("total", 0)}

    @router.get("/api/mini/music/vk/library")
    async def vk_library(offset: int = Query(default=0, ge=0), limit: int = Query(default=10, ge=1, le=25),
                         user=Depends(require_owner)):
        from app.services.vk_music_import import VkImportError, list_vk_audio, resolve_vk_credentials
        try:
            token, user_id = resolve_vk_credentials(settings)
            listing = await list_vk_audio(token, user_id, offset=offset, limit=limit)
        except VkImportError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, **listing}

    @router.post("/api/mini/music/vk/import")
    async def vk_import(payload: VkImportBody, user=Depends(require_owner), session=Depends(get_session)):
        from app.services.vk_music_import import VkImportError, import_vk_batch
        sessions = async_sessionmaker(session.bind, expire_on_commit=False)
        await session.rollback()
        try:
            result = await import_vk_batch(settings, sessions, offset=payload.offset, limit=payload.limit)
        except VkImportError as exc:
            raise HTTPException(409, str(exc)) from exc
        return result
