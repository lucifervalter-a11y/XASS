from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import httpx

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

WEATHER_CODES_RU = {
    0: "Ясно",
    1: "Малооблачно",
    2: "Переменная облачность",
    3: "Пасмурно",
    45: "Туман",
    48: "Туман",
    51: "Морось",
    53: "Морось",
    55: "Морось",
    56: "Ледяная морось",
    57: "Ледяная морось",
    61: "Дождь",
    63: "Дождь",
    65: "Ливень",
    66: "Ледяной дождь",
    67: "Ледяной дождь",
    71: "Снег",
    73: "Снег",
    75: "Сильный снег",
    77: "Снежная крупа",
    80: "Ливень",
    81: "Ливень",
    82: "Сильный ливень",
    85: "Снегопад",
    86: "Снегопад",
    95: "Гроза",
    96: "Гроза с градом",
    99: "Гроза с градом",
}


@dataclass(slots=True)
class WeatherCard:
    location_name: str
    latitude: float
    longitude: float
    timezone_name: str
    temperature: str
    feels_like: str
    wind_speed: str
    humidity: str
    weather_text: str
    updated_time: str


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_float(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip().replace(",", ".")
        if not raw:
            return fallback
        try:
            return float(raw)
        except ValueError:
            return fallback
    return fallback


def _format_temp(value: Any) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{parsed:.1f}°C"


def _format_wind(value: Any) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{parsed:.1f} м/с"


def _format_humidity(value: Any) -> str:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return "-"
    return f"{parsed}%"


def _format_updated(value: Any, timezone_name: str) -> str:
    raw = _clean_text(value)
    if not raw:
        return "-"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(timezone_name)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        else:
            parsed = parsed.astimezone(tz)
        return f"{parsed.strftime('%H:%M')} {parsed.tzname() or timezone_name}"
    except Exception:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%H:%M UTC")


def _weather_text_from_code(code: Any) -> str:
    try:
        parsed = int(code)
    except (TypeError, ValueError):
        return "Без уточнения"
    return WEATHER_CODES_RU.get(parsed, "Без уточнения")


def _extract_location_from_profile(profile: dict[str, Any]) -> tuple[str, float, float, str]:
    location_name = _clean_text(profile.get("weather_location_name")) or "Москва"
    latitude = _to_float(profile.get("weather_latitude"), 55.7558)
    longitude = _to_float(profile.get("weather_longitude"), 37.6176)
    timezone_name = _clean_text(profile.get("weather_timezone")) or "Europe/Moscow"
    return location_name, latitude, longitude, timezone_name


async def _resolve_location_by_query(query: str) -> tuple[str, float, float, str] | None:
    text = _clean_text(query)
    if not text:
        return None
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(
                OPEN_METEO_GEOCODING_URL,
                params={"name": text, "count": 1, "language": "ru", "format": "json"},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None

    name = _clean_text(first.get("name")) or text
    country = _clean_text(first.get("country"))
    admin = _clean_text(first.get("admin1"))
    location_name = ", ".join(part for part in (name, admin, country) if part)
    latitude = _to_float(first.get("latitude"), 0.0)
    longitude = _to_float(first.get("longitude"), 0.0)
    timezone_name = _clean_text(first.get("timezone")) or "Europe/Moscow"
    if latitude == 0.0 and longitude == 0.0:
        return None
    return location_name, latitude, longitude, timezone_name


async def build_weather_card(query: str, profile: dict[str, Any]) -> WeatherCard | None:
    resolved = await _resolve_location_by_query(query)
    if resolved is None:
        resolved = _extract_location_from_profile(profile)

    location_name, latitude, longitude, timezone_name = resolved
    try:
        async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
            response = await client.get(
                OPEN_METEO_FORECAST_URL,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
                    "timezone": timezone_name,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    current = payload.get("current")
    if not isinstance(current, dict):
        return None

    return WeatherCard(
        location_name=location_name,
        latitude=latitude,
        longitude=longitude,
        timezone_name=timezone_name,
        temperature=_format_temp(current.get("temperature_2m")),
        feels_like=_format_temp(current.get("apparent_temperature")),
        wind_speed=_format_wind(current.get("wind_speed_10m")),
        humidity=_format_humidity(current.get("relative_humidity_2m")),
        weather_text=_weather_text_from_code(current.get("weather_code")),
        updated_time=_format_updated(current.get("time"), timezone_name),
    )


def build_weather_links(card: WeatherCard) -> dict[str, str]:
    query = card.location_name or f"{card.latitude},{card.longitude}"
    encoded = quote_plus(query)
    lat = f"{card.latitude:.4f}"
    lon = f"{card.longitude:.4f}"
    return {
        "Google": f"https://www.google.com/search?q=weather+{encoded}",
        "Яндекс": f"https://yandex.ru/search/?text=погода+{encoded}",
        "2GIS": f"https://2gis.ru/search/{encoded}",
        "Windy": f"https://www.windy.com/{lat}/{lon}?{lat},{lon},8",
    }

