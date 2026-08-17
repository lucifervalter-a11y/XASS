from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import WebPushSubscription


def push_configured(settings: Settings) -> bool:
    return bool(
        settings.pwa_vapid_public_key.strip()
        and settings.pwa_vapid_private_key.strip()
        and settings.pwa_vapid_subject.strip()
    )


def normalize_subscription(payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(payload.get("endpoint") or "").strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or len(endpoint) > 2048:
        raise ValueError("Некорректный endpoint push-подписки")
    keys = payload.get("keys") if isinstance(payload.get("keys"), dict) else {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not p256dh or not auth or len(p256dh) > 512 or len(auth) > 256:
        raise ValueError("Браузер не передал ключи push-подписки")
    return {"endpoint": endpoint, "expirationTime": payload.get("expirationTime"), "keys": {"p256dh": p256dh, "auth": auth}}


def _endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


async def save_subscription(
    session: AsyncSession,
    *,
    owner_user_id: int,
    payload: dict[str, Any],
    user_agent: str,
) -> WebPushSubscription:
    clean = normalize_subscription(payload)
    digest = _endpoint_hash(clean["endpoint"])
    item = await session.scalar(
        select(WebPushSubscription).where(WebPushSubscription.endpoint_hash == digest)
    )
    if item is None:
        item = WebPushSubscription(owner_user_id=owner_user_id, endpoint_hash=digest)
        session.add(item)
    item.owner_user_id = owner_user_id
    item.subscription = clean
    item.user_agent = user_agent.strip()[:300] or None
    item.active = True
    item.last_error = None
    await session.commit()
    await session.refresh(item)
    return item


async def remove_subscription(session: AsyncSession, *, owner_user_id: int, endpoint: str) -> bool:
    endpoint = endpoint.strip()
    if not endpoint:
        return False
    item = await session.scalar(
        select(WebPushSubscription).where(
            WebPushSubscription.owner_user_id == owner_user_id,
            WebPushSubscription.endpoint_hash == _endpoint_hash(endpoint),
        )
    )
    if item is None:
        return False
    item.active = False
    await session.commit()
    return True


def _send_one(subscription: dict[str, Any], data: str, settings: Settings) -> None:
    from pywebpush import webpush

    webpush(
        subscription_info=subscription,
        data=data,
        vapid_private_key=settings.pwa_vapid_private_key,
        vapid_claims={"sub": settings.pwa_vapid_subject},
        ttl=300,
    )


async def send_push(
    session: AsyncSession,
    settings: Settings,
    *,
    title: str,
    message: str,
    event_type: str,
    priority: str,
    url: str = "/miniapp.php?standalone=1#notifications",
) -> dict[str, int]:
    if not push_configured(settings):
        return {"sent": 0, "failed": 0}
    rows = list(
        await session.scalars(
            select(WebPushSubscription).where(WebPushSubscription.active.is_(True))
        )
    )
    data = json.dumps(
        {
            "title": title[:180] or "XASS",
            "body": message[:1000],
            "event_type": event_type[:96],
            "priority": priority[:16],
            "url": url,
        },
        ensure_ascii=False,
    )
    sent = 0
    failed = 0
    for item in rows:
        try:
            await asyncio.to_thread(_send_one, item.subscription or {}, data, settings)
            item.last_success_at = datetime.now(timezone.utc)
            item.last_error = None
            sent += 1
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            item.last_error = str(exc)[:500]
            if status_code in {404, 410}:
                item.active = False
            failed += 1
    await session.commit()
    return {"sent": sent, "failed": failed}
