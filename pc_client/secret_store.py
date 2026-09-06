from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD = b"xass-config-sealed-v2"
SECRET_KEYS = ("api_key", "e2e_private_jwk")
MASTER_NAME = ".xass-master.key"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _dpapi_protect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = DATA_BLOB(len(data), (ctypes.c_byte * len(data)).from_buffer_copy(data))
    blob_out = DATA_BLOB()
    entropy = b"xass-agent-secret-v1"
    blob_ent = DATA_BLOB(len(entropy), (ctypes.c_byte * len(entropy)).from_buffer_copy(entropy))
    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_ent),
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(blob_out),
    ):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = DATA_BLOB(len(data), (ctypes.c_byte * len(data)).from_buffer_copy(data))
    blob_out = DATA_BLOB()
    entropy = b"xass-agent-secret-v1"
    blob_ent = DATA_BLOB(len(entropy), (ctypes.c_byte * len(entropy)).from_buffer_copy(entropy))
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_ent),
        None,
        None,
        0x1,
        ctypes.byref(blob_out),
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _master_key(data_dir: Path) -> bytes:
    path = data_dir / MASTER_NAME
    if path.is_file():
        raw = path.read_bytes()
        if len(raw) == 32:
            return raw
    data_dir.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _aes_protect(data: bytes, data_dir: Path) -> dict[str, str]:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_master_key(data_dir)).encrypt(nonce, data, AAD)
    return {"cipher": "aes-256-gcm", "nonce": _encode(nonce), "data": _encode(ciphertext)}


def _aes_unprotect(envelope: dict[str, Any], data_dir: Path) -> bytes:
    nonce = _decode(str(envelope["nonce"]))
    ciphertext = _decode(str(envelope["data"]))
    return AESGCM(_master_key(data_dir)).decrypt(nonce, ciphertext, AAD)


def protect_bytes(data: bytes, *, data_dir: Path) -> dict[str, str]:
    if os.name == "nt":
        try:
            return {"cipher": "dpapi", "data": _encode(_dpapi_protect(data))}
        except OSError:
            pass
    return _aes_protect(data, data_dir)


def unprotect_bytes(envelope: dict[str, Any], *, data_dir: Path) -> bytes:
    cipher = str(envelope.get("cipher") or "")
    if cipher == "dpapi":
        return _dpapi_unprotect(_decode(str(envelope["data"])))
    if cipher == "aes-256-gcm":
        return _aes_unprotect(envelope, data_dir)
    raise ValueError("Unsupported secret cipher")


def _secret_payload(config: dict[str, Any]) -> dict[str, Any]:
    secrets: dict[str, Any] = {}
    for key in SECRET_KEYS:
        if key in config and config[key] not in (None, ""):
            secrets[key] = config[key]
    return secrets


def seal_config(config: dict[str, Any], *, data_dir: Path) -> dict[str, Any]:
    public = {key: value for key, value in config.items() if key not in SECRET_KEYS}
    secrets = _secret_payload(config)
    public["format"] = "xass-config"
    public["version"] = 2
    if secrets:
        public["sealed"] = protect_bytes(
            json.dumps(secrets, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            data_dir=data_dir,
        )
    else:
        public.pop("sealed", None)
    return public


def unseal_config(payload: dict[str, Any], *, data_dir: Path) -> dict[str, Any]:
    config = dict(payload)
    envelope = config.pop("sealed", None)
    if isinstance(envelope, dict) and envelope.get("data"):
        try:
            secrets = json.loads(unprotect_bytes(envelope, data_dir=data_dir))
        except Exception as exc:
            raise ValueError("Не удалось расшифровать локальный ключ агента") from exc
        if isinstance(secrets, dict):
            for key in SECRET_KEYS:
                if key in secrets:
                    config[key] = secrets[key]
    config.pop("format", None)
    config.pop("version", None)
    return config


def cipher_label(payload: dict[str, Any]) -> str:
    envelope = payload.get("sealed")
    if not isinstance(envelope, dict):
        if payload.get("api_key"):
            return "открытый config.json"
        return "нет ключа"
    cipher = str(envelope.get("cipher") or "")
    if cipher == "dpapi":
        return "Windows DPAPI"
    if cipher == "aes-256-gcm":
        return "AES-256-GCM"
    return cipher or "неизвестно"
