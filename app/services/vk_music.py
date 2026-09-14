"""VK Music import: fetch library metadata, decode encrypted audio URLs, download into the private library.

Official VK audio API is closed to third-party apps, so we use a Kate-Mobile-style
token (vk_access_token) with audio.get / audio.getById. Encrypted "audio_api_unavailable"
URLs are decoded with the vendored vk_api algorithm. Downloads are bounded, streamed
to private staging, then ingested through the existing SHA-dedup pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from app.services.music_ingest import IngestError, clean_text, ingest_path, private_root
from app.services.vk_audio_url_decoder import decode_audio_url

logger = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method"
VK_UA = ("Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

_AUDIO_ID_RE = re.compile(r"^(?:https?://(?:m\.)?vk\.com/audio)?(-?\d+)_(\d+)(?:_([a-f0-9]+))?$", re.I)
_UNAVAILABLE_RE = re.compile(r"audio_api_unavailable", re.I)


class VkMusicError(IngestError):
    """Safe, user-facing VK import error."""


@dataclass
class VkTrack:
    owner_id: int
    audio_id: int
    access_key: str = ""
    artist: str = ""
    title: str = ""
    album: str = ""
    duration: int = 0
    url: str = ""
    source: str = "vk"

    @property
    def full_id(self) -> str:
        base = f"{self.owner_id}_{self.audio_id}"
        return f"{base}_{self.access_key}" if self.access_key else base

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "audio_id": self.audio_id,
            "access_key": self.access_key,
            "artist": self.artist,
            "title": self.title,
            "album": self.album,
            "duration": self.duration,
            "has_url": bool(self.url),
            "source": self.source,
        }


def _vk_error(body: dict) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("error_msg") or err.get("error_code") or "vk_error")
    return "vk_error"


async def _vk_call(client: httpx.AsyncClient, method: str, token: str, params: dict,
                   api_version: str = "5.199") -> dict:
    payload = {"access_token": token, "v": api_version, **params}
    try:
        resp = await client.post(f"{VK_API}/{method}", data=payload)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as exc:
        raise VkMusicError("VK API недоступен") from exc
    if not isinstance(body, dict) or "error" in body:
        raise VkMusicError(_vk_error(body))
    return body.get("response") or {}


async def vk_get_library(token: str, owner_id: int | None = None, *,
                         api_version: str = "5.199", limit: int = 200,
                         offset: int = 0, client: httpx.AsyncClient | None = None) -> list[VkTrack]:
    """Fetch a page of the user's audio library (metadata + encrypted url)."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False,
                                         headers={"User-Agent": VK_UA})
    try:
        params: dict[str, Any] = {"count": max(1, min(limit, 200)), "offset": max(0, offset)}
        if owner_id:
            params["owner_id"] = owner_id
        data = await _vk_call(client, "audio.get", token, params, api_version)
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        out: list[VkTrack] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            out.append(VkTrack(
                owner_id=int(raw.get("owner_id") or 0),
                audio_id=int(raw.get("id") or 0),
                access_key=str(raw.get("access_key") or ""),
                artist=clean_text(raw.get("artist")),
                title=clean_text(raw.get("title")),
                album=clean_text((raw.get("album") or {}).get("title") if isinstance(raw.get("album"), dict) else ""),
                duration=int(raw.get("duration") or 0),
                url=str(raw.get("url") or ""),
            ))
        return out
    finally:
        if owned:
            await client.aclose()


async def vk_resolve_urls(token: str, tracks: list[VkTrack], *,
                          api_version: str = "5.199",
                          client: httpx.AsyncClient | None = None) -> list[VkTrack]:
    """audio.getById returns the playable (possibly encrypted) URL for a batch of tracks."""
    if not tracks:
        return tracks
    owned = client is None
    client = client or httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False,
                                         headers={"User-Agent": VK_UA})
    try:
        for i in range(0, len(tracks), 200):
            chunk = tracks[i:i + 200]
            ids = ",".join(t.full_id for t in chunk)
            data = await _vk_call(client, "audio.getById", token,
                                  {"audios": ids}, api_version)
            items = data if isinstance(data, list) else (data.get("items") or [])
            by_id = {}
            for raw in items:
                if isinstance(raw, dict):
                    by_id[(int(raw.get("owner_id") or 0), int(raw.get("id") or 0))] = raw
            for t in chunk:
                raw = by_id.get((t.owner_id, t.audio_id))
                if raw:
                    t.url = str(raw.get("url") or t.url)
                    t.artist = clean_text(raw.get("artist")) or t.artist
                    t.title = clean_text(raw.get("title")) or t.title
                    t.duration = int(raw.get("duration") or t.duration)
        return tracks
    finally:
        if owned:
            await client.aclose()


def decode_vk_url(url: str, user_id: int) -> str:
    """Turn an encrypted audio_api_unavailable URL into a direct CDN link."""
    if not url:
        return ""
    if not _UNAVAILABLE_RE.search(url):
        return url
    try:
        return decode_audio_url(url, user_id)
    except Exception as exc:
        logger.warning("vk url decode failed: %s", exc)
        return ""


async def _download_stream(client: httpx.AsyncClient, url: str, dest, *,
                           max_bytes: int, min_free: int, root) -> int:
    from app.services.music_ingest import _new_file, _require_space
    total = 0
    async with client.stream("GET", url, follow_redirects=True) as resp:
        if resp.status_code >= 400:
            raise VkMusicError(f"VK CDN вернул {resp.status_code}")
        with _new_file(dest) as out:
            async for chunk in resp.aiter_bytes(128 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise VkMusicError("Трек превышает допустимый размер")
                _require_space(root, len(chunk), min_free)
                out.write(chunk)
    return total


async def import_vk_library(settings, session_factory, *, token: str | None = None,
                            owner_id: int | None = None, limit: int = 50,
                            offset: int = 0, max_tracks: int = 200) -> dict:
    """Pull VK metadata, resolve URLs, download each track, ingest into the library."""
    token = (token or getattr(settings, "vk_access_token", "") or "").strip()
    if not token:
        raise VkMusicError("VK не подключён: задай vk_access_token в .env")
    vk_uid = owner_id or getattr(settings, "vk_user_id", None)
    api_version = getattr(settings, "vk_api_version", "5.199") or "5.199"
    root = private_root(settings)
    max_bytes = getattr(settings, "music_max_upload_bytes", 128 * 1024 * 1024)
    min_free = getattr(settings, "music_min_free_bytes", 512 * 1024 * 1024)

    async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False,
                                 headers={"User-Agent": VK_UA}) as client:
        tracks = await vk_get_library(token, vk_uid, api_version=api_version,
                                      limit=min(limit, max_tracks), offset=offset, client=client)
        if not tracks:
            return {"ok": True, "added": 0, "existing": 0, "skipped": 0, "errors": [],
                    "tracks": [], "message": "В VK-библиотеке нет треков на этой странице"}
        tracks = await vk_resolve_urls(token, tracks, api_version=api_version, client=client)

    added, existing, skipped, errors, ordered = [], [], 0, [], []
    headers = {"User-Agent": VK_UA, "Referer": "https://vk.com/"}
    async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=True,
                                 headers=headers) as dl:
        for t in tracks[:max_tracks]:
            name = f"{clean_text(t.artist) or 'Unknown'} - {clean_text(t.title) or 'Track'}.mp3"
            name = re.sub(r"[^\w\u0400-\u04FF.\- ]+", "_", name)[:200] + ".mp3"
            url = decode_vk_url(t.url, vk_uid or t.owner_id)
            if not url:
                errors.append(f"{name}: нет прямой ссылки (трек заблокирован или приватный)")
                skipped += 1
                continue
            staging = root / ".vk-ingest"
            staging.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest = staging / f"vk-{t.owner_id}-{t.audio_id}.mp3"
            try:
                size = await _download_stream(dl, url, dest, max_bytes=max_bytes,
                                              min_free=min_free, root=root)
                if size < 1024:
                    raise VkMusicError("Скачанный файл слишком мал")
                result = await ingest_path(settings, session_factory, dest, name,
                                           title=t.title, artist=t.artist)
                added.extend(result.added)
                existing.extend(result.existing)
                ordered.extend(result.ordered)
                errors.extend(result.errors)
                skipped += result.skipped
            except IngestError as exc:
                errors.append(f"{name}: {exc}")
                skipped += 1
            except Exception as exc:
                logger.exception("vk import failed for %s", name)
                errors.append(f"{name}: ошибка импорта")
                skipped += 1
            finally:
                dest.unlink(missing_ok=True)
    return {"ok": True, "added": len(added), "existing": len(existing), "skipped": skipped,
            "errors": errors[:20], "tracks": ordered[:50],
            "message": f"Импортировано {len(added)} новых, {len(existing)} уже были"}
