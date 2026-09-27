"""DB-backed, parameter-bound Passkey approvals, independent of process count."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets

from sqlalchemy import delete, select, update

from app.models import PasskeyCredential
from app.pwa_models import PwaActionProof
from app.services.pwa_auth import ACTION_PROOF_AGE_SEC, _session_generation


def binding_hash(binding: dict | None) -> str:
    if binding is not None and not isinstance(binding, dict):
        raise ValueError("Некорректные параметры подтверждения")
    try:
        encoded = json.dumps(binding or {}, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Некорректные параметры подтверждения") from exc
    if len(encoded) > 8192:
        raise ValueError("Слишком большой запрос подтверждения")
    return hashlib.sha256(encoded).hexdigest()


async def issue_action_proof(session, *, owner_id, credential_id, purpose,
                             parameters_hash, generation, settings) -> str:
    if owner_id != settings.owner_user_id or generation != _session_generation(settings):
        raise ValueError("Сеанс изменился. Войдите и подтвердите действие заново.")
    if not re.fullmatch(r"[0-9a-f]{64}", parameters_hash):
        raise ValueError("Некорректные параметры подтверждения")
    live = await session.scalar(select(PasskeyCredential.id).where(
        PasskeyCredential.id == credential_id, PasskeyCredential.owner_user_id == owner_id))
    if live is None:
        raise ValueError("Passkey удалён. Подтвердите действие действующим ключом.")
    now = datetime.now(timezone.utc)
    await session.execute(delete(PwaActionProof).where(PwaActionProof.expires_at <= now))
    token = "xpa_" + secrets.token_urlsafe(32)
    session.add(PwaActionProof(id=hashlib.sha256(token.encode()).hexdigest(), owner_id=owner_id,
        credential_id=credential_id, purpose=purpose, binding_hash=parameters_hash,
        generation=generation, expires_at=now + timedelta(seconds=ACTION_PROOF_AGE_SEC)))
    await session.commit()
    return token


async def consume_action_proof(session, token, owner_id, purpose, binding, settings) -> bool:
    """Atomically claim an approval. Caller must commit before running side effects."""
    if not re.fullmatch(r"xpa_[A-Za-z0-9_-]{43}", token or ""):
        return False  # Old reusable HMAC proofs deliberately cannot authorize writes.
    if owner_id != settings.owner_user_id:
        return False
    try:
        parameters_hash = binding_hash(binding)
    except ValueError:
        return False
    live = select(PasskeyCredential.id).where(
        PasskeyCredential.id == PwaActionProof.credential_id,
        PasskeyCredential.owner_user_id == owner_id).exists()
    result = await session.execute(update(PwaActionProof).where(
        PwaActionProof.id == hashlib.sha256(token.encode()).hexdigest(),
        PwaActionProof.owner_id == owner_id, PwaActionProof.purpose == purpose,
        PwaActionProof.binding_hash == parameters_hash,
        PwaActionProof.generation == _session_generation(settings),
        PwaActionProof.expires_at > datetime.now(timezone.utc),
        PwaActionProof.used.is_(False), live,
    ).values(used=True).execution_options(synchronize_session=False))
    return result.rowcount == 1
