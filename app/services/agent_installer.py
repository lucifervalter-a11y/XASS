from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.services.agent_updates import sign_manifest

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(frozen=True, slots=True)
class AgentInstaller:
    path: Path
    version: str
    revision: str
    sha256: str
    size: int


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else _repo_root() / path


@lru_cache(maxsize=8)
def _hash_file(path_text: str, size: int, modified_ns: int) -> str:
    del size, modified_ns
    digest = hashlib.sha256()
    with Path(path_text).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_agent_installer(settings: "Settings") -> AgentInstaller | None:
    path = _resolve_path(settings.agent_installer_path)
    metadata_path = _resolve_path(settings.agent_installer_metadata_path)
    if not path.is_file() or not metadata_path.is_file():
        return None
    try:
        metadata: dict[str, Any] = json.loads(metadata_path.read_text(encoding="utf-8"))
        stat = path.stat()
        sha256 = _hash_file(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        expected_sha = str(metadata.get("sha256") or "").strip().lower()
        if not expected_sha or expected_sha != sha256:
            return None
        version = str(metadata.get("version") or "").strip()
        revision = str(metadata.get("revision") or sha256).strip()
        if not version or not revision:
            return None
        return AgentInstaller(path=path, version=version, revision=revision, sha256=sha256, size=stat.st_size)
    except (OSError, ValueError, TypeError):
        return None


def installer_public_info(settings: "Settings") -> dict[str, object]:
    installer = get_agent_installer(settings)
    if installer is None:
        return {"available": False, "version": "", "size": 0}
    return {
        "available": True,
        "version": installer.version,
        "revision": installer.revision,
        "size": installer.size,
        "file_name": f"XASS-Setup-{installer.version}.exe",
        "download_path": "/api/mini/agent-installer/download",
    }


def build_installer_manifest(
    settings: "Settings",
    *,
    api_key: str,
    base_url: str,
    current_version: str,
    current_revision: str,
) -> dict[str, object] | None:
    installer = get_agent_installer(settings)
    if installer is None:
        return None
    url = f"{base_url.rstrip('/')}/agent/installer/{installer.revision}.exe"
    signature = sign_manifest(
        api_key,
        version=installer.version,
        revision=installer.revision,
        sha256=installer.sha256,
        url=url,
    )
    return {
        "available": current_version.strip() != installer.version or current_revision.strip() != installer.revision,
        "version": installer.version,
        "revision": installer.revision,
        "sha256": installer.sha256,
        "size": installer.size,
        "url": url,
        "signature": signature,
        "mandatory": False,
        "distribution": "installer",
    }
