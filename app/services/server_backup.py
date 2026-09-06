"""Portable XASS snapshots. Plaintext exists only in private temporary directories."""
from __future__ import annotations

import base64
import asyncio
import io
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from contextlib import closing

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy.engine import make_url

from app.config import Settings

MAGIC = b"XASSBK1\n"
CHUNK = 1024 * 1024
MAX_BYTES = 32 * 1024**3
IGNORED = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"}
PATH_FIELDS = (
    "media_root", "export_root", "profile_json_path", "profile_backups_dir",
    "profile_audit_log_path", "profile_avatars_dir", "conversation_avatar_cache_dir",
    "projects_json_path", "site_config_json_path", "quotes_json_path", "scenarios_json_path",
    "rules_json_path", "projects_backups_dir", "projects_audit_log_path", "projects_assets_dir",
    "backgrounds_assets_dir", "telegram_bot_identity_cache_path", "pwa_session_generation_path",
    "agent_update_cache_dir", "agent_installer_path", "agent_installer_metadata_path",
    "agent_workspace_dir", "agent_migration_export_dir", "update_log_path", "update_state_path",
    "restart_notice_path",
)


def resolved(root: Path, value: str) -> Path:
    return (root / value).resolve()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _derive(password: str, salt: bytes) -> bytes:
    if not 12 <= len(password) <= 256:
        raise ValueError("Пароль должен содержать от 12 до 256 символов")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def _oaep():
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=MAGIC)


def encrypt_file(source: Path, destination: Path, *, password: str = "", public_key: bytes = b"") -> None:
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("Архив превышает лимит 32 ГБ")
    nonce = os.urandom(12)
    header = {"version": 1, "nonce": _b64(nonce)}
    if public_key:
        recipient = serialization.load_pem_public_key(public_key)
        if not isinstance(recipient, rsa.RSAPublicKey) or recipient.key_size < 3072:
            raise ValueError("Нужен публичный RSA-ключ не короче 3072 бит")
        key = os.urandom(32)
        header.update(kind="rsa-oaep-sha256", wrapped_key=_b64(recipient.encrypt(key, _oaep())))
    else:
        salt = os.urandom(16)
        key = _derive(password, salt)
        header.update(kind="scrypt", salt=_b64(salt))
    raw = json.dumps(header, separators=(",", ":")).encode()
    aad = MAGIC + struct.pack(">I", len(raw)) + raw
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(aad)
    created = False
    try:
        with source.open("rb") as src, destination.open("xb") as dst:
            created = True
            os.chmod(destination, 0o600)
            dst.write(aad)
            while block := src.read(CHUNK):
                dst.write(cipher.update(block))
            dst.write(cipher.finalize())
            dst.write(cipher.tag)
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise


def decrypt_file(source: Path, destination: Path, *, password: str = "", private_key: bytes = b"") -> None:
    """Never expose unauthenticated plaintext: publish only after GCM finalization."""
    with tempfile.TemporaryDirectory(prefix=".xass-decrypt-", dir=destination.parent) as tmp:
        output = Path(tmp) / "verified.tar.gz"
        try:
            with source.open("rb") as src:
                prefix = src.read(len(MAGIC) + 4)
                if prefix[:len(MAGIC)] != MAGIC or len(prefix) != len(MAGIC) + 4:
                    raise ValueError("Это не архив сервера XASS")
                length = struct.unpack(">I", prefix[-4:])[0]
                if not 1 <= length <= 8192:
                    raise ValueError("Повреждён заголовок архива")
                raw = src.read(length)
                header = json.loads(raw)
                if header.get("version") != 1:
                    raise ValueError("Неподдерживаемая версия архива")
                nonce = base64.b64decode(header["nonce"], validate=True)
                if len(nonce) != 12:
                    raise ValueError("Повреждён заголовок архива")
                if header["kind"] == "scrypt":
                    salt = base64.b64decode(header["salt"], validate=True)
                    if len(salt) != 16:
                        raise ValueError("Повреждён заголовок архива")
                    key = _derive(password, salt)
                elif header["kind"] == "rsa-oaep-sha256" and private_key:
                    recipient = serialization.load_pem_private_key(private_key, password=None)
                    if not isinstance(recipient, rsa.RSAPrivateKey):
                        raise ValueError("Нужен приватный RSA-ключ")
                    key = recipient.decrypt(base64.b64decode(header["wrapped_key"], validate=True), _oaep())
                else:
                    raise ValueError("Для этого архива нужен приватный ключ получателя")
                remaining = source.stat().st_size - len(prefix) - length - 16
                if not 0 <= remaining <= MAX_BYTES:
                    raise ValueError("Неверный размер архива")
                src.seek(-16, os.SEEK_END)
                tag = src.read(16)
                src.seek(len(prefix) + length)
                cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
                cipher.authenticate_additional_data(prefix + raw)
                with output.open("xb") as dst:
                    os.chmod(output, 0o600)
                    while remaining:
                        block = src.read(min(CHUNK, remaining))
                        if not block:
                            raise ValueError("Архив обрезан")
                        remaining -= len(block)
                        dst.write(cipher.update(block))
                    dst.write(cipher.finalize())
            # Exclusive creation also prevents overwriting a user's existing archive.
            with destination.open("xb") as dst, output.open("rb") as src:
                os.chmod(destination, 0o600)
                shutil.copyfileobj(src, dst, CHUNK)
        except (InvalidTag, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("Неверный пароль/ключ или архив повреждён") from exc


def postgres_env(database_url: str) -> dict[str, str]:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("Нужна PostgreSQL DATABASE_URL")
    env = os.environ.copy()
    for name, value in {"PGHOST": url.host, "PGPORT": url.port, "PGUSER": url.username,
                        "PGPASSWORD": url.password, "PGDATABASE": url.database}.items():
        if value is not None:
            env[name] = str(value)
    for name in ("sslmode", "sslrootcert", "sslcert", "sslkey"):
        if name in url.query:
            env["PG" + name.upper()] = str(url.query[name])
    return env


def snapshot_database(settings: Settings, root: Path, directory: Path) -> tuple[str, set[Path]]:
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise ValueError("База в памяти не поддерживается")
        source = resolved(root, url.database)
        if not source.is_file():
            raise ValueError("Файл базы данных не найден")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(directory / "sqlite.db")) as dst:
                src.backup(dst, pages=1024, sleep=0.05)
                dst.execute("PRAGMA journal_mode=DELETE")
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Проверка SQLite не пройдена")
        return "sqlite", {source, Path(str(source) + "-wal"), Path(str(source) + "-shm")}
    if url.get_backend_name() == "postgresql":
        if not shutil.which("pg_dump"):
            raise ValueError("Установите postgresql-client той же версии, что и сервер БД")
        result = subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(directory / "postgres.dump")],
            env=postgres_env(settings.database_url), capture_output=True, timeout=1800,
        )
        if result.returncode:
            # Do not return PostgreSQL connection details or passwords through the API.
            raise ValueError("pg_dump завершился с ошибкой; проверьте доступ и версию postgresql-client")
        return "postgresql", set()
    raise ValueError("Поддерживаются SQLite и PostgreSQL")


def create_snapshot(root: Path, settings: Settings, destination: Path, *, password: str = "",
                    public_key: bytes = b"", system_config: bool = False) -> dict:
    root = root.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("Архив с таким именем уже существует")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_dir = resolved(root, settings.server_backup_dir)
    if backup_dir == root or backup_dir in root.parents:
        raise ValueError("Каталог копий должен быть отдельным каталогом")
    manifest = {"format": "xass-server", "version": 1, "created_at": time.time(),
                "source_root": str(root), "path_map": {}, "skipped_links": [], "unreadable_system_paths": [],
                "scope": "XASS code, database, configured files and local data; not an OS disk image",
                "system_config": system_config}
    revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    manifest["revision"] = revision.stdout.strip() if revision.returncode == 0 else ""
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=destination.parent) as tmp:
        temporary = Path(tmp)
        database = temporary / "database"
        database.mkdir()
        kind, excluded = snapshot_database(settings, root, database)
        manifest["database"] = kind
        seen = set()
        archive_path = temporary / "server.tar.gz"
        with tarfile.open(archive_path, "w:gz", compresslevel=3) as tar:
            def add_tree(source: Path, name: str, *, system: bool = False):
                if not source.exists():
                    return
                if source.is_symlink():
                    if system and source.resolve().is_relative_to(Path("/etc")):
                        source = source.resolve()
                    else:
                        manifest["skipped_links"].append(name)
                        return
                actual = source.resolve()
                if (actual == backup_dir or backup_dir in actual.parents or actual == destination
                        or actual == temporary or temporary in actual.parents or actual in excluded):
                    return
                if name in seen:
                    return
                seen.add(name)
                if source.is_dir():
                    for child in sorted(source.iterdir()):
                        if child.name not in IGNORED and not child.name.endswith(".pyc"):
                            try:
                                add_tree(child, name + "/" + child.name, system=system)
                            except PermissionError:
                                if not system:
                                    raise
                                manifest["unreadable_system_paths"].append(name + "/" + child.name)
                elif source.is_file():
                    tar.add(source, arcname=name, recursive=False)

            add_tree(root, "payload")
            for field in PATH_FIELDS:
                source = resolved(root, getattr(settings, field))
                if source.is_relative_to(root):
                    manifest["path_map"][field] = str(source.relative_to(root))
                else:
                    portable = "restored/" + field
                    manifest["path_map"][field] = portable
                    add_tree(source, "payload/" + portable)
            # Snapshot files are intentionally in the excluded temporary tree.
            for snapshot in database.iterdir():
                tar.add(snapshot, arcname="database/" + snapshot.name, recursive=False)
            effective = settings.model_dump(mode="json")
            for name, content in {"settings.json": effective}.items():
                raw = json.dumps(content, ensure_ascii=False).encode()
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(raw), 0o600
                tar.addfile(info, io.BytesIO(raw))
            if system_config:
                for path in [Path("/etc/nginx"), Path("/etc/letsencrypt"),
                             *Path("/etc/systemd/system").glob("serverredus*"),
                             *Path("/etc/systemd/system").glob("xass*")]:
                    try:
                        add_tree(path, "system" + str(path), system=True)
                    except PermissionError:
                        manifest["unreadable_system_paths"].append("system" + str(path))
            raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(raw), 0o600
            tar.addfile(info, io.BytesIO(raw))
        after = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
        if manifest["revision"] and after.stdout.strip() != manifest["revision"]:
            raise ValueError("Во время копирования изменился коммит. Повторите после завершения обновления")
        encrypt_file(archive_path, destination, password=password, public_key=public_key)
    return manifest


def extract_verified_tar(source: Path, destination: Path) -> dict:
    """Reject traversal, links, special files, duplicate entries and oversized archives."""
    total = 0
    names = set()
    with tarfile.open(source, "r:gz") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or not path.parts or path.parts[0] not in {"payload", "database", "system", "manifest.json", "settings.json"}
                    or not (member.isfile() or member.isdir()) or str(path) in names):
                raise ValueError("Небезопасное содержимое архива")
            names.add(str(path))
            total += member.size
            if total > MAX_BYTES or len(names) > 500000:
                raise ValueError("Распакованный архив превышает лимит")
            target = destination / str(path)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tar.extractfile(member) as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            os.chmod(target, 0o700 if member.mode & 0o111 else 0o600)
    manifest = json.loads((destination / "manifest.json").read_text())
    if manifest.get("format") != "xass-server" or manifest.get("version") != 1:
        raise ValueError("Неподдерживаемая версия снимка")
    return manifest


def restore_snapshot(source: Path, destination: Path, *, password: str = "", private_key: bytes = b"",
                     postgres_url: str = "") -> dict:
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Каталог назначения уже существует; укажите новый каталог")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".xass-restore-", dir=destination.parent) as tmp:
        work = Path(tmp)
        archive = work / "verified.tar.gz"
        decrypt_file(source, archive, password=password, private_key=private_key)
        unpacked = work / "unpacked"
        unpacked.mkdir()
        manifest = extract_verified_tar(archive, unpacked)
        payload = unpacked / "payload"
        values = json.loads((unpacked / "settings.json").read_text())
        for name, relative in manifest["path_map"].items():
            if name not in PATH_FIELDS or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                raise ValueError("Неверный путь в манифесте")
            values[name] = "./" + relative
        values["server_backup_dir"] = "../." + destination.name + "-backups"
        values["polling_drop_pending_updates"] = False
        data = payload / "data"
        data.mkdir(exist_ok=True)
        mapping = {manifest["source_root"]: str(destination)}
        original = json.loads((unpacked / "settings.json").read_text())
        for field, relative in manifest["path_map"].items():
            mapping[str(resolved(Path(manifest["source_root"]), original[field]))] = str(destination / relative)

        def relocate(path):
            for old, new in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
                if path == old or path.startswith(old.rstrip("/") + "/"):
                    return new + path[len(old):]
            return path

        if manifest["database"] == "sqlite":
            shutil.copy2(unpacked / "database/sqlite.db", data / "serverredus.db")
            values["database_url"] = "sqlite+aiosqlite:///./data/serverredus.db"
            # Stored media paths can be absolute, including an external MEDIA_ROOT.
            with closing(sqlite3.connect(data / "serverredus.db")) as db, db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "media_assets" in tables:
                    rows = db.execute("SELECT id, local_path FROM media_assets WHERE local_path IS NOT NULL").fetchall()
                    for ident, path in rows:
                        db.execute("UPDATE media_assets SET local_path=? WHERE id=?", (relocate(path), ident))
        elif manifest["database"] == "postgresql":
            if not postgres_url or not shutil.which("pg_restore") or not shutil.which("psql"):
                raise ValueError("Для PostgreSQL укажите новую пустую БД и установите postgresql-client")
            env = postgres_env(postgres_url)
            check = subprocess.run(["psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-c",
                                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                                    "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','v','m','S','f','p')"],
                                   env=env, capture_output=True, text=True, timeout=30)
            if check.returncode or check.stdout.strip() != "0":
                raise ValueError("БД назначения недоступна или не пуста; восстановление отменено")
            result = subprocess.run(["pg_restore", "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl",
                                     "--dbname", env["PGDATABASE"], str(unpacked / "database/postgres.dump")],
                                    env=env, capture_output=True, timeout=1800)
            if result.returncode:
                raise ValueError("pg_restore не выполнен; транзакция отменена")
            values["database_url"] = make_url(postgres_url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)

            async def relocate_postgres():
                import asyncpg
                dsn = make_url(postgres_url).set(drivername="postgresql").render_as_string(hide_password=False)
                connection = await asyncpg.connect(dsn, timeout=30)
                try:
                    async with connection.transaction():
                        if await connection.fetchval("SELECT to_regclass('public.media_assets')"):
                            for row in await connection.fetch("SELECT id, local_path FROM media_assets WHERE local_path IS NOT NULL"):
                                await connection.execute("UPDATE media_assets SET local_path=$1 WHERE id=$2", relocate(row["local_path"]), row["id"])
                finally:
                    await connection.close()
            asyncio.run(relocate_postgres())
        else:
            raise ValueError("Неподдерживаемая база данных")
        # Keep original environment for review; effective settings include process-only secrets.
        if (payload / ".env").exists():
            (payload / ".env").rename(payload / ".env.source")
        csv_fields = {"authorized_user_ids", "admin_user_ids", "monitored_services"}
        lines = []
        for name, value in values.items():
            if value is None:
                value = ""
            elif name in csv_fields:
                value = ",".join(map(str, value))
            elif isinstance(value, bool):
                value = "true" if value else "false"
            # Quote spaces, newlines and literal apostrophes in dotenv values.
            escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
            lines.append(name.upper() + "='" + escaped + "'")
        (payload / ".env").write_text("\n".join(lines) + "\n")
        os.chmod(payload / ".env", 0o600)
        (payload / ".migration-pending").write_text("Stop the source backend and agent before activating this copy.\n")
        shutil.copy2(unpacked / "manifest.json", payload / "migration-manifest.json")
        if (unpacked / "system").exists():
            shutil.move(str(unpacked / "system"), str(payload / "migration-system"))
        payload.rename(destination)
    return manifest
