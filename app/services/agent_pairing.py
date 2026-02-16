import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import SourceType
from app.models import AgentCredential, AgentPairCode, HeartbeatSource

PAIR_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_pair_code(raw: str) -> str:
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def _format_pair_code(normalized: str) -> str:
    if len(normalized) <= 4:
        return normalized
    chunks = [normalized[i : i + 4] for i in range(0, len(normalized), 4)]
    return "-".join(chunks)


def normalize_source_name(raw: str | None, fallback: str = "pc-agent") -> str:
    cleaned = " ".join((raw or "").strip().split())
    if not cleaned:
        cleaned = fallback
    return cleaned[:128]


def _generate_pair_code(length: int = 8) -> str:
    normalized_length = max(6, min(24, int(length)))
    raw = "".join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(normalized_length))
    return _format_pair_code(raw)


def generate_agent_api_key() -> str:
    return f"ag_{secrets.token_urlsafe(24)}"


def _key_hint(api_key: str) -> str:
    if len(api_key) < 10:
        return "hidden"
    return f"{api_key[:4]}...{api_key[-4:]}"


async def _source_name_exists(session: AsyncSession, source_name: str) -> bool:
    heartbeat_exists = await session.scalar(select(HeartbeatSource.id).where(HeartbeatSource.source_name == source_name))
    if heartbeat_exists is not None:
        return True
    credential_exists = await session.scalar(select(AgentCredential.id).where(AgentCredential.source_name == source_name))
    return credential_exists is not None


async def ensure_unique_source_name(session: AsyncSession, source_name: str) -> str:
    base = normalize_source_name(source_name)
    if not await _source_name_exists(session, base):
        return base

    suffix = 2
    while suffix < 1000:
        ending = f"-{suffix}"
        candidate = f"{base[: max(1, 128 - len(ending))]}{ending}"
        if not await _source_name_exists(session, candidate):
            return candidate
        suffix += 1
    raise ValueError("Failed to allocate unique source_name for agent")


@dataclass(slots=True)
class PairCodeIssueResult:
    code: str
    expires_at: datetime
    ttl_minutes: int


@dataclass(slots=True)
class PairClaimResult:
    source_name: str
    source_type: SourceType
    agent_api_key: str
    issued_at: datetime
    key_hint: str


@dataclass(slots=True)
class AgentAuthResult:
    mode: str  # "global" | "issued"
    source_name: str | None = None
    credential_id: int | None = None


class PairingError(ValueError):
    pass


async def issue_pair_code(
    session: AsyncSession,
    *,
    actor_user_id: int | None,
    ttl_minutes: int = 15,
    code_length: int = 8,
) -> PairCodeIssueResult:
    now = _now_utc()
    ttl = max(1, int(ttl_minutes))

    # Keep only the latest active code to simplify operator UX.
    await session.execute(
        update(AgentPairCode)
        .where(AgentPairCode.is_active.is_(True))
        .values(is_active=False, updated_at=now)
    )

    code = ""
    code_hash = ""
    for _ in range(10):
        code = _generate_pair_code(code_length)
        normalized = _normalize_pair_code(code)
        code_hash = _hash_secret(normalized)
        exists = await session.scalar(select(AgentPairCode.id).where(AgentPairCode.code_hash == code_hash))
        if exists is None:
            break
    else:
        raise RuntimeError("Failed to generate pair code")

    expires_at = now + timedelta(minutes=ttl)
    pair = AgentPairCode(
        code_hash=code_hash,
        code_hint=f"****-{_normalize_pair_code(code)[-4:]}",
        is_active=True,
        max_uses=1,
        used_count=0,
        created_by_user_id=actor_user_id,
        expires_at=expires_at,
    )
    session.add(pair)
    await session.commit()
    return PairCodeIssueResult(code=code, expires_at=expires_at, ttl_minutes=ttl)


async def claim_pair_code_and_issue_key(
    session: AsyncSession,
    *,
    pair_code: str,
    source_name: str | None,
    source_type: SourceType,
) -> PairClaimResult:
    normalized_code = _normalize_pair_code(pair_code)
    if not normalized_code:
        raise PairingError("Pair code is empty")

    now = _now_utc()
    code_hash = _hash_secret(normalized_code)
    pair = await session.scalar(
        select(AgentPairCode).where(
            AgentPairCode.code_hash == code_hash,
            AgentPairCode.is_active.is_(True),
            AgentPairCode.expires_at > now,
        )
    )
    if pair is None:
        raise PairingError("Pair code is invalid or expired")
    if pair.used_count >= pair.max_uses:
        pair.is_active = False
        pair.consumed_at = pair.consumed_at or now
        await session.commit()
        raise PairingError("Pair code already used")

    candidate_name = normalize_source_name(source_name)
    unique_source_name = await ensure_unique_source_name(session, candidate_name)

    api_key = generate_agent_api_key()
    credential = AgentCredential(
        source_name=unique_source_name,
        source_type=source_type.value,
        api_key_hash=_hash_secret(api_key),
        key_hint=_key_hint(api_key),
        is_active=True,
        created_by_user_id=pair.created_by_user_id,
        issued_at=now,
    )
    session.add(credential)

    pair.used_count += 1
    if pair.used_count >= pair.max_uses:
        pair.is_active = False
        pair.consumed_at = now

    await session.commit()
    return PairClaimResult(
        source_name=unique_source_name,
        source_type=source_type,
        agent_api_key=api_key,
        issued_at=now,
        key_hint=credential.key_hint,
    )


async def authenticate_agent_api_key(
    session: AsyncSession,
    *,
    api_key: str | None,
    global_agent_api_key: str,
) -> AgentAuthResult | None:
    key = (api_key or "").strip()
    if not key:
        return None

    if global_agent_api_key and key == global_agent_api_key:
        return AgentAuthResult(mode="global")

    credential = await session.scalar(
        select(AgentCredential).where(
            AgentCredential.api_key_hash == _hash_secret(key),
            AgentCredential.is_active.is_(True),
        )
    )
    if credential is None:
        return None

    credential.last_used_at = _now_utc()
    return AgentAuthResult(mode="issued", source_name=credential.source_name, credential_id=credential.id)
