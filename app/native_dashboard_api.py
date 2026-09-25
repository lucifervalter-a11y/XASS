"""Structured dashboard data for native clients."""
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.services.profile_editor import load_profile
from app.services.weather_card import build_weather_card


def build_router(settings, require_owner):
    router = APIRouter()

    @router.get("/api/mini/weather")
    async def weather(user=Depends(require_owner)):
        profile = load_profile(Path(settings.profile_json_path))
        card = await build_weather_card("", profile)
        if card is None:
            raise HTTPException(503, "Не удалось обновить погоду. Повторите позже.")
        return {"ok": True, "weather": asdict(card)}

    return router
