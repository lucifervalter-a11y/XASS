#!/usr/bin/env python3
"""Validate a staged CI installer, then atomically publish immutable downloads.

Run from the deployed checkout after uploading both files into
data/releases/.incoming-<revision>. No server restart or live EXE overwrite is
needed. The previous metadata remains at <metadata filename>.previous; its
binary is retained, so restoring that metadata is sufficient for rollback.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError(f"Invalid metadata file: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Metadata must be an object: {path.name}")
    return value


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _configured_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _atomic_bytes(target: Path, content: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _publication_lock(metadata_path: Path):
    lock = metadata_path.with_name(f".{metadata_path.name}.publish.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError(
            "Another installer publication is active. If it crashed, verify no publisher "
            f"is running before removing {lock.name}."
        ) from exc
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _install_immutable(source: Path, target: Path, sha256: str, size: int) -> None:
    def validate_existing() -> None:
        if target.is_symlink() or not target.is_file() or _file_digest(target) != (sha256, size):
            raise ValueError("Existing immutable artifact differs; it was not overwritten")

    if target.exists() or target.is_symlink():
        validate_existing()
        return
    fd, name = tempfile.mkstemp(prefix=".XASS-Setup-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        # Hash the copy too: a changing/incomplete upload cannot be published.
        digest = hashlib.sha256()
        copied = 0
        with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if (digest.hexdigest(), copied) != (sha256, size):
            raise ValueError("Staged installer changed during publication")
        temporary.chmod(0o644)
        try:
            # Same-directory hard linking publishes the completed file atomically
            # without ever replacing an existing name, including concurrent writes.
            os.link(temporary, target)
        except FileExistsError:
            validate_existing()
    finally:
        temporary.unlink(missing_ok=True)


def publish_installer(
    root: Path, staging_dir: Path, expected_revision: str, *, settings: Any = None
) -> dict[str, Any]:
    root = root.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", expected_revision):
        raise ValueError("Expected revision must be a full, clean Git commit hash")
    if settings is None:
        from app.config import Settings

        try:
            settings = Settings(_env_file=root / ".env")
        except ValueError as exc:
            # Pydantic's detailed validation text can include environment values.
            raise ValueError("Server settings could not be loaded; validate the root .env file") from exc
    stage = staging_dir if staging_dir.is_absolute() else root / staging_dir
    stage = stage.resolve()
    source = stage / "XASS-Setup.exe"
    metadata = _read_json(stage / "XASS-Setup.json")
    source_version = _read_json(root / "pc_client" / "version.json").get("version")
    version = metadata.get("version")
    if not isinstance(version, str) or not version or len(version) > 64 or version != source_version:
        raise ValueError("Installer version does not match the deployed source version")
    if metadata.get("revision") != expected_revision or metadata.get("local_build") is not False:
        raise ValueError("Installer revision must match the expected clean CI revision")
    sha256 = metadata.get("sha256")
    size = metadata.get("size")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("Installer SHA-256 is invalid")
    if type(size) is not int or size < 2:
        raise ValueError("Installer size is invalid")
    if source.is_symlink() or not source.is_file() or _file_digest(source) != (sha256, size):
        raise ValueError("Staged installer SHA-256 or size mismatch")
    with source.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise ValueError("Staged installer is not a Windows executable")

    configured = _configured_path(root, settings.agent_installer_path)
    metadata_path = _configured_path(root, settings.agent_installer_metadata_path)
    if metadata_path.is_symlink():
        raise ValueError("Live installer metadata must not be a symbolic link")
    target = configured.parent.resolve() / f"XASS-Setup-{sha256}.exe"
    metadata = {**metadata, "artifact_file": target.name}
    content = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    with _publication_lock(metadata_path):
        _install_immutable(source, target, sha256, size)
        previous = metadata_path.read_bytes() if metadata_path.exists() else None
        if previous != content:
            if previous is not None:
                _atomic_bytes(metadata_path.with_name(metadata_path.name + ".previous"), previous)
            _atomic_bytes(metadata_path, content)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-dir", required=True, type=Path)
    parser.add_argument("--expected-revision", required=True)
    args = parser.parse_args()
    try:
        metadata = publish_installer(ROOT, args.staging_dir, args.expected_revision)
    except (OSError, ValueError) as exc:
        print(f"Installer publication failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({key: metadata[key] for key in ("version", "revision", "sha256", "size", "artifact_file")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
