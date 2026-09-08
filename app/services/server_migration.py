"""Portable, verified server snapshots. Restore deliberately needs only Python's stdlib.

Archives contain secrets and must never be placed in a public downloads directory.
No tar member is extracted using extract()/extractall(), and restore never merges
with an existing installation. A stopped source gives a cross-file point-in-time
snapshot; SQLite's online backup also works safely while the source is running.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from app.services.agent_updates import is_agent_runtime_path


FORMAT = "xass-server-backup"
VERSION = 1
MANIFEST = "xass-manifest.json"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_FILES = 200_000
PATH_SETTINGS = {
    "media_root", "music_root", "export_root", "profile_json_path", "profile_backups_dir",
    "profile_audit_log_path", "profile_avatars_dir", "conversation_avatar_cache_dir",
    "projects_json_path", "site_config_json_path", "quotes_json_path", "scenarios_json_path",
    "rules_json_path", "projects_backups_dir", "projects_audit_log_path",
    "projects_assets_dir", "backgrounds_assets_dir", "telegram_bot_identity_cache_path",
    "pwa_session_generation_path", "agent_installer_path", "agent_installer_metadata_path",
    "agent_workspace_dir", "update_log_path", "update_state_path", "restart_notice_path",
}
REBUILDABLE_PATHS = {"agent_update_cache_dir", "agent_migration_export_dir"}
PUBLIC_EXTERNAL_TARGETS = {
    "profile_avatars_dir": "data/avatars",
    "projects_assets_dir": "assets/projects",
    "backgrounds_assets_dir": "assets/backgrounds",
}
EXCLUDED_PARTS = {".git", ".venv", "__pycache__", ".build-venv", "node_modules"}
SOURCE_DIRS = {"app", "agent", "pc_client", "assets", "projects", "deploy", "docs", "tests", ".github"}
SOURCE_FILES = {".env.example", ".gitignore", "requirements.txt", "README.md"}
CANCELLED_COMMAND_RESULT = {"ok": False, "message": "Команда отменена при переносе сервера. Отправьте её повторно.", "details": {"reason": "server_migration"}}
# Backups preserve identities and music, but must never resurrect one-time
# authorizations or play delayed audio after a restore. SQL is static, not input.
RESTORE_EPHEMERAL_UPDATES = {
    "native_challenges": "UPDATE native_challenges SET used=true",
    "native_action_proofs": "UPDATE native_action_proofs SET used=true",
    "pwa_pair_tokens": "UPDATE pwa_pair_tokens SET is_active=false",
    "agent_pair_codes": "UPDATE agent_pair_codes SET is_active=false",
    "music_transfers": "UPDATE music_transfers SET status='failed', detail='server_migration' WHERE status NOT IN ('ready','failed')",
    "music_remote_commands": "UPDATE music_remote_commands SET status='cancelled', error='server_migration' WHERE status='pending'",
    "music_playback_state": "UPDATE music_playback_state SET transfer_id='', queue_command_id=NULL, revision=revision+1",
    "music_sessions": "UPDATE music_sessions SET state='stopped', session_key='', share_site=false, share_discord=false",
    "music_storage_jobs": "UPDATE music_storage_jobs SET status='failed', error_code='server_migration' WHERE status IN ('pending','running')",
    "music_import_runs": "UPDATE music_import_runs SET status='cancelled' WHERE status IN ('pending','running')",
}


class MigrationError(ValueError):
    """A safe, actionable migration error, with no credential values."""


def _safe_relative(name: str) -> str:
    parts = PurePosixPath(name).parts
    if (
        not name or "\\" in name or any(ord(char) < 32 for char in name) or ":" in name
        or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/"))
        or any(part.endswith((".", " ")) for part in parts)
        or any(part.lower().split(".")[0] in {"con", "prn", "aux", "nul", *[f"com{i}" for i in range(1, 10)], *[f"lpt{i}" for i in range(1, 10)]} for part in parts)
    ):
        raise MigrationError("Unsafe archive path")
    return name


def _no_symlinks(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise MigrationError(f"Symbolic links/junctions are not supported: {item.name}")


def _path(root: Path, value: Any) -> Path:
    candidate = Path(str(value))
    candidate = candidate if candidate.is_absolute() else root / candidate
    _no_symlinks(candidate)
    return candidate.resolve()


def _hash_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _inside(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def _env_text(settings: Mapping[str, Any]) -> str:
    lines = ["# Restored XASS settings. Contains secrets; keep private."]
    for key, value in sorted(settings.items()):
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*", key):
            raise MigrationError("Invalid settings key")
        if value is None:
            value = ""
        elif isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, list):
            value = ",".join(str(item) for item in value)
        # python-dotenv interpolates ${...} even inside single quotes. Never emit
        # an archive that would silently change an already resolved secret.
        if re.search(r"\$\{[^}]*\}", str(value)):
            raise MigrationError(f"{key.upper()} contains a literal ${{...}} expression. Dotenv would expand it on restore; change this value explicitly before export.")
        # Single quotes preserve ordinary punctuation, backslashes and newlines.
        escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"{key.upper()}='{escaped}'")
    return "\n".join(lines) + "\n"


def _postgres_env(url: str) -> dict[str, str]:
    parsed = urlsplit(re.sub(r"^postgresql\+[^:]+:", "postgresql:", url))
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.path.strip("/"):
        raise MigrationError("Expected a PostgreSQL connection URL with a database name")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update({"PGDATABASE": unquote(parsed.path[1:]), "PGCONNECT_TIMEOUT": "15"})
    for key, value in (("PGHOST", parsed.hostname), ("PGPORT", parsed.port), ("PGUSER", parsed.username), ("PGPASSWORD", parsed.password)):
        if value is not None:
            env[key] = unquote(str(value))
    for key, value in parse_qsl(parsed.query):
        if key in {"sslmode", "sslcert", "sslkey", "sslrootcert"}:
            env["PG" + key.upper()] = value
        else:
            raise MigrationError(f"Unsupported PostgreSQL URL option: {key}")
    return env


def _pg_run(command: list[str], env: dict[str, str], *, input_text: str | None = None) -> str:
    if shutil.which(command[0]) is None:
        raise MigrationError(f"{command[0]} is missing. Install the PostgreSQL client matching your database version.")
    try:
        result = subprocess.run(command, env=env, input=input_text, capture_output=True, text=True, check=False, timeout=3600)
    except subprocess.TimeoutExpired as exc:
        raise MigrationError(f"{command[0]} timed out; destination files were not activated") from exc
    if result.returncode:
        # libpq stderr can contain credential-bearing connection strings.
        raise MigrationError(f"{command[0]} failed (exit {result.returncode}). Check database access, client version and available space.")
    return result.stdout


def _sqlite_path(root: Path, url: str) -> Path:
    match = re.match(r"^sqlite(?:\+aiosqlite)?:///(.+)$", url)
    if not match or match.group(1) == ":memory:" or "?" in match.group(1):
        raise MigrationError("Migration supports file-backed SQLite URLs, or PostgreSQL with its client tools")
    return _path(root, unquote(match.group(1)))


def export_server_archive(root: Path, destination: Path, settings: Any) -> dict[str, Any]:
    """Snapshot code, configured state, keys, uploads and database into a private tar.gz.

    settings accepts Settings or its model_dump(). An owner can invoke this in a
    worker thread; do not publish the resulting credential-bearing archive.
    """
    root = _path(Path.cwd(), root)
    destination = Path(destination).absolute()
    _no_symlinks(destination)
    if destination.exists():
        raise MigrationError("Backup destination already exists; use a new filename")
    if not (root / "app").is_dir() or not (root / "requirements.txt").is_file():
        raise MigrationError("Source is not an XASS installation")
    values = dict(settings.model_dump() if hasattr(settings, "model_dump") else settings)
    portable = dict(values)
    files: dict[str, Path] = {}
    path_map: dict[str, str] = {}
    excluded = [_path(root, values[key]) for key in REBUILDABLE_PATHS if values.get(key)]
    excluded += [destination]

    def collect(source: Path, target: str, *, pc_source: bool = False) -> None:
        _safe_relative(target)
        if any(_inside(source, skip) for skip in excluded):
            return
        if pc_source and is_agent_runtime_path(Path(PurePosixPath(target).relative_to("pc_client")), directory=source.is_dir()):
            return
        _no_symlinks(source)
        if not source.exists():
            return
        if source.is_dir():
            for item in sorted(source.iterdir()):
                if item.name in EXCLUDED_PARTS or item.name in {".updates", "build", "dist"}:
                    continue
                collect(item, f"{target}/{item.name}", pc_source=pc_source)
        elif source.is_file():
            if source.suffix == ".pyc":
                return
            previous = files.get(target)
            if previous is not None and previous != source:
                raise MigrationError(f"Conflicting configured paths: {target}")
            files[target] = source
        else:
            raise MigrationError(f"Unsupported filesystem object: {source.name}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".xass-export-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        excluded.append(stage)
        # Source distributions need no git metadata or access token to restore.
        for item in sorted(root.iterdir()):
            if item.name in SOURCE_DIRS or item.name in SOURCE_FILES or (item.is_file() and item.suffix in {".php", ".sh", ".bat", ".py", ".webmanifest", ".html", ".js"}):
                if item.name in {"run-agent.sh"}:
                    continue
                collect(item, item.name, pc_source=item.name == "pc_client")
        collect(root / "data", "data")
        for key in sorted(PATH_SETTINGS | REBUILDABLE_PATHS):
            if not values.get(key):
                continue
            source = _path(root, values[key])
            target = source.relative_to(root).as_posix() if _inside(source, root) and source != root else PUBLIC_EXTERNAL_TARGETS.get(key, f"data/imported/{key}/{source.name}")
            if source == root or source == source.parent:
                raise MigrationError(f"Configured {key} must point to a dedicated file or directory")
            _safe_relative(target)
            portable[key] = "./" + target
            path_map[str(source)] = target
            path_map[str(values[key])] = target
            if key not in REBUILDABLE_PATHS:
                collect(source, target)

        url = str(values.get("database_url", ""))
        if url.startswith("sqlite"):
            source_db = _sqlite_path(root, url)
            if not source_db.is_file():
                raise MigrationError("SQLite database does not exist; refusing to create an empty backup")
            snapshot = stage / "serverredus.db"
            with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
                with closing(sqlite3.connect(snapshot)) as target_db:
                    source.backup(target_db)
                    if target_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise MigrationError("SQLite integrity check failed")
            for name, source in list(files.items()):
                if source in {source_db, Path(str(source_db) + "-wal"), Path(str(source_db) + "-shm"), Path(str(source_db) + "-journal")}:
                    del files[name]
            database = {"engine": "sqlite", "path": "data/serverredus.db"}
            files[database["path"]] = snapshot
            portable["database_url"] = "sqlite+aiosqlite:///./data/serverredus.db"
        elif url.startswith(("postgresql:", "postgresql+", "postgres:")):
            snapshot = stage / "database.dump"
            _pg_run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(snapshot)], _postgres_env(url))
            database = {"engine": "postgresql", "path": "data/migration-database.dump"}
            files[database["path"]] = snapshot
        else:
            raise MigrationError("Unsupported database. Use SQLite or PostgreSQL")

        environment = stage / "settings.env"
        environment.write_text(_env_text(portable), encoding="utf-8", newline="\n")
        environment.chmod(0o600)
        files[".env"] = environment
        manifest: dict[str, Any] = {
            "format": FORMAT, "version": VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": database, "path_map": path_map,
            "contains_secrets": True, "files": {},
            "notes": ["Keep the HTTPS domain to retain device endpoints and passkeys.", "Archives stored on PC agents remain on those PCs; only server data is included.", "Stop source writes for a cross-file point-in-time cutover."],
        }
        if shutil.which("git"):
            revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
            if revision.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", revision.stdout.strip()):
                manifest["source_revision"] = revision.stdout.strip()
        archive_path = stage / "archive.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for name, source in sorted(files.items()):
                _no_symlinks(source)
                before = source.stat()
                if not stat.S_ISREG(before.st_mode):
                    raise MigrationError("Source file changed type during export")
                info = tarfile.TarInfo(name)
                info.size = before.st_size
                info.mode = 0o600 if name == ".env" or name.startswith("data/") else (0o755 if before.st_mode & 0o111 else 0o644)
                info.mtime = int(before.st_mtime)
                with source.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    handle.seek(0)
                    archive.addfile(info, handle)
                after = source.stat()
                if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                    raise MigrationError("Source changed during export; stop XASS briefly and retry")
                manifest["files"][name] = {"sha256": digest, "size": info.size, "mode": info.mode}
            body = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            if len(body) > MAX_MANIFEST_BYTES or len(files) > MAX_FILES:
                raise MigrationError("Backup has too many files")
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mode = len(body), 0o600
            archive.addfile(info, io.BytesIO(body))
        # Validate the finished stream before publishing it, including races while reading.
        inspect_server_archive(archive_path)
        # Hard-link the completed archive on this same filesystem: no partial output
        # is ever published, and an existing destination is never overwritten.
        archive_path.chmod(0o600)
        os.link(archive_path, destination)
        return {"path": str(destination), "size": destination.stat().st_size, "sha256": _hash_file(destination), "files": len(files), "database": database["engine"], "contains_secrets": True}


def _validated_archive(archive: tarfile.TarFile, max_bytes: int) -> tuple[dict[str, Any], dict[str, tarfile.TarInfo]]:
    members: dict[str, tarfile.TarInfo] = {}
    folded: set[str] = set()
    total = 0
    for member in archive:
        name = _safe_relative(member.name)
        if not member.isfile() or member.issparse() or name in members or name.casefold() in folded:
            raise MigrationError("Archive contains duplicate, linked, sparse or non-file members")
        total += member.size
        if member.size < 0 or total > max_bytes or len(members) >= MAX_FILES + 1:
            raise MigrationError("Archive exceeds the configured restore size/file limit")
        members[name] = member
        folded.add(name.casefold())
    info = members.get(MANIFEST)
    if info is None or info.size > MAX_MANIFEST_BYTES:
        raise MigrationError("Missing or oversized XASS manifest")
    try:
        with archive.extractfile(info) as handle:
            manifest = json.load(handle)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise MigrationError("Invalid XASS manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise MigrationError("Unsupported XASS backup format/version")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(members) - {MANIFEST}:
        raise MigrationError("Manifest does not match archive contents")
    database = manifest.get("database", {})
    if not isinstance(database, dict) or database.get("engine") not in {"sqlite", "postgresql"} or database.get("path") not in files or ".env" not in files:
        raise MigrationError("Missing database or settings in backup")
    for name, spec in files.items():
        _safe_relative(name)
        if not isinstance(spec, dict) or spec.get("size") != members[name].size or not re.fullmatch(r"[a-f0-9]{64}", str(spec.get("sha256", ""))):
            raise MigrationError("Invalid manifest file metadata")
        for parent in PurePosixPath(name).parents:
            if str(parent) != "." and str(parent) in files:
                raise MigrationError("Archive has conflicting file/directory paths")
    mapping = manifest.get("path_map", {})
    if not isinstance(mapping, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in mapping.items()):
        raise MigrationError("Invalid path mapping")
    for value in mapping.values():
        _safe_relative(value)
    return manifest, members


def _read_verified(archive: tarfile.TarFile, member: tarfile.TarInfo, spec: dict[str, Any], destination: Path | None = None) -> None:
    digest = hashlib.sha256()
    target = destination.open("xb") if destination is not None else None
    try:
        with archive.extractfile(member) as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
                if target is not None:
                    target.write(block)
        if digest.hexdigest() != spec["sha256"]:
            raise MigrationError(f"Checksum mismatch: {member.name}")
    finally:
        if target is not None:
            target.close()


def inspect_server_archive(path: Path, *, max_bytes: int = 100 * 1024**3) -> dict[str, Any]:
    """Validate the entire archive, returning metadata without exposing settings."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            manifest, members = _validated_archive(archive, max_bytes)
            for name, spec in manifest["files"].items():
                _read_verified(archive, members[name], spec)
            return {"format": FORMAT, "version": VERSION, "created_at": manifest["created_at"], "database": manifest["database"]["engine"], "files": len(manifest["files"]), "unpacked_bytes": sum(spec["size"] for spec in manifest["files"].values()), "contains_secrets": True}
    except (tarfile.TarError, EOFError) as exc:
        raise MigrationError("Truncated or damaged backup archive") from exc


def _portable_media_path(value: str, mapping: dict[str, str]) -> str:
    normalized = value.replace("\\", "/")
    for old, new in sorted(mapping.items(), key=lambda pair: len(pair[0]), reverse=True):
        prefix = old.replace("\\", "/").rstrip("/")
        if normalized == prefix or normalized.startswith(prefix + "/"):
            if Path(new).is_absolute():
                suffix = normalized[len(prefix):].lstrip("/")
                return str(Path(new) / suffix) if suffix else str(Path(new))
            return "./" + new + normalized[len(prefix):]
    return value


def _prepare_sqlite(path: Path, mapping: dict[str, str]) -> None:
    with closing(sqlite3.connect(path)) as database, database:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError("Restored SQLite database failed integrity check")
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "media_assets" in tables:
            rows = database.execute("SELECT id, local_path FROM media_assets WHERE local_path IS NOT NULL").fetchall()
            for asset_id, path_value in rows:
                replacement = _portable_media_path(path_value, mapping)
                if replacement != path_value:
                    database.execute("UPDATE media_assets SET local_path=? WHERE id=?", (replacement, asset_id))
        # Old queued commands must not unexpectedly run when devices reconnect.
        if "agent_commands" in tables:
            columns = {row[1] for row in database.execute("PRAGMA table_info(agent_commands)")}
            completed_at = ", completed_at=CURRENT_TIMESTAMP" if "completed_at" in columns else ""
            database.execute(f"UPDATE agent_commands SET status='failed', result=?{completed_at} WHERE status IN ('pending', 'delivered', 'awaiting_media')", (json.dumps(CANCELLED_COMMAND_RESULT),))
        if "heartbeat_sources" in tables:
            database.execute("UPDATE heartbeat_sources SET is_online=0")
        for table, statement in RESTORE_EPHEMERAL_UPDATES.items():
            if table in tables:
                database.execute(statement)


def _restore_postgres(path: Path, url: str, mapping: dict[str, str]) -> None:
    env = _postgres_env(url)
    count = _pg_run(["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-c", "SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')"], env).strip()
    if count != "0":
        raise MigrationError("Restore requires an empty destination PostgreSQL database; existing data was not changed")
    # Convert locally, then restore and adjust in ONE transaction. A failure leaves
    # the new database empty and retryable rather than half-activated.
    sql_path = path.with_suffix(".restore.sql")
    adjustments_path = path.with_suffix(".adjustments.sql")
    _pg_run(["pg_restore", "--no-owner", "--no-acl", "--file", str(sql_path), str(path)], env)
    literal = lambda value: "'" + value.replace("'", "''") + "'"
    edits = ["SET standard_conforming_strings = on;"]
    for old, new in sorted(mapping.items(), key=lambda pair: len(pair[0]), reverse=True):
        prefix = old.rstrip("/")
        target_prefix = new if Path(new).is_absolute() else './' + new
        update = f"UPDATE media_assets SET local_path={literal(target_prefix)} || substring(local_path from {len(prefix) + 1}) WHERE local_path={literal(prefix)} OR left(local_path, {len(prefix) + 1})={literal(prefix + '/')}"
        # The quoted DO body is an SQL literal, so a path cannot terminate it.
        body = f"BEGIN IF to_regclass('media_assets') IS NOT NULL THEN EXECUTE {literal(update)}; END IF; END"
        edits.append(f"DO {literal(body)};")
    result_literal = literal(json.dumps(CANCELLED_COMMAND_RESULT))
    body = f"""BEGIN
        IF to_regclass('agent_commands') IS NOT NULL THEN
            IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agent_commands' AND column_name='completed_at') THEN
                UPDATE agent_commands SET completed_at=CURRENT_TIMESTAMP WHERE status IN ('pending','delivered','awaiting_media');
            END IF;
            UPDATE agent_commands SET status='failed', result={result_literal} WHERE status IN ('pending','delivered','awaiting_media');
        END IF;
        IF to_regclass('heartbeat_sources') IS NOT NULL THEN UPDATE heartbeat_sources SET is_online=false; END IF;
    END"""
    edits.append(f"DO {literal(body)};")
    for table, statement in RESTORE_EPHEMERAL_UPDATES.items():
        body = f"BEGIN IF to_regclass({literal(table)}) IS NOT NULL THEN EXECUTE {literal(statement)}; END IF; END"
        edits.append(f"DO {literal(body)};")
    adjustments_path.write_text("\n".join(edits), encoding="utf-8")
    try:
        _pg_run(["psql", "-X", "--single-transaction", "-v", "ON_ERROR_STOP=1", "-f", str(sql_path), "-f", str(adjustments_path)], env)
    finally:
        sql_path.unlink(missing_ok=True)
        adjustments_path.unlink(missing_ok=True)


def restore_server_archive(archive_path: Path, target: Path, *, postgres_url: str | None = None, max_bytes: int = 100 * 1024**3) -> dict[str, Any]:
    """Verify/stage then publish into an EMPTY target. Never runs archived code."""
    # Serialize/validate before ANY external database write. Validation after a
    # committed restore would strand a populated DB with no activated files.
    postgres_environment = _env_text({"database_url": postgres_url}) if postgres_url else ""
    if postgres_url:
        _postgres_env(postgres_url)
    target = Path(target).absolute()
    _no_symlinks(target)
    if target == target.parent or target == Path.home() or target == Path.cwd():
        raise MigrationError("Choose a new dedicated installation directory")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise MigrationError("Target must be empty; existing installation was not changed")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".xass-restore-", dir=target.parent) as temporary:
        stage = Path(temporary) / "installation"
        stage.mkdir(mode=0o700)
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                manifest, members = _validated_archive(archive, max_bytes)
                total = sum(spec["size"] for spec in manifest["files"].values())
                if shutil.disk_usage(target.parent).free < total + 64 * 1024**2:
                    raise MigrationError("Not enough free space to restore safely")
                if manifest["database"]["engine"] == "postgresql" and not postgres_url:
                    raise MigrationError("Set XASS_RESTORE_DATABASE_URL to an EMPTY new PostgreSQL database; the old database is never reused automatically")
                if manifest["database"]["engine"] == "postgresql":
                    for command in ("psql", "pg_restore"):
                        if shutil.which(command) is None:
                            raise MigrationError(f"{command} is missing. Install the PostgreSQL client matching your database version before restore.")
                for name, spec in manifest["files"].items():
                    output = stage.joinpath(*PurePosixPath(name).parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    _read_verified(archive, members[name], spec, output)
                    output.chmod(0o600 if name == ".env" or name.startswith("data/") else (0o755 if int(spec.get("mode", 0)) & 0o111 else 0o644))
        except (tarfile.TarError, EOFError) as exc:
            raise MigrationError("Truncated or damaged backup archive") from exc
        database_path = stage / manifest["database"]["path"]
        if manifest["database"]["engine"] == "sqlite":
            _prepare_sqlite(database_path, manifest.get("path_map", {}))
        else:
            _restore_postgres(database_path, postgres_url, manifest.get("path_map", {}))
            with (stage / ".env").open("a", encoding="utf-8") as handle:
                handle.write(postgres_environment)
        with (stage / ".env").open("a", encoding="utf-8") as handle:
            handle.write(_env_text({"server_backup_dir": "../." + target.name + "-backups"}))
        # Persist provenance, but omit source machine paths and any credentials.
        (stage / "data").mkdir(exist_ok=True)
        (stage / "data" / "migration-receipt.json").write_text(json.dumps({"format": FORMAT, "version": VERSION, "restored_at": datetime.now(timezone.utc).isoformat(), "archive_sha256": _hash_file(archive_path), "files": len(manifest["files"]), "source_revision": manifest.get("source_revision", "")}, indent=2), encoding="utf-8")
        _no_symlinks(target)
        if target.exists():
            target.rmdir()  # Only a verified still-empty directory; never recursive.
        stage.rename(target)
    return {"target": str(target), "files": len(manifest["files"]), "database": manifest["database"]["engine"], "next": "Follow docs/MIGRATION.md to activate services and switch the HTTPS domain"}
