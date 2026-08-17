from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 8:
        raise ValueError("Пароль резервной копии должен содержать минимум 8 символов")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))


def encrypt_backup(bundle: dict[str, Any], passphrase: str) -> dict[str, Any]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(passphrase, salt)
    plaintext = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, b"xass-config-encrypted-v1")
    return {
        "format": "xass-config-encrypted",
        "version": 1,
        "kdf": {"name": "scrypt", "n": 32768, "r": 8, "p": 1, "salt": _encode(salt)},
        "cipher": {"name": "AES-256-GCM", "nonce": _encode(nonce)},
        "data": _encode(ciphertext),
    }


def decrypt_backup(envelope: dict[str, Any], passphrase: str) -> dict[str, Any]:
    if envelope.get("format") != "xass-config-encrypted" or int(envelope.get("version") or 0) != 1:
        raise ValueError("Это не зашифрованная резервная копия XASS")
    try:
        salt = _decode(str(envelope["kdf"]["salt"]))
        nonce = _decode(str(envelope["cipher"]["nonce"]))
        ciphertext = _decode(str(envelope["data"]))
        plaintext = AESGCM(_derive_key(passphrase, salt)).decrypt(
            nonce, ciphertext, b"xass-config-encrypted-v1"
        )
        bundle = json.loads(plaintext)
    except (KeyError, TypeError, ValueError, InvalidTag, json.JSONDecodeError) as exc:
        raise ValueError("Неверный пароль или резервная копия повреждена") from exc
    if not isinstance(bundle, dict) or bundle.get("format") != "xass-config":
        raise ValueError("Расшифрованный файл не является резервной копией XASS")
    return bundle
