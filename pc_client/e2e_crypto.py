from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"XASS"
VERSION = 1
HKDF_SALT = b"xass-e2e-v1"
HKDF_INFO = b"xass-e2e-aes"
SEALED_CONTENT_TYPE = "application/x-xass-sealed"


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _coord(value: int) -> str:
    return _b64url_encode(value.to_bytes(32, "big"))


def _coord_int(value: str) -> int:
    raw = _b64url_decode(value)
    if len(raw) != 32:
        raise ValueError("Invalid P-256 coordinate")
    return int.from_bytes(raw, "big")


def is_public_jwk(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("kty") != "EC" or value.get("crv") != "P-256":
        return False
    try:
        _coord_int(str(value.get("x") or ""))
        _coord_int(str(value.get("y") or ""))
    except (ValueError, TypeError):
        return False
    return True


def public_only(jwk: dict[str, Any]) -> dict[str, str]:
    if not is_public_jwk(jwk):
        raise ValueError("Invalid E2E public key")
    return {"kty": "EC", "crv": "P-256", "x": str(jwk["x"]), "y": str(jwk["y"])}


def generate_keypair() -> tuple[dict[str, str], dict[str, str]]:
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.private_numbers()
    public = {
        "kty": "EC",
        "crv": "P-256",
        "x": _coord(numbers.public_numbers.x),
        "y": _coord(numbers.public_numbers.y),
    }
    secret = {**public, "d": _coord(numbers.private_value)}
    return secret, public


def _private_key(jwk: dict[str, Any]) -> ec.EllipticCurvePrivateKey:
    if not is_public_jwk(jwk) or not jwk.get("d"):
        raise ValueError("Invalid E2E private key")
    return ec.derive_private_key(_coord_int(str(jwk["d"])), ec.SECP256R1())


def _public_key(jwk: dict[str, Any]) -> ec.EllipticCurvePublicKey:
    cleaned = public_only(jwk)
    return ec.EllipticCurvePublicNumbers(
        _coord_int(cleaned["x"]),
        _coord_int(cleaned["y"]),
        ec.SECP256R1(),
    ).public_key()


def shared_key(private_jwk: dict[str, Any], peer_public_jwk: dict[str, Any]) -> bytes:
    shared = _private_key(private_jwk).exchange(ec.ECDH(), _public_key(peer_public_jwk))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=HKDF_SALT, info=HKDF_INFO).derive(shared)


def seal_bytes(plaintext: bytes, *, private_jwk: dict[str, Any], peer_public_jwk: dict[str, Any], aad: bytes) -> bytes:
    nonce = os.urandom(12)
    ciphertext = AESGCM(shared_key(private_jwk, peer_public_jwk)).encrypt(nonce, plaintext, aad)
    return MAGIC + bytes([VERSION]) + nonce + ciphertext


def unseal_bytes(blob: bytes, *, private_jwk: dict[str, Any], peer_public_jwk: dict[str, Any], aad: bytes) -> bytes:
    if len(blob) < 18 or blob[:4] != MAGIC or blob[4] != VERSION:
        raise ValueError("Это не зашифрованный пакет XASS")
    nonce = blob[5:17]
    ciphertext = blob[17:]
    return AESGCM(shared_key(private_jwk, peer_public_jwk)).decrypt(nonce, ciphertext, aad)


def is_sealed_blob(blob: bytes) -> bool:
    return len(blob) >= 18 and blob[:4] == MAGIC and blob[4] == VERSION


def seal_text(text: str, *, private_jwk: dict[str, Any], peer_public_jwk: dict[str, Any], aad: str) -> dict[str, Any]:
    blob = seal_bytes(text.encode("utf-8"), private_jwk=private_jwk, peer_public_jwk=peer_public_jwk, aad=aad.encode("utf-8"))
    return {"sealed": True, "blob": _b64url_encode(blob)}


def unseal_text(payload: dict[str, Any], *, private_jwk: dict[str, Any], peer_public_jwk: dict[str, Any], aad: str) -> str:
    blob = _b64url_decode(str(payload.get("blob") or ""))
    return unseal_bytes(blob, private_jwk=private_jwk, peer_public_jwk=peer_public_jwk, aad=aad.encode("utf-8")).decode("utf-8")


def can_seal(config: dict[str, Any]) -> bool:
    return is_public_jwk(config.get("e2e_private_jwk")) and is_public_jwk(config.get("owner_e2e_public_jwk")) and bool(
        (config.get("e2e_private_jwk") or {}).get("d")
    )


def ensure_agent_keys(config: dict[str, Any]) -> bool:
    private = config.get("e2e_private_jwk")
    public = config.get("e2e_public_jwk")
    if is_public_jwk(private) and private.get("d") and is_public_jwk(public):
        return False
    secret, published = generate_keypair()
    config["e2e_private_jwk"] = secret
    config["e2e_public_jwk"] = published
    return True


def dump_public(jwk: Any) -> str:
    return json.dumps(public_only(jwk), separators=(",", ":"), sort_keys=True)
