from __future__ import annotations

import hashlib
import hmac
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(slots=True)
class AgentPackage:
    path: Path
    version: str
    revision: str
    sha256: str
    size: int


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _client_root() -> Path:
    return _repo_root() / "pc_client"


def _version(client_root: Path) -> str:
    version_path = client_root / "version.json"
    try:
        payload = json.loads(version_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "0.0.0"
    return str(payload.get("version") or "0.0.0").strip() or "0.0.0"


def _package_files(client_root: Path) -> list[Path]:
    ignored_parts = {".venv", ".build-venv", ".updates", "build", "dist", "__pycache__"}
    ignored_names = {
        "config.json",
        ".command-results.json",
        ".agent-status.json",
        ".update-result.json",
        ".installed-revision",
    }
    result: list[Path] = []
    for path in client_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(client_root)
        if any(part in ignored_parts for part in relative.parts):
            continue
        if path.name in ignored_names or path.suffix.lower() == ".pyc" or ".generated." in path.name:
            continue
        result.append(path)
    return sorted(result, key=lambda item: item.relative_to(client_root).as_posix())


def _content_revision(client_root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(client_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_agent_package(settings: "Settings") -> AgentPackage:
    client_root = _client_root()
    files = _package_files(client_root)
    if not files:
        raise RuntimeError("PC client package is empty")

    version = _version(client_root)
    revision = _content_revision(client_root, files)
    cache_root = Path(settings.agent_update_cache_dir)
    if not cache_root.is_absolute():
        cache_root = _repo_root() / cache_root
    cache_root.mkdir(parents=True, exist_ok=True)
    package_path = cache_root / f"xass-pc-{version}-{revision[:12]}.zip"

    if not package_path.exists():
        temporary = package_path.with_name(f".{package_path.name}.{uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for path in files:
                    archive.write(path, path.relative_to(client_root).as_posix())
            temporary.replace(package_path)
        finally:
            temporary.unlink(missing_ok=True)

    return AgentPackage(
        path=package_path,
        version=version,
        revision=revision,
        sha256=_file_sha256(package_path),
        size=package_path.stat().st_size,
    )


def _manifest_message(version: str, revision: str, sha256: str, url: str) -> bytes:
    return f"{version}\n{revision}\n{sha256}\n{url}".encode("utf-8")


def sign_manifest(api_key: str, *, version: str, revision: str, sha256: str, url: str) -> str:
    return hmac.new(
        api_key.encode("utf-8"),
        _manifest_message(version, revision, sha256, url),
        hashlib.sha256,
    ).hexdigest()


def build_update_manifest(
    settings: "Settings",
    *,
    api_key: str,
    base_url: str,
    current_version: str,
    current_revision: str,
) -> dict[str, object] | None:
    if not settings.agent_updates_enabled:
        return None

    package = build_agent_package(settings)
    # Keep the revision in the path so reverse proxies cannot accidentally
    # serve a cached ZIP for another revision after ignoring a query string.
    url = f"{base_url.rstrip('/')}/agent/update/package/{package.revision}.zip"
    current_version = (current_version or "0.0.0").strip()
    current_revision = (current_revision or "").strip()
    available = current_version != package.version or current_revision != package.revision
    signature = sign_manifest(
        api_key,
        version=package.version,
        revision=package.revision,
        sha256=package.sha256,
        url=url,
    )
    return {
        "available": available,
        "version": package.version,
        "revision": package.revision,
        "sha256": package.sha256,
        "size": package.size,
        "url": url,
        "signature": signature,
        "mandatory": False,
    }
