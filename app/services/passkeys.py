from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PasskeyCredential


CHALLENGE_TTL_SEC = 5 * 60


@dataclass(slots=True)
class PendingChallenge:
    challenge: bytes
    owner_user_id: int
    rp_id: str
    origin: str
    purpose: str
    expires_at: float


_pending: dict[str, PendingChallenge] = {}


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _transaction(challenge: bytes, owner_user_id: int, rp_id: str, origin: str, purpose: str) -> str:
    now = time.time()
    for key, item in list(_pending.items()):
        if item.expires_at <= now:
            _pending.pop(key, None)
    token = secrets.token_urlsafe(32)
    _pending[token] = PendingChallenge(
        challenge=challenge,
        owner_user_id=owner_user_id,
        rp_id=rp_id,
        origin=origin,
        purpose=purpose,
        expires_at=now + CHALLENGE_TTL_SEC,
    )
    return token


def _consume(token: str, purpose: str | None = None) -> PendingChallenge:
    item = _pending.pop((token or "").strip(), None)
    if item is None or item.expires_at <= time.time():
        raise ValueError("Проверка истекла. Повторите действие.")
    if purpose is not None and item.purpose != purpose:
        raise ValueError("Назначение проверки не совпадает")
    return item


def transaction_owner(token: str, purpose: str) -> int:
    item = _pending.get((token or "").strip())
    if item is None or item.expires_at <= time.time() or item.purpose != purpose:
        raise ValueError("Проверка истекла. Повторите действие.")
    return item.owner_user_id


def _library() -> dict[str, Any]:
    try:
        from webauthn import (
            generate_authentication_options,
            generate_registration_options,
            options_to_json,
            verify_authentication_response,
            verify_registration_response,
        )
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
    except ImportError as exc:
        raise RuntimeError("Поддержка Passkey ещё не установлена на сервере") from exc
    return locals()


async def count_credentials(session: AsyncSession, owner_user_id: int) -> int:
    rows = list(
        await session.scalars(
            select(PasskeyCredential).where(PasskeyCredential.owner_user_id == owner_user_id)
        )
    )
    return len(rows)


async def list_credentials(session: AsyncSession, owner_user_id: int) -> list[PasskeyCredential]:
    return list(
        await session.scalars(
            select(PasskeyCredential)
            .where(PasskeyCredential.owner_user_id == owner_user_id)
            .order_by(PasskeyCredential.created_at.desc())
        )
    )


async def delete_credential(
    session: AsyncSession,
    *,
    owner_user_id: int,
    credential_id: int,
) -> PasskeyCredential | None:
    credential = await session.scalar(
        select(PasskeyCredential).where(
            PasskeyCredential.id == int(credential_id),
            PasskeyCredential.owner_user_id == int(owner_user_id),
        )
    )
    if credential is None:
        return None
    await session.delete(credential)
    await session.commit()
    return credential


async def registration_options(
    session: AsyncSession,
    *,
    owner_user_id: int,
    owner_name: str,
    rp_id: str,
    origin: str,
) -> dict[str, Any]:
    lib = _library()
    credentials = await list_credentials(session, owner_user_id)
    descriptors = [
        lib["PublicKeyCredentialDescriptor"](
            id=_decode(item.credential_id),
        )
        for item in credentials
    ]
    selection = lib["AuthenticatorSelectionCriteria"](
        resident_key=lib["ResidentKeyRequirement"].PREFERRED,
        user_verification=lib["UserVerificationRequirement"].REQUIRED,
    )
    options = lib["generate_registration_options"](
        rp_id=rp_id,
        rp_name="XASS",
        user_id=str(owner_user_id).encode("utf-8"),
        user_name=owner_name or f"owner-{owner_user_id}",
        user_display_name=owner_name or "Владелец XASS",
        authenticator_selection=selection,
        exclude_credentials=descriptors,
    )
    return {
        "transaction": _transaction(options.challenge, owner_user_id, rp_id, origin, "register"),
        "options": json.loads(lib["options_to_json"](options)),
    }


async def complete_registration(
    session: AsyncSession,
    *,
    transaction: str,
    credential: dict[str, Any],
    name: str,
) -> PasskeyCredential:
    pending = _consume(transaction, "register")
    lib = _library()
    result = lib["verify_registration_response"](
        credential=credential,
        expected_challenge=pending.challenge,
        expected_rp_id=pending.rp_id,
        expected_origin=pending.origin,
        require_user_verification=True,
    )
    encoded_id = _encode(result.credential_id)
    existing = await session.scalar(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == encoded_id)
    )
    transports = credential.get("response", {}).get("transports") or []
    if existing is None:
        existing = PasskeyCredential(
            owner_user_id=pending.owner_user_id,
            credential_id=encoded_id,
            public_key=_encode(result.credential_public_key),
            sign_count=int(result.sign_count or 0),
            name=(name or "Face ID / Passkey")[:120],
            transports=[str(item) for item in transports][:12],
        )
        session.add(existing)
    else:
        existing.public_key = _encode(result.credential_public_key)
        existing.sign_count = int(result.sign_count or 0)
        existing.name = (name or existing.name or "Face ID / Passkey")[:120]
        existing.transports = [str(item) for item in transports][:12]
    existing.device_type = str(getattr(result, "credential_device_type", "") or "")[:64] or None
    existing.backed_up = bool(getattr(result, "credential_backed_up", False))
    await session.commit()
    await session.refresh(existing)
    return existing


async def authentication_options(
    session: AsyncSession,
    *,
    owner_user_id: int,
    rp_id: str,
    origin: str,
    purpose: str,
) -> dict[str, Any]:
    lib = _library()
    credentials = await list_credentials(session, owner_user_id)
    if not credentials:
        raise ValueError("Сначала добавьте Face ID / Passkey после входа через Telegram")
    descriptors = [
        lib["PublicKeyCredentialDescriptor"](
            id=_decode(item.credential_id),
        )
        for item in credentials
    ]
    options = lib["generate_authentication_options"](
        rp_id=rp_id,
        allow_credentials=descriptors,
        user_verification=lib["UserVerificationRequirement"].REQUIRED,
    )
    return {
        "transaction": _transaction(options.challenge, owner_user_id, rp_id, origin, purpose),
        "options": json.loads(lib["options_to_json"](options)),
    }


async def complete_authentication(
    session: AsyncSession,
    *,
    transaction: str,
    credential: dict[str, Any],
) -> tuple[PasskeyCredential, str]:
    pending = _consume(transaction)
    raw_id = str(credential.get("id") or credential.get("rawId") or "").strip()
    stored = await session.scalar(
        select(PasskeyCredential).where(
            PasskeyCredential.owner_user_id == pending.owner_user_id,
            PasskeyCredential.credential_id == raw_id,
        )
    )
    if stored is None:
        raise ValueError("Этот Passkey не зарегистрирован в XASS")
    lib = _library()
    result = lib["verify_authentication_response"](
        credential=credential,
        expected_challenge=pending.challenge,
        expected_rp_id=pending.rp_id,
        expected_origin=pending.origin,
        credential_public_key=_decode(stored.public_key),
        credential_current_sign_count=int(stored.sign_count or 0),
        require_user_verification=True,
    )
    from datetime import datetime, timezone

    stored.sign_count = int(result.new_sign_count or stored.sign_count or 0)
    stored.device_type = str(getattr(result, "credential_device_type", "") or "")[:64] or stored.device_type
    stored.backed_up = bool(getattr(result, "credential_backed_up", stored.backed_up))
    stored.last_used_at = datetime.now(timezone.utc)
    await session.commit()
    return stored, pending.purpose
