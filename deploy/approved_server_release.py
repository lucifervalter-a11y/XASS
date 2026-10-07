#!/usr/bin/env python3
"""One approved code-only deployment; never changes dependencies, config or DB.

Stream this helper over the existing pinned SSH transport. The server receives
only the approved release commit, not this ops tooling.
All subprocess stderr and response bodies stay private; receipts contain checks.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "822ac05d4a53dd5475312834065501b9af063b1a"
RELEASE = "45cbf71480de12f260e689e7be63e057eacd25b9"
BRANCH = "ops/server-runtime-20261007"
ROOT = Path("/home/red/serverredus")
SERVICE = "serverredus-backend"
FILES = {
    "app/bot_api.py": "539443c8a451ecc274dece195ee822c85b6fa2f35fdaa376cea832032e803d6b",
    "app/music_storage_api.py": "76037db0a0e7c1f45f3cfce03712d7754b4273ad15550cfcc8157a860a616358",
    "app/poller.py": "49557d8bd3d408f40abef06c206871f2061767d70dd8439a0888e7df51d2592a",
    "app/services/notifications.py": "84bac5d6dcc7083ea661a65769a170f85c7ec71430fe439b34a231274906b47f",
    "docs/MUSIC_PLAYER.md": "1f76e41169a69f63817dfe66d02ea4c212f517fc8e24354b0025cd86a0581230",
    "pc_client/archive_store.py": "7f15018aa74fbf9f287b266547884189519c3adf1b979d7e77f5a6964a803fa0",
    "tests/test_music_restore_recovery.py": "39cde2047188ec65cb8f61dc958a044d64dd05d00ae28706d4ea15b5d95fd57b",
    "tests/test_runtime_regressions.py": "977736bfda0885ff6556835ec3d2400630f443e9fa4015d6b2709015cda48a78",
}


class Stop(RuntimeError):
    """Only fixed, non-sensitive messages may be raised with this type."""


def require(condition, message):
    if not condition:
        raise Stop(message)


def progress(stage):
    print(json.dumps({"stage": stage}), flush=True)


def run(args, root=ROOT, timeout=60, env=None):
    result = subprocess.run(args, cwd=root, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, env=env)
    require(result.returncode == 0, "Subprocess failed; output withheld to protect configuration.")
    return result.stdout


def git(root, *args):
    return run(["git", "-C", str(root), *args], root).decode("utf-8").strip()


def clean(root):
    require(not git(root, "status", "--porcelain", "--untracked-files=no"),
            "Tracked checkout changes require review; stopping.")


def validate_release(root, base, release, files):
    require(git(root, "rev-list", "--parents", "-n", "1", release).split() == [release, base],
            "Release does not have the approved single parent.")
    changed = set(git(root, "diff", "--name-only", base, release).splitlines())
    require(changed == set(files), "Release changes paths outside the approved manifest.")
    for name, expected in files.items():
        raw = run(["git", "show", f"{release}:{name}"], root)
        require(hashlib.sha256(raw).hexdigest() == expected, "Release content differs from the reviewed package.")
    added = git(root, "diff", "--name-only", "--diff-filter=A", base, release).splitlines()
    for name in added:
        require(not os.path.lexists(root / name), "New release file collides with existing untracked data.")


def rollback_code(root, base, release):
    clean(root)
    current = git(root, "rev-parse", "HEAD")
    require(current in {base, release}, "Rollback refused: checkout moved to another revision.")
    if current == release:
        # --keep preserves unrelated untracked runtime files and rejects conflicts.
        git(root, "reset", "--keep", base)
    require(git(root, "rev-parse", "HEAD") == base, "Rollback revision did not match.")
    clean(root)


def service_state(systemctl):
    fields = run([systemctl, "show", SERVICE, "--property=MainPID",
                  "--property=ActiveState", "--property=InvocationID",
                  "--property=WorkingDirectory", "--property=User"]).decode().splitlines()
    data = dict(line.split("=", 1) for line in fields if "=" in line)
    require(data.get("ActiveState") == "active", "Backend service is not active.")
    require(data.get("WorkingDirectory") == str(ROOT), "Backend working directory differs.")
    require(data.get("MainPID", "").isdigit() and int(data["MainPID"]) > 0, "Backend PID is unavailable.")
    require(Path(f"/proc/{data['MainPID']}/cwd").resolve(strict=True) == ROOT, "Running backend uses another checkout.")
    require(re.fullmatch(r"[0-9a-f]{32}", data.get("InvocationID", "")), "Service invocation cannot be verified.")
    import pwd
    require(data.get("User") == pwd.getpwuid(os.geteuid()).pw_name, "Deployment and backend users differ.")
    return data


def journal_check(invocation, *, reject_errors):
    output = run(["journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}", "--no-pager", "-o", "json", "-n", "500"])
    entries = [json.loads(line) for line in output.splitlines() if line.strip()]
    require(bool(entries), "Cannot verify backend journal with current access; no changes to access are allowed.")
    errors = sum(int(entry.get("PRIORITY", 6)) <= 3 or bool(re.search(
        r"Traceback \(most recent call last\)|\bERROR\b|\bCRITICAL\b", str(entry.get("MESSAGE", "")))) for entry in entries)
    if reject_errors:
        require(errors == 0, "New backend invocation has error entries; rolling back.")
    return {"entries_checked": len(entries), "error_entries": errors}


def api_checks(port, *, public=True):
    checks = {}
    origins = [("local", f"http://127.0.0.1:{port}")]
    if public:
        origins.append(("public", "https://redvps.site"))
    for label, origin in origins:
        for path, key in [("/health", "health"), ("/api/mini/ping", "ping"),
                          ("/api/mini/music/storage", "music_auth"), ("/api/mini/weather", "weather_auth")]:
            # The PHP frontend routes /api through this envelope, as miniapp.php
            # does. Direct public /api URLs intentionally serve the profile page.
            envelope_mode = label == "public" and path.startswith("/api/")
            url = origin + ("/proxy.php?" + urllib.parse.urlencode({"_p": path}) if envelope_mode else path)
            request = urllib.request.Request(url, headers={"User-Agent": "XASS-approved-release-check"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    status, body = response.status, response.read(65537)
                    require(response.url == url, "API request unexpectedly redirected.")
            except urllib.error.HTTPError as exc:
                status, body = exc.code, b""
            except urllib.error.URLError as exc:
                reason = type(exc.reason).__name__
                verification = getattr(exc.reason, "verify_code", None)
                raise Stop(f"{label}_{key}: transport failed ({reason}, TLS verify code {verification}).") from None
            if envelope_mode:
                require(status == 200 and len(body) <= 65536, "Public API proxy transport failed.")
                try:
                    envelope = json.loads(body)
                except (ValueError, UnicodeError):
                    raise Stop("Public API proxy returned invalid JSON.") from None
                require(isinstance(envelope, dict) and type(envelope.get("_s")) is int
                        and isinstance(envelope.get("_b"), str), "Public API proxy envelope is invalid.")
                status, body = envelope["_s"], envelope["_b"].encode("utf-8")
            if key.endswith("_auth"):
                require(status in {401, 403}, "Protected API is not rejecting unauthenticated requests.")
            else:
                require(status == 200 and len(body) <= 65536, "Public health API failed.")
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError):
                    raise Stop(f"{label}_{key}: response is not valid JSON.") from None
                require(data.get("status") == "ok" if key == "health" else data.get("ok") is True,
                        "Health API returned an unexpected contract.")
            checks[f"{label}_{key}"] = status
            progress(f"api_{label}_{key}_status_{status}")
    return checks


def wait_healthy(port):
    for _ in range(30):
        try:
            api_checks(port, public=False)
            return
        except Exception:
            time.sleep(1)
    raise Stop("Backend health checks did not pass after restart.")


NETWORK_ENVIRONMENT = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
})


def network_environment(raw):
    """Select only the running service's network settings; never export tokens."""
    selected = {}
    for entry in raw.split(b"\0"):
        name, separator, value = entry.partition(b"=")
        if separator and name.decode("ascii", errors="ignore") in NETWORK_ENVIRONMENT:
            selected[name.decode("ascii")] = os.fsdecode(value)
    return selected


def telegram_check(pid):
    # Separate process avoids importing pre-deploy modules again after checkout changes.
    # HTTPX reads proxies/CA paths from the process environment, not Settings' .env.
    # Use exactly the already-running service's values, without printing them or
    # changing systemd, proxy services, VPNs, trust stores or configuration files.
    selected = network_environment(Path(f"/proc/{pid}/environ").read_bytes())
    environment = {key: value for key, value in os.environ.items() if key not in NETWORK_ENVIRONMENT}
    environment.update(selected)
    print(json.dumps({"stage": "telegram_using_existing_service_network_environment",
                      "setting_names": sorted(selected)}), flush=True)
    worker = '''
import asyncio, json
from app.config import Settings
from app.bot_api import TelegramBotClient
async def check():
    token = Settings().bot_token
    assert token
    client = TelegramBotClient(token)
    try:
        identity = await asyncio.wait_for(client.get_me(), timeout=25)
        assert identity.get("is_bot") is True and isinstance(identity.get("id"), int)
        print(json.dumps({"ok": True}))
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        print(json.dumps({"ok": False, "failure_type": type(exc).__name__,
                          "http_status": status if type(status) is int else None}))
    finally:
        await client.close()
asyncio.run(check())
'''
    try:
        result = json.loads(run([sys.executable, "-B", "-c", worker], timeout=35, env=environment))
    except (Stop, subprocess.TimeoutExpired):
        raise Stop("Telegram getMe failed or exceeded its deadline with the existing backend network settings.") from None
    if result.get("ok") is not True:
        kind = result.get("failure_type", "UnknownError")
        require(isinstance(kind, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", kind), "Invalid Telegram diagnostic result.")
        status = result.get("http_status")
        require(status is None or type(status) is int, "Invalid Telegram diagnostic status.")
        raise Stop(f"Telegram getMe failed using existing backend network settings: {kind}, HTTP status {status}.")
    return {"get_me": True, "messages_sent": 0, "updates_consumed": 0}


def verify_backup(destination, root, base):
    info = json.loads((destination / "COMPLETE.json").read_text())
    require(info.get("revision") == base, "Backup revision differs from the approved baseline.")
    require((destination / ".env").read_bytes() == (root / ".env").read_bytes(), "Configuration backup differs.")
    expected = {}
    raw = run(["git", "ls-tree", "-r", "-z", base], root)
    for record in raw.split(b"\0"):
        if record:
            metadata, name = record.split(b"\t", 1)
            mode, kind, sha = metadata.split()
            require(kind == b"blob", "Backup verification does not support submodules.")
            expected[name.decode()] = sha.decode()
    seen = set()
    with tarfile.open(destination / "code.tar.gz", "r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            require(member.name in expected and member.name not in seen, "Code backup has unexpected entries.")
            if member.issym():
                content = member.linkname.encode()
            else:
                require(member.isfile(), "Code backup has unsupported entries.")
                content = archive.extractfile(member).read()
            blob = b"blob " + str(len(content)).encode() + b"\0" + content
            require(hashlib.sha1(blob).hexdigest() == expected[member.name], "Code backup failed Git blob verification.")
            seen.add(member.name)
    require(seen == set(expected), "Code backup is incomplete.")
    if info["database"] == "sqlite":
        with closing(sqlite3.connect((destination / "database/sqlite.db").as_uri() + "?mode=ro", uri=True)) as connection:
            require(connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "Database backup integrity check failed.")
    elif info["database"] == "postgresql":
        run(["pg_restore", "--list", str(destination / "database/postgres.dump")], root)
    else:
        raise Stop("Unsupported backup database format.")
    for path in [destination, *destination.rglob("*")]:
        require(not path.is_symlink(), "Backup contains symlinks.")
        if os.name == "posix":
            require(path.stat().st_mode & 0o077 == 0, "Backup permissions are not private.")
    return {"tracked_blobs_verified": len(seen), "database_integrity": True, "configuration_copy_verified": True}


def rollback_script(base, release, systemctl, port):
    return f'''#!/usr/bin/env bash
set -Eeuo pipefail
cd /home/red/serverredus
exec 9>/home/red/.xass-deployment.lock
flock -w 120 9
test -z "$(git status --porcelain --untracked-files=no)"
revision="$(git rev-parse HEAD)"
[[ "$revision" == '{release}' || "$revision" == '{base}' ]]
if [[ "$revision" == '{release}' ]]; then git reset --keep '{base}'; fi
test "$(git rev-parse HEAD)" = '{base}'
sudo -n '{systemctl}' restart serverredus-backend
for attempt in {{1..30}}; do
  if curl --fail --silent --max-time 2 'http://127.0.0.1:{port}/health' >/dev/null; then
    echo 'Original code restored; backend health passed. Database and .env were not restored.'
    exit 0
  fi
  sleep 1
done
echo 'Original code restored but backend health failed.' >&2
exit 1
'''


def deploy(support, run_id):
    import fcntl
    require(re.fullmatch(r"[0-9a-f]{40}", support), "Invalid workflow revision.")
    require(re.fullmatch(r"[0-9]+-[0-9]+", run_id), "Invalid workflow run ID.")
    require(Path.cwd() == ROOT and ROOT.resolve() == ROOT, "Unexpected deployment root.")
    require(sys.version_info >= (3, 11), "Backend Python version is incompatible.")
    sys.path.insert(0, str(ROOT))
    from dotenv import dotenv_values
    from deploy.predeploy_backup import create_backup
    port_text = dotenv_values(ROOT / ".env").get("PORT") or "8000"
    require(str(port_text).isdigit() and 1 <= int(port_text) <= 65535, "Invalid backend port.")
    port = int(port_text)
    systemctl = shutil.which("systemctl")
    require(bool(systemctl), "systemctl is unavailable.")
    lock_path = ROOT.parent / ".xass-deployment.lock"
    require(not lock_path.is_symlink(), "Deployment lock is a symlink.")
    with lock_path.open("a") as lock:
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, "Another deployment holds the lock.")
                time.sleep(1)
        clean(ROOT)
        require(git(ROOT, "rev-parse", "HEAD") == BASE, "Live checkout is not the approved baseline; no deployment.")
        progress("baseline_matches_fetching_approved_branch")
        git(ROOT, "fetch", "--no-tags", "origin", f"refs/heads/{BRANCH}")
        require(git(ROOT, "rev-parse", "FETCH_HEAD") == support, "Remote workflow branch moved.")
        git(ROOT, "merge-base", "--is-ancestor", RELEASE, support)
        support_paths = set(git(ROOT, "diff", "--name-only", RELEASE, support).splitlines())
        require(support_paths == {".github/workflows/approved-server-runtime.yml",
                "deploy/approved_server_release.py", "deploy/check_approved_server_release.py",
                "tests/test_approved_server_release.py", "deploy/telegram_network_diagnostics.py",
                "tests/test_telegram_network_diagnostics.py"}, "Workflow changes extend beyond approved deployment tooling.")
        validate_release(ROOT, BASE, RELEASE, FILES)
        progress("release_verified_checking_dependencies")
        run([sys.executable, "-m", "pip", "check"])
        progress("dependencies_verified_checking_existing_restart_permission")
        run(["sudo", "-n", "-l", systemctl, "restart", SERVICE])
        progress("restart_permission_verified_checking_service")
        before = service_state(systemctl)
        progress("service_verified_checking_api")
        baseline_api = api_checks(port)
        progress("api_verified_checking_journal")
        baseline_journal = journal_check(before["InvocationID"], reject_errors=False)
        print(json.dumps({"stage": "baseline_journal_checked", **baseline_journal}), flush=True)
        progress("journal_verified_checking_telegram_get_me")
        baseline_telegram = telegram_check(before["MainPID"])
        receipt = {"baseline_matches": True, "release": RELEASE, "workflow": support,
                   "baseline_api": baseline_api, "baseline_telegram": baseline_telegram,
                   "baseline_journal": baseline_journal}
        print(json.dumps({"stage": "preflight_passed", **receipt}), flush=True)
        destination = create_backup(ROOT, f"server-{run_id}")
        receipt["backup"] = str(destination)
        receipt["backup_checks"] = verify_backup(destination, ROOT, BASE)
        rollback = destination / "rollback-code.sh"
        rollback.write_text(rollback_script(BASE, RELEASE, systemctl, port))
        rollback.chmod(0o700)
        run(["bash", "-n", str(rollback)])
        receipt["rollback_command"] = f"bash {rollback}"
        print(json.dumps({"stage": "backup_verified", **receipt}), flush=True)
        # The backend uses the same deployment user; preserve standard tracked-file modes.
        attempted = False
        try:
            clean(ROOT)
            require(git(ROOT, "rev-parse", "HEAD") == BASE, "Checkout changed during preflight.")
            attempted = True
            progress("installing_verified_release")
            git(ROOT, "merge", "--ff-only", RELEASE)
            require(git(ROOT, "rev-parse", "HEAD") == RELEASE, "Installed revision differs.")
            run(["sudo", "-n", systemctl, "restart", SERVICE])
            progress("backend_restarted_checking_live_results")
            wait_healthy(port)
            after = service_state(systemctl)
            require(after["InvocationID"] != before["InvocationID"], "Backend did not start a new invocation.")
            receipt["api"] = api_checks(port)
            receipt["telegram"] = telegram_check(after["MainPID"])
            time.sleep(10)
            receipt["journal"] = journal_check(after["InvocationID"], reject_errors=True)
            require(service_state(systemctl)["InvocationID"] == after["InvocationID"], "Backend restarted during verification.")
            clean(ROOT)
            receipt.update(status="deployed", revision_matches=True, backend_restarted=True)
        except BaseException:
            if attempted:
                progress("deployment_failed_rolling_back_code")
                # Ignore further SSH cancellation signals while the bounded rollback runs.
                for sig in (signal.SIGTERM, signal.SIGHUP):
                    signal.signal(sig, signal.SIG_IGN)
                rollback_code(ROOT, BASE, RELEASE)
                run(["sudo", "-n", systemctl, "restart", SERVICE])
                wait_healthy(port)
                receipt.update(status="rolled_back", rollback_health=True)
                (destination / "deployment-receipt.json").write_text(json.dumps(receipt, indent=2))
                print(json.dumps(receipt), flush=True)
            raise
        (destination / "deployment-receipt.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt), flush=True)


def main():
    def interrupted(_signal, _frame):
        raise Stop("Deployment interrupted.")
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        require(len(sys.argv) == 3, "Expected workflow SHA and run ID.")
        deploy(sys.argv[1], sys.argv[2])
    except BaseException as exc:
        message = str(exc) if isinstance(exc, Stop) else "Deployment failed; sensitive exception details withheld."
        print(json.dumps({"status": "failed", "reason": message, "error_type": type(exc).__name__}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
