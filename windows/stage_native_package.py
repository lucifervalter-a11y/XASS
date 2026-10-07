"""Validate and stage an installer payload; never reads user configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("packaging") / "native-payload.json"
FORBIDDEN_NAMES = {"config.json", "appearance.json", ".xass-master.key", ".agent-status.json",
                   "music-playback.json", "migration-manifest.json", ".command-results.json",
                   "runtime.json", "local-music.json", ".agent.log", "agent.log"}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".pfx", ".sqlite3", ".db", ".xass"}


def safe_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Expected a real build-output directory")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlinks are not allowed in installer input")
        lower = path.name.lower()
        # Package trust stores legitimately contain public CA .pem files. Only
        # certifi's generated public trust store is allowlisted, never user keys.
        public_ca = path.relative_to(root).as_posix().lower().endswith("certifi/cacert.pem")
        if lower.startswith(".env") or lower in FORBIDDEN_NAMES or any(
            lower.startswith(name + ".") for name in FORBIDDEN_NAMES
        ) or (
            path.suffix.lower() in FORBIDDEN_SUFFIXES and not public_ca
        ):
            raise ValueError(f"Possible personal/configuration material in build output: {path.name}")
        if path.is_file():
            files.append(path)
    return files


def require_files(root: Path, names: list[str]) -> None:
    actual = {p.relative_to(root).as_posix().lower(): p for p in safe_files(root)}
    for name in names:
        path = actual.get(name.lower())
        if path is None or path.stat().st_size == 0:
            raise ValueError(f"Required packaged file missing or empty: {name}")


def require_x64_pe(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("Expected Windows PE executable")
        offset = struct.unpack_from("<I", header, 0x3C)[0]
        if not 64 <= offset <= 1024 * 1024:
            raise ValueError("Invalid PE header offset")
        stream.seek(offset)
        pe = stream.read(6)
        if len(pe) != 6 or pe[:4] != b"PE\0\0" or struct.unpack_from("<H", pe, 4)[0] != 0x8664:
            raise ValueError("Expected x64 executable")


def copy_tree(source: Path, destination: Path) -> None:
    for file in safe_files(source):
        target = destination / file.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)


def validate_source_allowlist(sources: list[str]) -> None:
    if not isinstance(sources, list) or not sources or any(
        not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*\.py", name) for name in sources
    ) or len(sources) != len(set(sources)):
        raise ValueError("Invalid helper source allowlist")


def stage(native: Path, companion: Path, destination: Path, revision: str, licenses: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}|local-build", revision):
        raise ValueError("Revision must be a full immutable SHA or local-build")
    if destination.exists():
        raise ValueError("Staging destination must be new and empty")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    require_files(native, manifest["required_native_files"])
    require_files(companion, manifest["required_companion_files"])
    build_info = companion / "_internal" / "build-info.json"
    identity = json.loads(build_info.read_text(encoding="utf-8"))
    distribution = identity.get("distribution")
    if identity.get("revision") != revision or distribution not in {"native-test", "native"}:
        raise ValueError("Frozen companion identity does not match this native build")
    require_x64_pe(native / "Xass.Native.exe")
    require_x64_pe(companion / "XASS.NativeHelper.exe")
    for filename in ("Xass.Native.deps.json", "Xass.Native.runtimeconfig.json"):
        json.loads((native / filename).read_text(encoding="utf-8"))
    sources = manifest["pc_client_sources"]
    validate_source_allowlist(sources)
    for name in sources:
        if not (ROOT / "pc_client" / name).is_file():
            raise ValueError(f"Required allowlisted source missing: {name}")
    require_files(licenses, ["python-LICENSE.txt", "python-dependencies.json"])
    copy_tree(native, destination)
    copy_tree(companion, destination / "runtime")
    copy_tree(licenses, destination / "licenses")
    source_output = destination / "pc_client"
    source_output.mkdir()
    # The native updater and source fallback must see the same immutable identity
    # as the frozen helper; a missing identity would offer the current release again.
    shutil.copyfile(build_info, destination / "build-info.json")
    shutil.copyfile(build_info, source_output / "build-info.json")
    (destination / "native-install.json").write_text(json.dumps({
        "schema": 1, "app_id": "B4D7E8B9-9C58-4C36-A432-D114393006D8", "distribution": distribution,
        "version": identity["version"], "revision": revision
    }, indent=2) + "\n", encoding="utf-8")
    for name in sources:
        shutil.copyfile(ROOT / "pc_client" / name, source_output / name)
    for name in ("requirements.txt", "voice-requirements.txt", "version.json"):
        shutil.copyfile(ROOT / "pc_client" / name, source_output / name)
    (destination / "README-FIRST.txt").write_text(
        ("XASS Native / Windows x64\n\n" if distribution == "native" else "XASS Native Test / Windows x64\n\n") +
        "This installer includes the complete WinUI/.NET/Windows App SDK app and\n"
        "a private frozen Python 3.12 companion with agent, audio and faster-whisper\n"
        "CPU dependencies. No separately installed Python is required by bundled roles.\n"
        "The executable uses its adjacent runtime files; do not move it alone.\n\n"
        "The Whisper speech model is NOT included and is not downloaded by XASS.\n"
        "Select an existing local faster-whisper model folder to use speech.\n"
        "Ollama is optional and must already be installed/running with its model.\n"
        "Discord automatic voice joining is not supplied by this installer.\n\n"
        "The native app keeps its own installation identity. Existing agent pairing,\n"
        "archives and data remain in %LOCALAPPDATA%\\XASS. Native appearance stays\n"
        "in %LOCALAPPDATA%\\XASS.Native. Updating or uninstalling the native app does\n"
        "not delete either data folder. No credentials or personal configs are bundled.\n"
        "An existing running agent remains the playback owner; do not run competing agents.\n\n"
        "This installer is unsigned. Speech needs an existing local model.\n"
        "See licenses/ for runtime\n"
        "notices and the exact Python dependency inventory.\n\n"
        f"Commit: {revision}\n", encoding="utf-8")
    files = safe_files(destination)
    inventory = {"schema": 1, "revision": revision, "architecture": "x64", "python_bundled": True,
                 "whisper_runtime_bundled": True, "whisper_model_bundled": False,
                 "files": [{"path": p.relative_to(destination).as_posix(), "bytes": p.stat().st_size,
                            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
    (destination / "payload-manifest.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return inventory


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("native", "companion", "destination", "licenses"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    result = stage(args.native, args.companion, args.destination, args.revision, args.licenses)
    print(f"Verified native installer payload: {len(result['files'])} files")


if __name__ == "__main__":
    main()
