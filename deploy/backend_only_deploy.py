#!/usr/bin/env python3
"""One explicitly approved server-only release; never publish/update clients.

Run the reviewed script from stdin with the server's existing .venv Python.
All commands are bounded, stderr is private, and rollback never restores a DB.
The private receipt records the overlay SHA separately from its backend source.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

BEFORE = "81601bb56190c564c1f25928f3c60c616ff9cdf4"
SOURCE = "01f565f934cda8754b1ab70c8a67ac34edbb9e2f"
PUBLIC_KEY = "44263961fe17874feeb002f663d5c71f7ce23461347ba6d0ac2becc3627cd13e"
LISTEN_URL = "https://redvps.site"
ROOT = Path("/home/red/serverredus")
REQUIRED_SOURCE = ("app", "index.php", "proxy.php")
OPTIONAL_SOURCE = ("miniapp.php", "assets/miniapp-hermes.css", "assets/miniapp-hermes.js")
OPERATIONS = {".github/workflows/checks.yml", ".github/workflows/server-route-diagnostics.yml",
              "deploy/backend_only_deploy.py", "deploy/nginx_private_guard.py",
              "deploy/backend_startup_diagnostics.py"}


class DeployError(RuntimeError):
    pass


class UnsettledCommandError(DeployError):
    """A surviving command could still submit a future service job."""


def run(root: Path, command: list[str], *, timeout: float = 30) -> bytes:
    """Never echo subprocess output/errors: they can contain private settings."""
    # Popen.__exit__ calls wait() without a timeout. Do not use its context
    # manager: a detached/privileged descendant can keep inherited pipes open.
    child = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                             start_new_session=os.name == "posix")
    try:
        try:
            out, _ = child.communicate(timeout=timeout)
        except BaseException:
            try:
                if os.name == "posix":
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    child.kill()
            except OSError:
                # A root-owned descendant may not be killable by this user.
                # Cleanup must still finish within a bounded interval.
                try:
                    child.kill()
                except OSError:
                    pass
            try:
                child.communicate(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass
            finally:
                for stream in (child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise UnsettledCommandError("Command cleanup did not settle; manual review required.") from None
            raise
        if child.returncode:
            raise DeployError("A bounded deployment command failed; private output was not printed.")
        if len(out) > 2 * 1024 * 1024:
            raise DeployError("Deployment command output exceeded the safety bound.")
        return out
    finally:
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()


def systemd_timeout_seconds(value: str) -> float:
    """Parse bounded systemctl *USec values, not arbitrary unit expressions."""
    if not value or len(value) > 80:
        raise DeployError("Missing or unsupported systemd timeout; restart refused.")
    if value.isdecimal():
        seconds = int(value) / 1_000_000  # Raw D-Bus microseconds.
    else:
        units = {"us": .000001, "ms": .001, "s": 1, "min": 60, "h": 3600}
        seconds, position = 0.0, 0
        for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(us|ms|min|s|h)", value):
            if value[position:match.start()].strip():
                raise DeployError("Unsupported systemd timeout; restart refused.")
            seconds += float(match.group(1)) * units[match.group(2)]
            position = match.end()
        if not position or value[position:].strip():
            raise DeployError("Unsupported systemd timeout; restart refused.")
    if not math.isfinite(seconds) or not 0 < seconds <= 300:
        raise DeployError("Unbounded systemd timeout requires manual review.")
    return seconds


def no_links(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise DeployError("Deployment paths must not contain links.")


def private_read(path: Path, *, limit: int = 2 * 1024 * 1024) -> bytes:
    no_links(path)
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as incoming:
        info = os.fstat(incoming.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DeployError("Configuration/receipt must be a regular unlinked file.")
        value = incoming.read(limit + 1)
        if len(value) > limit:
            raise DeployError("Private file exceeds its safety bound.")
        return value


def atomic_private(path: Path, value: bytes, *, metadata=None, mode: int | None = None) -> None:
    no_links(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".xass-deploy-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            if metadata is not None:
                os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            permissions = mode if mode is not None else stat.S_IMODE(metadata.st_mode) if metadata is not None else 0o600
            os.chmod(temporary, permissions)
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configured_env(original: bytes, effective_hermes: str) -> bytes:
    """Replace only Discord keys; pin old Hermes default only when unspecified."""
    text = original.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    keys = {match.group(1).casefold() for line in lines if (match := pattern.match(line))}
    replace = {"discord_public_key": PUBLIC_KEY, "discord_listen_url": LISTEN_URL}
    if not {"hermes_url", "hermes_webui_url"} & keys:
        if (not isinstance(effective_hermes, str) or len(effective_hermes) > 2048
                or any(ord(char) < 32 for char in effective_hermes)):
            raise DeployError("Cannot preserve the current Hermes endpoint safely.")
        # JSON quoting is compatible with dotenv double-quoted string parsing.
        replace["hermes_url"] = json.dumps(effective_hermes, ensure_ascii=False)
    kept = [line for line in lines if not ((match := pattern.match(line))
            and match.group(1).casefold() in replace)]
    result = "".join(kept)
    if result and not result.endswith(("\n", "\r")):
        result += "\n"
    result += "".join(f"{key.upper()}={value}\n" for key, value in replace.items())
    return result.encode("utf-8")


@contextmanager
def deployment_lock(root: Path):
    if os.name != "posix":
        raise DeployError("Live deployment requires the POSIX VPS lock.")
    import fcntl
    path = root.parent / ".xass-deployment.lock"
    no_links(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise DeployError("Deployment lock is not owned by this deployment user.")
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeployError("Another deployment still holds the production lock.")
                time.sleep(.25)
        yield
    finally:
        os.close(descriptor)


SNAPSHOT = r'''
import hashlib,json
from pathlib import Path
from app.config import Settings
from app.services.agent_updates import _client_root,_package_files,_content_revision
s=Settings()
root=_client_root()
digests=[]
for raw in (s.agent_installer_path,s.agent_installer_metadata_path):
    p=Path(raw)
    if not p.exists():
        digests.append(None)
        continue
    if not p.is_file() or p.is_symlink() or p.stat().st_size>512*1024*1024:
        raise RuntimeError('Unsafe installer path')
    h=hashlib.sha256()
    with p.open('rb') as f:
        while block:=f.read(1024*1024): h.update(block)
    digests.append(h.hexdigest())
print(json.dumps({'source_revision':_content_revision(root,_package_files(root)),
                 'installers':digests,'hermes_url':s.hermes_url}))
'''

# This imports settings/URL parsing only. It never opens or connects to SQLite,
# and neither the configured URL nor any secret is written to stdout/stderr here.
STORAGE_CONFIG_CHECK = r'''
import os
from pathlib import Path
from sqlalchemy.engine import make_url
from app.config import Settings
url = make_url(Settings().database_url)
expected = Path.cwd() / 'data' / 'serverredus.db'
valid = (url.get_backend_name() == 'sqlite' and bool(url.database)
         and not url.host and not url.query and not url.username and not url.password)
if not valid or Path(os.path.abspath(url.database)) != expected:
    raise SystemExit(1)
print('FIXED_SQLITE_CONFIRMED')
'''


class Deployment:
    def __init__(self, root: Path, target: str, run_id: str):
        self.root, self.target, self.run_id = root.absolute(), target, run_id
        self.backup = self.root.parent / ".xass-predeploy" / run_id
        self.original_env: bytes | None = None
        self.new_env: bytes | None = None
        self.mutation_started = False
        self.env_metadata = None
        self.env_mode_before = self.env_mode_after = 0o600
        self.original_branch = None

    def phase(self, name: str) -> None:
        print(f"[PHASE] {name}", flush=True)

    def command(self, *args: str, timeout: float = 30) -> bytes:
        return run(self.root, list(args), timeout=timeout)

    def git(self, *args: str) -> bytes:
        return self.command("git", *args)

    def python(self, *args: str, timeout: float = 30) -> bytes:
        return self.command(sys.executable, *args, timeout=timeout)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").decode("ascii").strip()

    def clean(self) -> None:
        if self.git("status", "--porcelain", "--untracked-files=no").strip():
            raise DeployError("Tracked server changes require review; no overwrite is allowed.")
        if self.git("ls-files", "--", ".env").strip():
            raise DeployError("Private configuration must not be a tracked deployment file.")

    def validate(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", self.target) or self.target in {BEFORE, SOURCE}:
            raise DeployError("A distinct immutable server-only overlay commit is required.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", self.run_id):
            raise DeployError("Invalid private backup run ID.")
        no_links(self.root)
        self.clean()
        if self.head() != BEFORE:
            raise DeployError("The live revision changed; inspect before deploying again.")
        self.git("merge-base", "--is-ancestor", BEFORE, self.target)
        changed = self.git("diff", "--name-only", "-z", BEFORE, self.target).decode("utf-8").split("\0")
        for path in filter(None, changed):
            if not (path.startswith(("app/", "tests/")) or path in REQUIRED_SOURCE
                    or path in OPTIONAL_SOURCE or path in OPERATIONS):
                raise DeployError("Overlay changes a protected client/release/config path.")
        for path in (*REQUIRED_SOURCE, *(p for p in OPTIONAL_SOURCE if p in changed)):
            self.git("diff", "--exit-code", SOURCE, self.target, "--", path)
        # These scripts run during deployment; they must remain reviewed baseline bytes.
        for path in ("deploy/predeploy_backup.py", "deploy/repair_public_permissions.py",
                     "requirements.txt", "bootstrap_server_dependencies.py"):
            self.git("diff", "--exit-code", BEFORE, self.target, "--", path)

    def environment_access(self) -> None:
        """Fail before mutation instead of replacing service-readable config with an inaccessible file."""
        info = (self.root / ".env").stat()
        current_uid = os.geteuid()
        user = self.command("systemctl", "show", "serverredus-backend", "--property=User", "--value").decode().strip()
        if not user:
            service_uid = 0
        elif user.isdecimal():
            service_uid = int(user)
        else:
            import pwd
            service_uid = pwd.getpwnam(user).pw_uid
        if info.st_uid != current_uid or service_uid != current_uid:
            raise DeployError("Deployment user, configuration owner and backend user must match; inspect service permissions first.")
        mode = stat.S_IMODE(info.st_mode)
        if mode not in {0o600, 0o640, 0o644, 0o664}:
            raise DeployError("Unexpected configuration permissions require separate review.")
        if current_uid != 0 and info.st_gid not in {os.getegid(), *os.getgroups()}:
            raise DeployError("Cannot preserve the existing configuration group without extra privileges.")
        self.env_metadata = info
        self.env_mode_before = mode
        # Explicitly approved legacy permission repair: never restore world readability.
        self.env_mode_after = 0o600 if mode in {0o644, 0o664} else mode

    def snapshot(self) -> dict:
        value = json.loads(self.python("-c", SNAPSHOT, timeout=60))
        if not re.fullmatch(r"[a-f0-9]{64}", value.get("source_revision", "")):
            raise DeployError("Could not fingerprint the stable source package.")
        if not isinstance(value.get("installers"), list) or len(value["installers"]) != 2:
            raise DeployError("Could not fingerprint stable installers.")
        return value

    def backup_ready(self) -> None:
        self.python("deploy/predeploy_backup.py", self.run_id, timeout=125)
        receipt = json.loads(private_read(self.backup / "COMPLETE.json"))
        if (receipt.get("format") != "xass-predeploy-rollback" or receipt.get("version") != 1
                or receipt.get("revision") != BEFORE or receipt.get("database") not in {"sqlite", "postgresql"}):
            raise DeployError("The private rollback snapshot is incomplete or has the wrong revision.")
        if private_read(self.backup / ".env") != self.original_env:
            raise DeployError("Configuration changed during backup; deployment stopped.")

    def checkout(self, revision: str) -> None:
        self.git("-c", "core.hooksPath=/dev/null", "-c", "advice.detachedHead=false",
                 "checkout", "--detach", revision)
        if self.head() != revision:
            raise DeployError("Checkout did not reach the pinned revision.")

    def service_properties(self, *names: str, timeout: float = 10) -> dict[str, str]:
        raw = self.command("systemctl", "show", "serverredus-backend", "--no-pager", "--all",
                           *("--property=" + name for name in names), timeout=timeout)
        if len(raw) > 2048:
            raise DeployError("Unexpected service state output; manual review required.")
        try:
            lines = raw.decode("ascii").splitlines()
        except UnicodeError:
            raise DeployError("Invalid service state encoding; manual review required.") from None
        values = {}
        for line in lines:
            key, separator, value = line.partition("=")
            if not separator or key not in names or key in values:
                raise DeployError("Unexpected service state properties; manual review required.")
            values[key] = value.strip()
        if set(values) != set(names):
            raise DeployError("Incomplete service state properties; manual review required.")
        return values

    def get_restart_budget(self) -> int:
        values = self.service_properties("TimeoutStartUSec", "TimeoutStopUSec")
        budget = math.ceil(sum(systemd_timeout_seconds(values[name]) for name in values)) + 20
        if budget > 300:
            raise DeployError("Combined systemd restart budget exceeds 300s; manual review required.")
        return budget

    def restart(self) -> None:
        budget = self.get_restart_budget()
        self.phase(f"backend restart budget {budget}s includes both systemd timeouts plus 20s grace")
        self.command("sudo", "-n", "systemctl", "restart", "serverredus-backend", timeout=budget)

    def wait_service_settled(self, *, timeout: float = 30) -> None:
        """Read-only fence: never replace code under an unfinished systemd job."""
        deadline = time.monotonic() + timeout
        previous = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeployError("Service job did not settle; rollback checkout refused, manual review required.")
            state = self.service_properties("ActiveState", "SubState", "Job", timeout=min(10, remaining))
            active, sub, job = state["ActiveState"], state["SubState"], state["Job"]
            if (not re.fullmatch(r"[a-z][a-z-]{0,39}", active)
                    or not re.fullmatch(r"[a-z][a-z-]{0,39}", sub)
                    or (job not in {"", "0"} and not re.fullmatch(r"[1-9][0-9]{0,9}", job))):
                raise DeployError("Unknown service/job state; rollback checkout refused.")
            settled = (active, sub) in {("active", "running"), ("inactive", "dead"), ("failed", "failed")}
            snapshot = (active, sub) if settled and job in {"", "0"} else None
            if snapshot is not None and snapshot == previous:
                return
            previous = snapshot
            time.sleep(min(.5, max(0, deadline - time.monotonic())))

    def protect_config(self) -> None:
        """Explicit emergency permission-only repair. Never fetch configuration over HTTP."""
        self.phase("protect-config: verify live revision, same owner/service UID and regular private path")
        if self.head() != BEFORE:
            raise DeployError("Live revision differs from the inspected server; protection stopped.")
        self.clean()
        path = self.root / ".env"
        no_links(path)
        self.environment_access()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_uid, info.st_gid, info.st_ino, info.st_dev) !=
                    (self.env_metadata.st_uid, self.env_metadata.st_gid, self.env_metadata.st_ino, self.env_metadata.st_dev)):
                raise DeployError("Configuration path changed or is not a single regular file.")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self.phase(f"protect-config: permissions {self.env_mode_before:04o} -> 0600; no contents changed, no service restart")
        # This is HEAD, not GET: never download or log configuration bytes.
        code = self.command("curl", "--head", "--silent", "--show-error", "--connect-timeout", "5",
                            "--max-time", "10", "--output", "/dev/null", "--write-out", "%{http_code}",
                            LISTEN_URL + "/.env", timeout=12).strip()
        if code not in {b"403", b"404"}:
            print("[CRITICAL] Configuration is now 0600 but public HEAD is not403/404. Keep hardened permissions; nginx denial requires immediate review.", flush=True)
            raise DeployError("Public configuration exposure was not verified closed; permissions remain hardened.")
        self.health(public=False)
        body = self.command("curl", "--fail", "--silent", "--show-error", "--connect-timeout", "5",
                            "--max-time", "10", LISTEN_URL + "/health", timeout=12)
        if json.loads(body).get("status") != "ok":
            raise DeployError("Protection completed but public backend health needs review.")
        print("[OK] protect-config completed: public HEAD denied, backend healthy, config bytes and running services unchanged.", flush=True)

    def protect_storage(self) -> None:
        """Owner-only emergency containment; no sudo, DB reads, writes or restarts."""
        self.phase("protect-storage: verify exact live revision, SQLite path and same owner/service UID")
        if self.head() != BEFORE:
            raise DeployError("Live revision differs from the inspected server; storage protection stopped.")
        self.clean()
        no_links(self.root / ".env")
        self.environment_access()
        if self.python("-c", STORAGE_CONFIG_CHECK, timeout=20).strip() != b"FIXED_SQLITE_CONFIRMED":
            raise DeployError("Configured database is not the fixed approved SQLite file.")

        def public_head(path: str) -> bytes:
            result = self.command("curl", "--head", "--silent", "--show-error", "--connect-timeout", "5",
                "--max-time", "10", "--output", "/dev/null", "--write-out", "%{http_code}\t%{content_type}",
                LISTEN_URL + path, timeout=12).strip()
            if len(result) > 256 or not re.match(rb"^[1-5][0-9]{2}\t", result):
                raise DeployError("Public HEAD check did not return valid bounded metadata.")
            return result

        public_paths = ("/", "/miniapp.php", "/assets/miniapp-device-key.js")
        public_before = {path: public_head(path) for path in public_paths}
        plans = (("data/serverredus.db", 0o600, False, False),
                 ("data/serverredus.db-wal", 0o600, True, False),
                 ("data/serverredus.db-shm", 0o600, True, False),
                 (".git", 0o700, False, True))
        opened = []
        absent = []
        current_uid = os.geteuid()

        def identity(info):
            return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode)

        def validate_target(info, directory):
            correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
            if not correct_type or info.st_uid != current_uid or (not directory and info.st_nlink != 1):
                raise DeployError("Storage target is not an owned single regular file or approved directory.")

        try:
            # Open and validate EVERY target before changing ANY permissions.
            for relative, mode, optional, directory in plans:
                path = self.root / relative
                no_links(path)
                try:
                    expected = path.lstat()
                except FileNotFoundError:
                    if optional:
                        absent.append(path)
                        continue
                    raise DeployError("A required fixed storage target is missing.") from None
                validate_target(expected, directory)
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                if directory:
                    flags |= getattr(os, "O_DIRECTORY", 0)
                descriptor = os.open(path, flags)
                opened.append((path, descriptor, mode, directory, expected))
                actual = os.fstat(descriptor)
                validate_target(actual, directory)
                if identity(actual) != identity(expected):
                    raise DeployError("Storage target changed while opening; no permissions changed.")
            for path, descriptor, _mode, directory, expected in opened:
                no_links(path)
                current = path.lstat()
                validate_target(current, directory)
                if identity(current) != identity(expected) or identity(os.fstat(descriptor)) != identity(expected):
                    raise DeployError("Storage target changed before protection; no permissions changed.")
            if any(path.exists() or path.is_symlink() for path in absent):
                raise DeployError("SQLite sidecars changed during validation; inspect and retry.")
            # Closing access is intentional. Never undo these modes on later failure.
            for path, descriptor, mode, directory, expected in opened:
                os.fchmod(descriptor, mode)
            for path, descriptor, mode, directory, expected in opened:
                actual = os.fstat(descriptor)
                current = path.lstat()
                validate_target(actual, directory)
                if ((actual.st_dev, actual.st_ino, actual.st_uid, actual.st_gid) != identity(expected)[:4]
                        or stat.S_IMODE(actual.st_mode) != mode or identity(current) != identity(actual)):
                    raise DeployError("Storage changed during protection; hardened permissions retained, inspect manually.")
            if any(path.exists() or path.is_symlink() for path in absent):
                raise DeployError("A new SQLite sidecar needs inspection; existing hardened permissions retained.")
        finally:
            for _path, descriptor, _mode, _directory, _expected in opened:
                os.close(descriptor)

        self.phase("protect-storage: database/sidecars 0600, .git 0700; no bytes changed or services restarted")
        private_paths = ["/" + path.relative_to(self.root).as_posix() for path, _, _, directory, _ in opened if not directory]
        private_paths += ["/.git/config", "/.env"]
        for path in private_paths:
            if public_head(path).split(b"\t", 1)[0] not in {b"403", b"404"}:
                print("[CRITICAL] Private HEAD denial not verified; keep restricted modes, nginx still requires administrator review.", flush=True)
                raise DeployError("Storage exposure closure unverified; hardened permissions remain in place.")
        for path, previous in public_before.items():
            if public_head(path) != previous:
                raise DeployError("Public route changed after protection; restricted storage permissions retained.")
        self.health(public=False)
        body = self.command("curl", "--fail", "--silent", "--show-error", "--connect-timeout", "5",
            "--max-time", "10", LISTEN_URL + "/health", timeout=12)
        if json.loads(body).get("status") != "ok":
            raise DeployError("Storage is restricted but public backend health requires review.")
        print("[OK] protect-storage completed: private HEAD denied, public routes unchanged, backend healthy; no DB/config bytes, keys or services changed.", flush=True)

    def health(self, *, public: bool) -> None:
        ready = False
        for _ in range(20):
            try:
                value = self.command("curl", "--fail", "--silent", "--show-error", "--noproxy", "*",
                    "--connect-timeout", "1", "--max-time", "2", "http://127.0.0.1:8000/health", timeout=3)
                ready = json.loads(value).get("status") == "ok"
            except (DeployError, ValueError, subprocess.TimeoutExpired):
                pass
            if ready:
                break
            time.sleep(.5)
        if not ready:
            raise DeployError("Backend did not become healthy within the bounded startup check.")
        if public:
            response = self.command("curl", "--silent", "--show-error", "--connect-timeout", "5",
                "--max-time", "10", "--output", "/dev/null", "--write-out", "%{http_code}",
                "--request", "POST", "--header", "Content-Type: application/json",
                "--data-binary", '{"type":1}', LISTEN_URL + "/api/discord/interactions", timeout=12)
            if response.strip() != b"401":
                raise DeployError("Public Discord route did not preserve its real signature-rejection status.")
            body = self.command("curl", "--fail", "--silent", "--show-error", "--connect-timeout", "5",
                                "--max-time", "10", LISTEN_URL + "/health", timeout=12)
            if json.loads(body).get("status") != "ok":
                raise DeployError("Public backend health response was not valid.")

    def rollback(self) -> None:
        # Killing the systemctl client does NOT cancel a job already accepted by
        # PID 1. Require two matching, no-job stable states before any checkout.
        self.wait_service_settled()
        # Never overwrite new user edits or restore a DB snapshot over new messages.
        self.clean()
        if self.head() not in {BEFORE, self.target}:
            raise DeployError("Concurrent checkout change: automatic rollback refused.")
        current = private_read(self.root / ".env")
        if current not in {self.original_env, self.new_env}:
            raise DeployError("Concurrent configuration change: automatic rollback refused.")
        self.checkout(BEFORE)
        if self.original_branch and self.git("rev-parse", self.original_branch).decode().strip() == BEFORE:
            self.git("symbolic-ref", "HEAD", self.original_branch)
        atomic_private(self.root / ".env", self.original_env, metadata=self.env_metadata, mode=self.env_mode_after)
        self.python("deploy/repair_public_permissions.py")
        self.restart()
        self.health(public=False)

    def execute(self) -> None:
        self.phase("validate exact revisions, unchanged clients and private configuration access")
        self.validate()
        self.original_env = private_read(self.root / ".env")
        self.environment_access()
        # Refuse unsupported/infinite unit timeouts before touching code/config.
        self.get_restart_budget()
        try:
            self.original_branch = self.git("symbolic-ref", "--quiet", "HEAD").decode().strip()
        except DeployError:  # An already detached checkout has no original symbolic branch.
            self.original_branch = None
        self.phase("fingerprint stable packages and preserve effective Hermes endpoint")
        stable_before = self.snapshot()
        self.new_env = configured_env(self.original_env, stable_before["hermes_url"])
        self.phase("create private code, configuration and consistent database backup")
        self.backup_ready()
        # Recheck after backup and before the first mutation under the same lock.
        self.clean()
        if self.head() != BEFORE or private_read(self.root / ".env") != self.original_env:
            raise DeployError("Live code/configuration changed while preparing deployment.")
        self.wait_service_settled()
        try:
            self.mutation_started = True
            self.phase("activate server-only overlay and preserve configuration metadata")
            self.checkout(self.target)
            if self.env_mode_before != self.env_mode_after:
                self.phase(f"harden legacy configuration mode {self.env_mode_before:04o} to {self.env_mode_after:04o}; retain owner/group")
            atomic_private(self.root / ".env", self.new_env, metadata=self.env_metadata, mode=self.env_mode_after)
            self.python("deploy/repair_public_permissions.py")
            stable_after = self.snapshot()
            if stable_after != stable_before:
                raise DeployError("Stable packages or effective Hermes configuration changed.")
            self.phase("restart backend only and verify local/public health plus signature rejection")
            self.restart()
            self.health(public=True)
            self.clean()
            self.phase("save private deployment receipt; no database restore or client publication")
            atomic_private(self.backup / "DEPLOYED.json", json.dumps({
                "deployment_revision": self.target, "backend_source_revision": SOURCE,
                "before_revision": BEFORE, "source_package_revision": stable_after["source_revision"],
                "installer_hashes": stable_after["installers"], "database_restored": False,
                "configuration_mode_before": f"{self.env_mode_before:04o}",
                "configuration_mode_after": f"{self.env_mode_after:04o}",
                "discord_key_sha256": hashlib.sha256(PUBLIC_KEY.encode()).hexdigest(),
            }, indent=2).encode())
        except BaseException as failure:
            if isinstance(failure, UnsettledCommandError):
                print("[FAILED] A deployment command is still unsettled. No automatic rollback checkout; manual review required.", flush=True)
                raise DeployError("Unsettled command requires manual review; code/config retained, no database restored.") from None
            # A second termination request cannot interrupt the recovery attempt.
            saved_handlers = {}
            for name in ("SIGTERM", "SIGHUP", "SIGINT"):
                if hasattr(signal, name):
                    sig = getattr(signal, name)
                    saved_handlers[sig] = signal.signal(sig, signal.SIG_IGN)
            try:
                self.phase("recover previous code/configuration and verify previous backend")
                self.rollback()
            except BaseException:
                print("[FAILED] Automatic rollback could not be verified. Keep private backup; manual review required.", flush=True)
                raise DeployError("Deployment and rollback require manual review; no database was restored.") from None
            finally:
                for sig, handler in saved_handlers.items():
                    signal.signal(sig, handler)
            print("[ROLLBACK] Previous code/.env bytes restored; private configuration permissions retained; backend healthy; database was not restored.", flush=True)
            raise DeployError("Server-only deployment failed and was rolled back.") from None
        print("[OK] Server-only overlay active; stable source/installers unchanged. Discord signed PING still requires portal verification.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--run-id", required=True)
    protection = parser.add_mutually_exclusive_group()
    protection.add_argument("--protect-config", action="store_true", help="Explicit permission-only protection; no code/key/service changes")
    protection.add_argument("--protect-storage", action="store_true", help="Owner-only fixed database/Git permission protection; no sudo or database reads")
    args = parser.parse_args()
    if Path.cwd().absolute() != ROOT:
        print("[FAILED] Run only from the explicitly approved server checkout.", flush=True)
        return 2
    previous = {}
    def interrupt(_sig, _frame):
        raise DeployError("Deployment interrupted.")
    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        if hasattr(signal, name):
            sig = getattr(signal, name)
            previous[sig] = signal.signal(sig, interrupt)
    try:
        with deployment_lock(ROOT):
            action = Deployment(ROOT, args.target, args.run_id)
            if args.protect_config:
                action.protect_config()
            elif args.protect_storage:
                action.protect_storage()
            else:
                action.execute()
        return 0
    except BaseException:
        print("[FAILED] Server-only deployment did not complete; see bounded status above. Private output was not printed.", flush=True)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
