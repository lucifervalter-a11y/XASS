from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
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


def is_agent_runtime_path(relative: Path, *, directory: bool = False) -> bool:
    """Keep a development agent's private state out of shared source packages.

    Match runtime basenames, including atomic-write temporaries/backups, without
    treating source templates such as config.example.json or assets as secrets.
    This same predicate can be used by server snapshot exporters.
    """
    parts = tuple(part.casefold() for part in relative.parts)
    if not parts:
        return False
    directories = parts if directory else parts[:-1]
    ignored_dirs = {".git", ".ssh", ".build-venv", ".updates", "build", "dist", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}
    if any(part in ignored_dirs or part.startswith((".venv", "venv")) for part in directories):
        return True
    # Runtime storage lives alongside the source when running an unpackaged
    # agent. Nested assets/data and documentation examples remain packageable.
    if directories and directories[0] in {"data", "archive", "archives", "logs", "log", "cache", ".cache", "runtime", "env"}:
        return True
    if directory:
        return False
    name = parts[-1]
    normalized = name.lstrip(".")
    if name in {"config.json.example", "config.json.template", "config.json.sample"}:
        return False
    runtime_names = {
        "config.json", "xass-master.key", "command-results.json", "agent-status.json",
        "update-result.json", "installed-revision", "xass-archive-state.json",
        "xass-archive.sqlite3", "xass-managed-files.json", "migration.json",
    }
    if any(normalized == base or normalized.startswith((base + ".", base + "-")) for base in runtime_names):
        return True
    if name.startswith(".env") and name not in {".env.example", ".env.template"}:
        return True
    return bool(
        re.search(r"\.log(?:\.[0-9]+)?$", name)
        or (name.startswith(".xass") and name.endswith(".instance"))
        or name.endswith(".pyc") or ".generated." in name
    )


def _package_files(client_root: Path) -> list[Path]:
    result: list[Path] = []
    for current, directories, filenames in os.walk(client_root, followlinks=False):
        directory = Path(current)
        # Prune before descending: an archive/venv can contain millions of files.
        directories[:] = [name for name in directories if not (
            is_agent_runtime_path((directory / name).relative_to(client_root), directory=True)
            or (directory / name).is_symlink()
            or (hasattr(Path, "is_junction") and (directory / name).is_junction())
        )]
        for name in filenames:
            path = directory / name
            if path.is_symlink() or not path.is_file() or is_agent_runtime_path(path.relative_to(client_root)):
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


def update_is_available(
    *,
    current_version: str,
    current_revision: str,
    published_version: str,
    published_revision: str,
) -> bool:
    """Return True for upgrades or a new build of the same version, never downgrades."""

    def version_key(value: str) -> tuple[int, int, int, int]:
        parts = [int(item) for item in re.findall(r"\d+", str(value or ""))[:4]]
        return tuple((parts + [0, 0, 0, 0])[:4])  # type: ignore[return-value]

    current_key = version_key(current_version)
    published_key = version_key(published_version)
    if published_key != current_key:
        return published_key > current_key
    return str(current_revision or "").strip() != str(published_revision or "").strip()


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
                archive.writestr(
                    ".xass-managed-files.json",
                    json.dumps(
                        {
                            "version": 1,
                            "files": [path.relative_to(client_root).as_posix() for path in files],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
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
    available = update_is_available(
        current_version=current_version,
        current_revision=current_revision,
        published_version=package.version,
        published_revision=package.revision,
    )
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
