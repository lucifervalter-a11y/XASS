from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PwaPairToken

PAIR_TOKEN_PREFIX = "xpw_"
DEFAULT_TTL_MINUTES = 10


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class PwaPairIssueResult:
    token: str
    expires_at: datetime
    ttl_minutes: int


class PwaPairingError(ValueError):
    pass


async def issue_pwa_pair_token(
    session: AsyncSession,
    *,
    actor_user_id: int,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> PwaPairIssueResult:
    now = _now_utc()
    ttl = max(1, min(int(ttl_minutes), 30))

    # Only the newest link remains usable. This makes an accidentally shared
    # older link harmless as soon as the owner creates a replacement.
    await session.execute(
        update(PwaPairToken)
        .where(PwaPairToken.created_by_user_id == actor_user_id, PwaPairToken.is_active.is_(True))
        .values(is_active=False)
    )

    token = ""
    token_hash = ""
    for _ in range(10):
        token = f"{PAIR_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        token_hash = _hash_token(token)
        exists = await session.scalar(select(PwaPairToken.id).where(PwaPairToken.token_hash == token_hash))
        if exists is None:
            break
    else:
        raise RuntimeError("Failed to generate PWA pair token")

    expires_at = now + timedelta(minutes=ttl)
    session.add(
        PwaPairToken(
            token_hash=token_hash,
            token_hint=f"{token[:7]}...{token[-4:]}",
            created_by_user_id=actor_user_id,
            is_active=True,
            expires_at=expires_at,
        )
    )
    await session.commit()
    return PwaPairIssueResult(token=token, expires_at=expires_at, ttl_minutes=ttl)


async def consume_pwa_pair_token(session: AsyncSession, token: str) -> int:
    clean = (token or "").strip()
    if not clean.startswith(PAIR_TOKEN_PREFIX) or len(clean) < 32:
        raise PwaPairingError("Ссылка подключения неверна")

    now = _now_utc()
    token_hash = _hash_token(clean)
    row = await session.scalar(
        select(PwaPairToken).where(
            PwaPairToken.token_hash == token_hash,
            PwaPairToken.is_active.is_(True),
            PwaPairToken.expires_at > now,
        )
    )
    if row is None:
        raise PwaPairingError("Ссылка подключения уже использована или истекла")

    # The conditional update prevents two simultaneous requests from consuming
    # the same one-time link.
    result = await session.execute(
        update(PwaPairToken)
        .where(PwaPairToken.id == row.id, PwaPairToken.is_active.is_(True))
        .values(is_active=False, consumed_at=now)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise PwaPairingError("Ссылка подключения уже использована")
    await session.commit()
    return int(row.created_by_user_id)
