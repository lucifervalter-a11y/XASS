from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings
from app.models import HeartbeatSource
from app.services.music_card import normalize_track_input
from app.services.profile_editor import ensure_profile_exists, save_profile

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
VK_STATUS_URL = "https://api.vk.com/method/status.get"
NOW_PLAYING_SOURCES = {"pc_agent", "iphone", "vk"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_float(value: Any, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
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


def _to_int(value: Any, fallback: int, *, min_value: int, max_value: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        parsed = int(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return fallback
        try:
            parsed = int(float(raw))
        except ValueError:
            return fallback
    else:
        return fallback
    if parsed < min_value:
        return min_value
    if parsed > max_value:
        return max_value
    return parsed


def _to_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"1", "true", "yes", "y", "on"}:
            return True
        if raw in {"0", "false", "no", "n", "off"}:
            return False
    return fallback


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = _to_clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _normalize_now_playing_source(value: Any, fallback: str = "pc_agent") -> str:
    raw = _to_clean_text(value).lower()
    if raw in NOW_PLAYING_SOURCES:
        return raw
    return fallback if fallback in NOW_PLAYING_SOURCES else "pc_agent"


def _resolve_now_playing_source(profile: dict[str, Any], settings: Settings) -> str:
    return _normalize_now_playing_source(
        profile.get("now_listening_source"),
        _normalize_now_playing_source(settings.now_playing_source_default, "pc_agent"),
    )


def _format_float_compact(value: Any, precision: int = 1) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return ""
    text = f"{parsed:.{precision}f}".rstrip("0").rstrip(".")
    return text


def _format_weather_updated_time(raw_value: Any, timezone_name: str) -> str:
    text = _to_clean_text(raw_value)
    if not text:
        return ""

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""

    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return f"{dt.strftime('%H:%M')} {dt.tzname() or timezone_name}"


def _weather_code_to_ru(code: int) -> str:
    mapping = {
        0: "РЇСЃРЅРѕ",
        1: "РњР°Р»РѕРѕР±Р»Р°С‡РЅРѕ",
        2: "РџРµСЂРµРјРµРЅРЅР°СЏ РѕР±Р»Р°С‡РЅРѕСЃС‚СЊ",
        3: "РџР°СЃРјСѓСЂРЅРѕ",
        45: "РўСѓРјР°РЅ",
        48: "РўСѓРјР°РЅ",
        51: "РњРѕСЂРѕСЃСЊ",
        53: "РњРѕСЂРѕСЃСЊ",
        55: "РњРѕСЂРѕСЃСЊ",
        56: "Р›РµРґСЏРЅР°СЏ РјРѕСЂРѕСЃСЊ",
        57: "Р›РµРґСЏРЅР°СЏ РјРѕСЂРѕСЃСЊ",
        61: "Р”РѕР¶РґСЊ",
        63: "Р”РѕР¶РґСЊ",
        65: "Р”РѕР¶РґСЊ",
        66: "Р›РµРґСЏРЅРѕР№ РґРѕР¶РґСЊ",
        67: "Р›РµРґСЏРЅРѕР№ РґРѕР¶РґСЊ",
        71: "РЎРЅРµРі",
        73: "РЎРЅРµРі",
        75: "РЎРЅРµРі",
        77: "РЎРЅРµР¶РЅР°СЏ РєСЂСѓРїР°",
        80: "Р›РёРІРµРЅСЊ",
        81: "Р›РёРІРµРЅСЊ",
        82: "Р›РёРІРµРЅСЊ",
        85: "РЎРЅРµРіРѕРїР°Рґ",
        86: "РЎРЅРµРіРѕРїР°Рґ",
        95: "Р“СЂРѕР·Р°",
        96: "Р“СЂРѕР·Р° СЃ РіСЂР°РґРѕРј",
        99: "Р“СЂРѕР·Р° СЃ РіСЂР°РґРѕРј",
    }
    return mapping.get(code, "Р‘РµР· СѓС‚РѕС‡РЅРµРЅРёСЏ")


def _select_weather_location(profile: dict[str, Any]) -> tuple[str, float, float, str, int, bool]:
    location_name = _to_clean_text(profile.get("weather_location_name")) or "РњРѕСЃРєРІР°"
    latitude = _to_float(profile.get("weather_latitude"), 55.7558)
    longitude = _to_float(profile.get("weather_longitude"), 37.6176)
    timezone_name = _to_clean_text(profile.get("weather_timezone")) or "Europe/Moscow"
    refresh_minutes = _to_int(profile.get("weather_refresh_minutes"), 60, min_value=10, max_value=720)
    auto_enabled = _to_bool(profile.get("weather_auto_enabled"), True)
    return location_name, latitude, longitude, timezone_name, refresh_minutes, auto_enabled


async def _fetch_weather_text(
    *,
    location_name: str,
    latitude: float,
    longitude: float,
    timezone_name: str,
) -> str | None:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "timezone": timezone_name,
    }

    try:
        async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
            response = await client.get(OPEN_METEO_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    current = payload.get("current")
    if not isinstance(current, dict):
        return None

    temperature = _format_float_compact(current.get("temperature_2m"))
    if not temperature:
        return None
    apparent = _format_float_compact(current.get("apparent_temperature"))
    wind = _format_float_compact(current.get("wind_speed_10m"))
    weather_code_raw = current.get("weather_code")
    weather_code = int(weather_code_raw) if isinstance(weather_code_raw, (int, float)) else -1

    parts = [f"{location_name}: {temperature}В°C", _weather_code_to_ru(weather_code)]
    if apparent:
        parts.append(f"РѕС‰СѓС‰Р°РµС‚СЃСЏ РєР°Рє {apparent}В°C")
    if wind:
        parts.append(f"РІРµС‚РµСЂ {wind} Рј/СЃ")

    updated = _format_weather_updated_time(current.get("time"), timezone_name)
    if updated:
        parts.append(f"РѕР±РЅРѕРІР»РµРЅРѕ {updated}")

    return ", ".join(parts)


def _resolve_vk_credentials(profile: dict[str, Any], settings: Settings) -> tuple[int | None, str]:
    profile_user_id_raw = profile.get("vk_user_id")
    profile_user_id: int | None = None
    if isinstance(profile_user_id_raw, int):
        profile_user_id = profile_user_id_raw if profile_user_id_raw > 0 else None
    elif isinstance(profile_user_id_raw, str):
        raw = profile_user_id_raw.strip()
        if raw.isdigit():
            parsed = int(raw)
            profile_user_id = parsed if parsed > 0 else None

    profile_token = _to_clean_text(profile.get("vk_access_token"))
    if profile_user_id and profile_token:
        return profile_user_id, profile_token

    return settings.vk_user_id, settings.vk_access_token


async def _fetch_vk_now_playing_resolved(
    *,
    user_id: int | None,
    access_token: str,
    api_version: str,
) -> str | None:
    if not access_token or not user_id:
        return None

    params = {
        "user_id": user_id,
        "access_token": access_token,
        "v": api_version or "5.199",
    }

    try:
        async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
            response = await client.get(VK_STATUS_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return None

    if not isinstance(payload, dict) or "error" in payload:
        return None

    result = payload.get("response")
    if not isinstance(result, dict):
        return None

    audio = result.get("audio")
    if isinstance(audio, dict):
        artist = _to_clean_text(audio.get("artist"))
        title = _to_clean_text(audio.get("title"))
        if artist and title:
            return f"{artist} - {title}"
        if title:
            return title
        if artist:
            return artist

    status_text = _to_clean_text(result.get("text"))
    return status_text or None


async def sync_profile_now_playing_from_heartbeat(
    session: AsyncSession,
    settings: Settings,
    heartbeat_timeout_minutes: int,
) -> bool:
    profile_path = Path(settings.profile_json_path)
    profile = ensure_profile_exists(profile_path)
    if not _to_bool(profile.get("now_listening_auto_enabled"), True):
        return False

    now = _now_utc()
    mode = _resolve_now_playing_source(profile, settings)
    current_text = _to_clean_text(profile.get("now_listening_text"))
    current_updated_at = _parse_iso_datetime(profile.get("now_listening_updated_at"))
    changed = _to_clean_text(profile.get("now_listening_source")) != mode

    if mode == "vk":
        refresh_minutes = _to_int(settings.vk_now_playing_refresh_minutes, 2, min_value=1, max_value=120)
        vk_user_id, vk_access_token = _resolve_vk_credentials(profile, settings)
        should_refresh = (
            current_updated_at is None
            or (now - current_updated_at) >= timedelta(minutes=refresh_minutes)
            or not current_text
            or current_text == "VK: РЅРµС‚ РґР°РЅРЅС‹С…"
        )
        if should_refresh:
            fetched = await _fetch_vk_now_playing_resolved(
                user_id=vk_user_id,
                access_token=vk_access_token,
                api_version=settings.vk_api_version or "5.199",
            )
            new_text = fetched or "VK: РЅРµС‚ РґР°РЅРЅС‹С…"
            if new_text != current_text:
                profile["now_listening_text"] = new_text
                changed = True
            profile["now_listening_updated_at"] = now.isoformat()
            changed = True

    elif mode == "iphone":
        stale_minutes = _to_int(settings.iphone_now_playing_stale_minutes, 180, min_value=5, max_value=1440)
        is_stale = (
            current_updated_at is None
            or (now - current_updated_at) >= timedelta(minutes=stale_minutes)
        )
        new_text = current_text or "РЎРµР№С‡Р°СЃ РЅРёС‡РµРіРѕ РЅРµ РёРіСЂР°РµС‚"
        if is_stale:
            new_text = "iPhone: РЅРµС‚ СЃРІРµР¶РёС… РґР°РЅРЅС‹С…"
        if new_text != current_text:
            profile["now_listening_text"] = new_text
            changed = True

    else:
        source = await session.scalar(
            select(HeartbeatSource)
            .where(HeartbeatSource.source_type == "PC_AGENT")
            .order_by(HeartbeatSource.last_seen_at.desc())
            .limit(1)
        )

        new_text = "РќРµС‚ РґР°РЅРЅС‹С… СЃ РџРљ"
        if source is not None:
            payload = source.last_payload or {}
            now_playing = _to_clean_text(payload.get("now_playing"))
            activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
            activity_title = _to_clean_text(activity.get("title")) if isinstance(activity, dict) else ""
            activity_text = _to_clean_text(activity.get("text")) if isinstance(activity, dict) else ""
            active_app = _to_clean_text(payload.get("active_app"))
            now_playing_last_seen = _parse_iso_datetime(payload.get("now_playing_last_seen_at"))
            age_sec = int((now - _ensure_utc(source.last_seen_at)).total_seconds())
            is_stale = age_sec > max(60, heartbeat_timeout_minutes * 60)
            now_playing_ttl_sec = max(heartbeat_timeout_minutes * 60 * 3, 45 * 60)
            now_playing_stale = False
            if now_playing_last_seen is not None:
                now_playing_stale = int((now - now_playing_last_seen).total_seconds()) > now_playing_ttl_sec

            normalized_now_playing = normalize_track_input(now_playing) if now_playing else ""
            if normalized_now_playing and source.is_online and not is_stale and not now_playing_stale:
                new_text = normalized_now_playing
            elif source.is_online and not is_stale:
                fallback = ""
                for candidate in (activity_title, active_app, activity_text):
                    normalized_candidate = normalize_track_input(candidate)
                    if normalized_candidate:
                        fallback = normalized_candidate
                        break
                if fallback:
                    new_text = fallback
                else:
                    new_text = "Сейчас ничего не играет"
            else:
                new_text = f"{source.source_name}: РЅРµ РІ СЃРµС‚Рё"

        if new_text != current_text:
            profile["now_listening_text"] = new_text
            profile["now_listening_updated_at"] = now.isoformat()
            changed = True

    profile["now_listening_source"] = mode
    if not changed:
        return False
    save_profile(profile_path, profile)
    return True


def set_profile_now_playing_source(settings: Settings, source: str) -> tuple[bool, str]:
    profile_path = Path(settings.profile_json_path)
    profile = ensure_profile_exists(profile_path)
    normalized = _normalize_now_playing_source(source, _normalize_now_playing_source(settings.now_playing_source_default))
    if _to_clean_text(profile.get("now_listening_source")) == normalized:
        return False, normalized
    profile["now_listening_source"] = normalized
    save_profile(profile_path, profile)
    return True, normalized


def update_profile_now_playing_external(settings: Settings, text: str, source: str = "iphone") -> bool:
    profile_path = Path(settings.profile_json_path)
    profile = ensure_profile_exists(profile_path)

    normalized_source = _normalize_now_playing_source(source, "iphone")
    cleaned_text = _to_clean_text(text)
    normalized_text = normalize_track_input(cleaned_text) if cleaned_text else ""
    if normalized_text:
        cleaned_text = normalized_text
    elif not cleaned_text:
        cleaned_text = "РЎРµР№С‡Р°СЃ РЅРёС‡РµРіРѕ РЅРµ РёРіСЂР°РµС‚"
    else:
        cleaned_text = "РЎРµР№С‡Р°СЃ РЅРёС‡РµРіРѕ РЅРµ РёРіСЂР°РµС‚"

    changed = False
    if _to_clean_text(profile.get("now_listening_text")) != cleaned_text:
        profile["now_listening_text"] = cleaned_text
        changed = True

    if _to_clean_text(profile.get("now_listening_source")) != normalized_source:
        profile["now_listening_source"] = normalized_source
        changed = True

    profile["now_listening_updated_at"] = _now_utc().isoformat()
    changed = True
    save_profile(profile_path, profile)
    return changed


async def sync_profile_weather(settings: Settings, *, force: bool = False) -> bool:
    profile_path = Path(settings.profile_json_path)
    profile = ensure_profile_exists(profile_path)
    location_name, latitude, longitude, timezone_name, refresh_minutes, auto_enabled = _select_weather_location(profile)
    if not auto_enabled:
        return False

    now = _now_utc()
    last_updated = _parse_iso_datetime(profile.get("weather_updated_at"))
    if not force and last_updated is not None and (now - last_updated) < timedelta(minutes=refresh_minutes):
        return False

    weather_text = await _fetch_weather_text(
        location_name=location_name,
        latitude=latitude,
        longitude=longitude,
        timezone_name=timezone_name,
    )
    if not weather_text:
        return False

    profile["weather_text"] = weather_text
    profile["weather_updated_at"] = now.isoformat()
    save_profile(profile_path, profile)
    return True


