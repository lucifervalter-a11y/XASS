"""Import the owner's VK audio library into the private XASS catalog.

Official VK apps often omit the audio scope. Tokens from a client that still
exposes audio.get (for example Kate Mobile / an app with audio permission)
work. HLS/m3u8 URLs are skipped — the PC player needs a real file.
"""
from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.services.music_ingest import IngestError, ingest_path, private_root
from app.services.profile_editor import load_profile

logger = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method"
PAGE = 200
MAX_PAGES = 30
MAX_DOWNLOAD = 80 * 1024 * 1024
BATCH = 25


class VkImportError(ValueError):
    pass


def resolve_vk_credentials(settings) -> tuple[str, int]:
    profile = load_profile(Path(settings.profile_json_path))
    token = str(getattr(settings, "vk_access_token", "") or "").strip() or str(profile.get("vk_access_token") or "").strip()
    raw_uid = getattr(settings, "vk_user_id", None) or profile.get("vk_user_id")
    try:
        user_id = int(raw_uid or 0)
    except (TypeError, ValueError):
        user_id = 0
    if len(token) < 20:
        raise VkImportError("ВКонтакте не подключён. Откройте карточку VK и выдайте доступ к аудио.")
    if user_id <= 0:
        raise VkImportError("Нет vk_user_id. Подключите VK ещё раз.")
    return token, user_id


async def vk_call(token: str, method: str, **params: Any) -> Any:
    query = {k: v for k, v in params.items() if v is not None}
    query.update(access_token=token, v="5.199")
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        response = await client.get(f"{VK_API}/{method}", params=query)
        response.raise_for_status()
        payload = response.json()
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = int(error.get("error_code") or 0)
        message = str(error.get("error_msg") or "VK API error")
        if code in {5, 15, 201, 1133}:
            raise VkImportError(
                "VK не отдал аудиотеку. Нужен токен с правом audio "
                "(приложение вроде Kate Mobile, client_id 2685278) или повторное подключение."
            )
        raise VkImportError(f"VK: {message}")
    return payload.get("response") if isinstance(payload, dict) else payload


def _direct_url(item: dict) -> str:
    raw = str(item.get("url") or "").strip()
    if not raw or raw.startswith("http://"):
        return ""
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    if "vk.com" not in host and "vk-cdn" not in host and "vkuseraudio" not in host and "userapi.com" not in host:
        return ""
    if ".m3u8" in raw or "index.m3u8" in raw:
        return ""
    return raw


def _safe_name(title: str, artist: str) -> str:
    base = re.sub(r"[^\w\s.-]+", "", f"{artist} - {title}".strip(), flags=re.U)
    base = re.sub(r"\s+", " ", base).strip(" .")[:80] or "vk-track"
    return base + ".mp3"


async def list_vk_audio(token: str, owner_id: int, *, offset: int = 0, limit: int = BATCH) -> dict[str, Any]:
    collected: list[dict[str, Any]] = []
    total = 0
    cursor = max(0, int(offset))
    remaining = max(1, min(int(limit), BATCH))
    pages = 0
    while remaining > 0 and pages < MAX_PAGES:
        pages += 1
        response = await vk_call(token, "audio.get", owner_id=owner_id, offset=cursor, count=min(PAGE, remaining + 20))
        if isinstance(response, dict):
            total = int(response.get("count") or total)
            items = response.get("items") or []
        elif isinstance(response, list):
            items = response
        else:
            items = []
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            cursor += 1
            title = str(item.get("title") or "").strip()[:240]
            artist = str(item.get("artist") or "").strip()[:240]
            vk_id = f"{item.get('owner_id')}_{item.get('id')}"
            url = _direct_url(item)
            collected.append({
                "vk_id": vk_id,
                "title": title or "Без названия",
                "artist": artist,
                "duration": int(item.get("duration") or 0),
                "url": url,
                "importable": bool(url),
            })
            if len(collected) >= remaining:
                break
        if len(items) < min(PAGE, remaining + 20):
            break
    return {"total": total or cursor, "offset": cursor, "items": collected}


async def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, headers={"User-Agent": "XASS/0.17"}) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            ctype = str(response.headers.get("content-type") or "").lower()
            if "mpegurl" in ctype or "m3u8" in ctype:
                raise VkImportError("VK отдал поток HLS, файл скачать нельзя")
            written = 0
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(64 * 1024):
                    written += len(chunk)
                    if written > MAX_DOWNLOAD:
                        raise VkImportError("Файл VK слишком большой")
                    handle.write(chunk)
            if written < 1024:
                raise VkImportError("Пустой ответ VK")


async def import_vk_batch(settings, session_factory, *, offset: int = 0, limit: int = BATCH) -> dict[str, Any]:
    token, owner_id = resolve_vk_credentials(settings)
    listing = await list_vk_audio(token, owner_id, offset=offset, limit=limit)
    root = private_root(settings)
    staging = root / ".vk-ingest"
    staging.mkdir(parents=True, exist_ok=True)
    added: list[int] = []
    existing: list[int] = []
    errors: list[str] = []
    skipped = 0
    for item in listing["items"]:
        if not item["importable"]:
            skipped += 1
            errors.append(f"{item['artist']} — {item['title']}: нет прямой ссылки (HLS или нет права audio)")
            continue
        name = _safe_name(item["title"], item["artist"])
        temp = staging / (secrets.token_hex(12) + ".mp3")
        try:
            await _download(item["url"], temp)
            imported = await ingest_path(
                settings,
                session_factory,
                temp,
                name,
                title=item["title"],
                artist=item["artist"],
            )
            added.extend(imported.added)
            existing.extend(imported.existing)
            errors.extend(imported.errors)
        except (VkImportError, IngestError, httpx.HTTPError, OSError) as exc:
            errors.append(f"{item['artist']} — {item['title']}: {exc}")
        finally:
            temp.unlink(missing_ok=True)
    return {
        "ok": True,
        "offset": listing["offset"],
        "total": listing["total"],
        "scanned": len(listing["items"]),
        "added": len(added),
        "duplicates": len(existing),
        "skipped": skipped,
        "added_ids": added,
        "existing_ids": existing,
        "errors": errors[:40],
        "done": listing["offset"] >= listing["total"] or not listing["items"],
    }
