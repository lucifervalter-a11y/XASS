"""Native approvals without WebAuthn domain entitlements or cookie-only enrollment.

A fresh Telegram-issued one-time link enrolls a P-256 public key. The iOS private
key remains user-presence protected in Keychain/Secure Enclave. The server checks
signatures, not a client-supplied 'Face ID succeeded' flag.
"""
from __future__ import annotations
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update

from app.db import get_session
from app.models import PwaPairToken
from app.native_models import NativeChallenge, NativeDevice, NativeProof
from app.services.miniapp import MiniAppUser
from app.services.pwa_auth import COOKIE_NAME, SESSION_AGE_SEC, _session_generation, issue_session


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def unb64(value: str) -> bytes:
    if len(value) > 4096:
        raise ValueError("Oversized signature")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def binding_hash(value: dict | None) -> str:
    try:
        encoded = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "Некорректные параметры подтверждения") from exc
    if len(encoded.encode()) > 8192:
        raise HTTPException(400, "Слишком большой запрос подтверждения")
    return digest(encoded)


def public_key(value: str):
    try:
        raw = unb64(value)
        if len(raw) != 65 or raw[0] != 4:
            raise ValueError()
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError as exc:
        raise HTTPException(400, "Некорректный ключ устройства") from exc


def verify_signature(key: str, signature: str, message: str):
    try:
        public_key(key).verify(unb64(signature), message.encode(), ec.ECDSA(hashes.SHA256()))
    except (ValueError, InvalidSignature) as exc:
        raise HTTPException(403, "Подпись устройства не прошла проверку") from exc


class EnrollmentOptions(BaseModel):
    public_key: str = Field(min_length=80, max_length=128)
    pair_token: str = Field(min_length=32, max_length=256)


class EnrollmentVerify(EnrollmentOptions):
    challenge_id: str = Field(min_length=64, max_length=64)
    signature: str = Field(min_length=64, max_length=128)
    device_name: str = Field(default="iPhone", min_length=1, max_length=100)


class ActionOptions(BaseModel):
    device_id: str = Field(min_length=32, max_length=32)
    purpose: str = Field(min_length=1, max_length=256)
    binding: dict = Field(default_factory=dict)


class ActionVerify(BaseModel):
    device_id: str = Field(min_length=32, max_length=32)
    challenge_id: str = Field(min_length=64, max_length=64)
    signature: str = Field(min_length=64, max_length=128)


async def consume_native_proof(session, token, owner_id, purpose, binding, settings) -> bool:
    if not token.startswith("xna_") or len(token) > 128:
        return False
    now = datetime.now(timezone.utc)
    proof = await session.get(NativeProof, digest(token))
    if proof is None or proof.owner_id != owner_id or proof.purpose != purpose or proof.binding_hash != binding_hash(binding):
        return False
    generation = _session_generation(settings)
    device = await session.get(NativeDevice, proof.device_id)
    if device is None or device.revoked or device.owner_id != owner_id or device.generation != generation:
        return False
    # A revocation may commit after the ORM read above. Check liveness again in
    # the same SQL statement that consumes the proof, not from a cached object.
    live_device = select(NativeDevice.id).where(NativeDevice.id == proof.device_id,
        NativeDevice.owner_id == owner_id, NativeDevice.revoked.is_(False),
        NativeDevice.generation == generation).exists()
    result = await session.execute(update(NativeProof).where(NativeProof.id == proof.id,
        NativeProof.used.is_(False), NativeProof.expires_at > now, NativeProof.generation == generation,
        live_device).values(used=True).execution_options(synchronize_session=False))
    # The caller commits this consumption with the actual action. Failed actions
    # roll back; successful actions cannot replay even inside the validity window.
    return result.rowcount == 1


def build_router(settings, require_owner):
    router = APIRouter()

    async def pair_owner(session, token):
        if not token.startswith("xpw_"):
            raise HTTPException(401, "Создайте новую защищённую ссылку в Telegram")
        row = await session.scalar(select(PwaPairToken).where(PwaPairToken.token_hash == digest(token),
            PwaPairToken.is_active.is_(True), PwaPairToken.expires_at > datetime.now(timezone.utc)))
        if row is None or row.created_by_user_id != settings.owner_user_id or not settings.owner_user_id:
            raise HTTPException(401, "Ссылка уже использована или истекла. Создайте новую в Telegram")
        return row

    async def device_for(session, device_id, owner_id):
        device = await session.scalar(select(NativeDevice).where(NativeDevice.id == device_id)
            .execution_options(populate_existing=True))
        if device is None or device.owner_id != owner_id or device.revoked or device.generation != _session_generation(settings):
            raise HTTPException(428, "Подключите подтверждение этого iPhone новой ссылкой из Telegram")
        return device

    async def challenge(session, *, owner_id, kind, key, device_id="", pair_hash="", purpose="", binding=None):
        now = datetime.now(timezone.utc)
        await session.execute(delete(NativeChallenge).where(NativeChallenge.expires_at < now).execution_options(synchronize_session=False))
        await session.execute(delete(NativeProof).where(NativeProof.expires_at < now).execution_options(synchronize_session=False))
        count = await session.scalar(select(func.count()).select_from(NativeChallenge).where(
            NativeChallenge.owner_id == owner_id, NativeChallenge.used.is_(False)))
        if count >= 30:
            raise HTTPException(429, "Слишком много подтверждений. Подождите две минуты")
        nonce = secrets.token_hex(32)
        gen = _session_generation(settings)
        bound = binding_hash(binding)
        message = json.dumps({"v": 1, "kind": "xass-native-" + kind, "nonce": nonce,
            "owner_id": owner_id, "device_id": device_id, "public_key_sha256": digest(key),
            "purpose": purpose, "binding_sha256": bound, "generation": gen,
            "expires": int(now.timestamp()) + 120}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        session.add(NativeChallenge(id=nonce, owner_id=owner_id, kind=kind, device_id=device_id,
            public_key=key, pair_hash=pair_hash, purpose=purpose, binding_hash=bound,
            message=message, generation=gen, expires_at=now + timedelta(seconds=120)))
        await session.commit()
        return {"ok": True, "challenge_id": nonce, "message": b64(message.encode()), "expires_in": 120}

    async def take_challenge(session, challenge_id, kind, owner_id):
        row = await session.get(NativeChallenge, challenge_id)
        if row is None or row.kind != kind or row.owner_id != owner_id:
            raise HTTPException(403, "Подтверждение не найдено")
        conditions = []
        if kind == "action":
            conditions.append(select(NativeDevice.id).where(NativeDevice.id == row.device_id,
                NativeDevice.owner_id == owner_id, NativeDevice.revoked.is_(False),
                NativeDevice.generation == row.generation, NativeDevice.public_key == row.public_key).exists())
        result = await session.execute(update(NativeChallenge).where(NativeChallenge.id == challenge_id,
            NativeChallenge.used.is_(False), NativeChallenge.expires_at > datetime.now(timezone.utc),
            NativeChallenge.generation == _session_generation(settings), *conditions).values(used=True).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise HTTPException(409, "Подтверждение уже использовано или истекло")
        return row

    @router.post("/api/native/enrollment/options")
    async def enrollment_options(payload: EnrollmentOptions, session=Depends(get_session)):
        public_key(payload.public_key)
        pair = await pair_owner(session, payload.pair_token)
        return await challenge(session, owner_id=pair.created_by_user_id, kind="enroll", key=payload.public_key,
                               pair_hash=pair.token_hash)

    @router.post("/api/native/enrollment/verify")
    async def enrollment_verify(payload: EnrollmentVerify, response: Response, session=Depends(get_session)):
        pair = await pair_owner(session, payload.pair_token)
        row = await take_challenge(session, payload.challenge_id, "enroll", pair.created_by_user_id)
        if row.public_key != payload.public_key or row.pair_hash != pair.token_hash:
            raise HTTPException(403, "Ссылка и ключ не соответствуют подтверждению")
        verify_signature(row.public_key, payload.signature, row.message)
        result = await session.execute(update(PwaPairToken).where(PwaPairToken.id == pair.id,
            PwaPairToken.is_active.is_(True), PwaPairToken.expires_at > datetime.now(timezone.utc)).values(
                is_active=False, consumed_at=datetime.now(timezone.utc)).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise HTTPException(409, "Ссылка уже использована")
        device_id = secrets.token_hex(16)
        session.add(NativeDevice(id=device_id, owner_id=pair.created_by_user_id, public_key=row.public_key,
            name=payload.device_name, generation=row.generation))
        await session.commit()
        user = MiniAppUser(user_id=pair.created_by_user_id, first_name="Владелец", last_name="", username="", is_owner=True)
        response.set_cookie(COOKIE_NAME, issue_session(user, settings), max_age=SESSION_AGE_SEC, httponly=True,
                            secure=settings.pwa_cookie_secure, samesite="lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, "device_id": device_id, "user": {"id": user.user_id, "is_owner": True}}

    @router.post("/api/native/actions/options")
    async def action_options(payload: ActionOptions, user=Depends(require_owner), session=Depends(get_session)):
        device = await device_for(session, payload.device_id, user.user_id)
        if not payload.purpose.startswith(("agent:", "music:evict:")):
            raise HTTPException(400, "Действие не поддерживается нативным подтверждением")
        return await challenge(session, owner_id=user.user_id, kind="action", key=device.public_key,
            device_id=device.id, purpose=payload.purpose, binding=payload.binding)

    @router.post("/api/native/actions/verify")
    async def action_verify(payload: ActionVerify, user=Depends(require_owner), session=Depends(get_session)):
        device = await device_for(session, payload.device_id, user.user_id)
        row = await take_challenge(session, payload.challenge_id, "action", user.user_id)
        # Refresh after the conditional challenge mutation: a cached initial
        # device must not be used to issue a proof following a revocation.
        device = await device_for(session, payload.device_id, user.user_id)
        if row.device_id != device.id or row.public_key != device.public_key:
            raise HTTPException(403, "Подтверждение другого устройства")
        verify_signature(device.public_key, payload.signature, row.message)
        token = "xna_" + secrets.token_urlsafe(32)
        session.add(NativeProof(id=digest(token), owner_id=user.user_id, device_id=device.id, purpose=row.purpose,
            binding_hash=row.binding_hash, generation=row.generation,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60)))
        await session.commit()
        return {"ok": True, "action_proof": token, "expires_in": 60}

    return router
