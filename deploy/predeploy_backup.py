#!/usr/bin/env python3
"""Private, bounded rollback snapshot; run from the checkout, including via stdin.

Usage: .venv/bin/python - RUN_ID < deploy/predeploy_backup.py
This is not a server migration backup: untracked media/caches are untouched by a
code deployment and deliberately excluded. A snapshot is ready only when this
command exits successfully and COMPLETE.json exists. Never auto-restore its DB
after deployment: doing so could discard messages received since the snapshot.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time


class BackupError(RuntimeError):
    pass


class Deadline:
    def __init__(self, seconds: float = 120):
        self.expires = time.monotonic() + seconds

    def remaining(self) -> float:
        remaining = self.expires - time.monotonic()
        if remaining <= 0:
            raise BackupError("Backup exceeded its time limit; deployment must stop.")
        return remaining


def _run(command: list[str], root: Path, deadline: Deadline, *, output=None) -> bytes:
    """Bound children, including pg_dump descendants; never print their stderr."""
    with subprocess.Popen(
        command, cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if output is None else output,
        stderr=subprocess.PIPE, start_new_session=os.name == "posix",
    ) as process:
        try:
            stdout, _stderr = process.communicate(timeout=deadline.remaining())
        except BaseException as exc:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.communicate()
            if isinstance(exc, (subprocess.TimeoutExpired, BackupError)):
                raise BackupError("Backup exceeded its time limit or was interrupted; deployment must stop.") from None
            raise
        if process.returncode:
            raise BackupError("Backup subprocess failed; deployment must stop.")
        return stdout or b""


def _no_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise BackupError("Backup paths must not contain symlinks or junctions.")


def _private_file(path: Path, content: bytes) -> None:
    with path.open("xb") as output:
        os.chmod(path, 0o600)
        output.write(content)


def _copy_env(root: Path, target: Path) -> None:
    source = root / ".env"
    _no_links(source)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(source, flags), "rb") as incoming:
        if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
            raise BackupError("The server .env must be a regular private configuration file.")
        content = incoming.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise BackupError("The server .env exceeds the configuration size limit.")
    _private_file(target / ".env", content)


DATABASE_WORKER = """
import os, sys
from pathlib import Path
os.umask(0o077)
root = Path.cwd()
sys.path.insert(0, str(root))
from app.config import Settings
from app.services.server_backup import snapshot_database
directory = Path(sys.argv[1])
kind, _ = snapshot_database(Settings(_env_file=root / '.env'), root, directory)
with (directory / 'kind').open('x', encoding='ascii') as output:
    output.write(kind)
"""


def create_backup(root: Path, run_id: str, *, timeout_seconds: float = 120) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", run_id):
        raise BackupError("Invalid run ID; use only letters, digits, hyphens and underscores.")
    if not 0 < timeout_seconds <= 120:
        raise BackupError("Backup timeout must be between zero and 120 seconds.")
    deadline = Deadline(timeout_seconds)
    root = root.absolute()
    _no_links(root)
    root = root.resolve(strict=True)
    backup_parent = root.parent / ".xass-predeploy"
    _no_links(backup_parent)
    previous_umask = os.umask(0o077)
    try:
        backup_parent.mkdir(mode=0o700, exist_ok=True)
        if not backup_parent.is_dir():
            raise BackupError("The backup parent is not a directory.")
        if hasattr(os, "geteuid") and backup_parent.stat().st_uid != os.geteuid():
            raise BackupError("The backup directory must be owned by the deployment user.")
        os.chmod(backup_parent, 0o700)
        destination = backup_parent / run_id
        destination.mkdir(mode=0o700)  # Exclusive: never reuse/overwrite an earlier run.
        print("[predeploy] Checking tracked checkout and recording current revision...", flush=True)
        git = ["git", "-C", str(root)]
        if _run(git + ["status", "--porcelain", "--untracked-files=no"], root, deadline).strip():
            raise BackupError("Tracked server changes need review; deployment must stop.")
        revision = _run(git + ["rev-parse", "--verify", "HEAD"], root, deadline).strip()
        if not re.fullmatch(rb"[0-9a-f]{40,64}", revision):
            raise BackupError("Could not validate the current Git revision.")
        _private_file(destination / "before-revision", revision + b"\n")
        print("[predeploy] Archiving tracked code only (no untracked media/cache)...", flush=True)
        with (destination / "code.tar.gz").open("xb") as archive:
            os.chmod(destination / "code.tar.gz", 0o600)
            _run(git + ["archive", "--format=tar.gz", revision.decode("ascii")], root, deadline, output=archive)
        print("[predeploy] Copying .env into the private rollback directory...", flush=True)
        _copy_env(root, destination)
        database = destination / "database"
        database.mkdir(mode=0o700)
        print("[predeploy] Creating consistent online database snapshot (bounded timeout)...", flush=True)
        _run([sys.executable, "-c", DATABASE_WORKER, str(database)], root, deadline)
        kind = (database / "kind").read_text(encoding="ascii")
        if kind not in {"sqlite", "postgresql"}:
            raise BackupError("The database snapshot did not report a supported format.")
        database_file = database / ("sqlite.db" if kind == "sqlite" else "postgres.dump")
        if not database_file.is_file() or database_file.stat().st_size == 0:
            raise BackupError("The database snapshot is missing or empty.")
        for path in database.iterdir():
            _no_links(path)
            if not path.is_file():
                raise BackupError("Unexpected database snapshot output.")
            os.chmod(path, 0o600)
        if _run(git + ["rev-parse", "HEAD"], root, deadline).strip() != revision:
            raise BackupError("The checkout revision changed during backup; deployment must stop.")
        deadline.remaining()
        _private_file(destination / "COMPLETE.json", json.dumps({
            "format": "xass-predeploy-rollback", "version": 1,
            "revision": revision.decode("ascii"), "database": kind,
            "scope": "tracked code, original .env, consistent database; no runtime media or caches",
        }, indent=2).encode("utf-8"))
        print("[predeploy] READY: private code/config/database rollback snapshot completed.", flush=True)
        return destination
    finally:
        os.umask(previous_umask)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage from the server checkout: .venv/bin/python - RUN_ID < predeploy_backup.py", file=sys.stderr, flush=True)
        return 2
    handlers = {}

    def interrupted(_signum, _frame):
        raise BackupError("Backup interrupted; deployment must stop.")

    # An SSH disconnect/runner cancellation must also stop any DB child group.
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            sig = getattr(signal, name)
            handlers[sig] = signal.signal(sig, interrupted)
    try:
        create_backup(Path.cwd(), sys.argv[1])
    except BackupError as exc:
        print(f"[predeploy] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    except Exception:
        # Settings validation/subprocess/OS exceptions can embed tokens or paths.
        print("[predeploy] FAILED: configuration/filesystem/database error; deployment must stop. Any incomplete private snapshot is not ready for rollback.", file=sys.stderr, flush=True)
        return 1
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
