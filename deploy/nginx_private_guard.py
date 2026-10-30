"""Emergency, fixed-vhost containment. Never reads application secrets or HTTP bodies."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

TARGET = Path('/etc/nginx/sites-available/redvps.site')
ENABLED = Path('/etc/nginx/sites-enabled/redvps.site')
BACKUPS = Path('/var/backups/xass-nginx')
LOCK = Path('/home/red/.xass-deployment.lock')
BEGIN = '    # BEGIN XASS PRIVATE FILE GUARD v1\n'
END = '    # END XASS PRIVATE FILE GUARD v1\n'
GUARD = BEGIN + r'''    # Deny dotfiles at every depth; only root /.well-known is exempt.
    location ~ "^/(?:\.(?!well-known(?:/|$))|.+/\.)" { return 404; }
    # Profile avatars are intentionally public. No directories or script execution.
    location ~ "^/data/avatars/[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:png|jpg|jpeg|webp)$" {
        try_files $uri =404;
    }
    location ~ "^/(?:data|app|pc_client|deploy|tests|ios|windows|tools|scripts|docs|artifacts|patches|restored|migration-system|__pycache__)(?:/|$)" {
        return 404;
    }
    # Root-level database/config/source backups must not fall through to PHP.
    location ~ "^/[^/]+\.(?:db|sqlite|sqlite3)(?:-(?:wal|shm|journal))?$" { return 404; }
    location ~ "^/(?:requirements[^/]*\.txt|pyproject\.toml|(?:config|settings)\.(?:json|ini|toml|ya?ml)|[^/]+\.(?:py|pyc|sh|ps1|log|sql|env|bak))$" { return 404; }
''' + END
PUBLIC = ('/', '/miniapp.php', '/assets/miniapp-device-key.js',
          '/manifest.webmanifest', '/sw.js', '/health', '/agent/update-manifest')
PRIVATE = ('/.env', '/%2eenv', '/.git/config', '/%2egit/config',
           '/data/serverredus.db', '/data/serverredus.db-wal', '/data/serverredus.db-shm',
           '/data/avatars/.env', '/assets/.env', '/.well-known/.env',
           '/app/main.py', '/pc_client/client.py', '/deploy/update.sh', '/tests/')


class GuardError(RuntimeError):
    pass


def render(original: bytes) -> bytes:
    if not original or len(original) > 65536 or b'\0' in original:
        raise GuardError('Unexpected vhost file size or encoding.')
    try:
        text = original.decode('utf-8')
    except UnicodeError as exc:
        raise GuardError('Vhost is not UTF-8.') from exc
    if '\r' in text:
        raise GuardError('Unexpected vhost line endings.')
    if BEGIN in text or END in text:
        if text.count(GUARD) != 1 or text.count(BEGIN) != 1 or text.count(END) != 1:
            raise GuardError('An unknown or partial private guard already exists.')
        base = text.replace(GUARD, '', 1)
    else:
        base = text
    lines = [line.split('#', 1)[0].strip() for line in base.splitlines()]
    active = '\n'.join(line for line in lines if line)
    # This tool is deliberately not a general nginx parser. Any shape drift aborts.
    if any(re.search(r'\b' + name + r'\b', active) for name in ('alias', 'error_page')):
        raise GuardError('Unexpected alias/error_page requires manual review.')
    if len(re.findall(r'(?m)^server\s*\{\s*$', active)) != 2:
        raise GuardError('Unexpected number of server blocks.')
    if re.findall(r'(?m)^root\s+([^;]+);$', active) != ['/home/red/serverredus']:
        raise GuardError('Unexpected document root.')
    headers = [re.sub(r'\s+', ' ', line) for line in lines if line.startswith('location ')]
    if headers != ['location = /profile/now-playing/external {', 'location / {', r'location ~ \.php$ {']:
        raise GuardError('Vhost locations changed; inspect before applying.')
    if active.count('try_files $uri $uri/ /index.php?$query_string;') != 1:
        raise GuardError('Unexpected public fallback.')
    includes = re.findall(r'(?m)^include\s+([^;]+);$', active)
    if includes != ['snippets/fastcgi-php.conf', '/etc/letsencrypt/options-ssl-nginx.conf']:
        raise GuardError('Unexpected nginx include requires review.')
    # Count braces in this known simple config; reject unmatched/extra scopes.
    depth = 0
    root_depth = None
    for line in lines:
        if line.startswith('root '):
            root_depth = depth
        depth += line.count('{') - line.count('}')
        if depth < 0:
            raise GuardError('Unbalanced vhost scopes.')
    if depth != 0 or root_depth != 1:
        raise GuardError('Unexpected vhost scope.')
    anchor = re.search(r'(?m)^[ \t]*location / \{\s*$', base)
    if not anchor:
        raise GuardError('Public location anchor missing.')
    candidate = base[:anchor.start()] + GUARD + base[anchor.start():]
    if BEGIN in text and candidate != text:
        raise GuardError('Existing guard is in an unexpected location.')
    return candidate.encode('utf-8')


def no_links(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise GuardError('A protected path is a symlink.')


@contextmanager
def deployment_lock():
    import fcntl
    no_links(LOCK)
    descriptor = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in (0, 1000) or stat.S_IMODE(info.st_mode) != 0o600):
            raise GuardError('Unsafe deployment lock metadata.')
        # Keep the shared lock usable by the normal UID 1000 deployment process.
        if info.st_uid == 0:
            os.fchown(descriptor, 1000, LOCK.parent.stat().st_gid)
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GuardError('Another deployment holds the production lock.')
                time.sleep(.2)
        yield
    finally:
        os.close(descriptor)


def command(argv: list[str]) -> None:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError('nginx validation/reload could not finish; inspect locally.') from exc
    if result.returncode:
        # nginx errors can contain config lines: do not copy them into CI logs.
        raise GuardError('nginx validation/reload failed; inspect locally.')


def atomic_write(path: Path, body: bytes, info: os.stat_result) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.xass-nginx-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(body)
            output.flush()
            os.fchown(output.fileno(), info.st_uid, info.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def head(host: str, path: str) -> dict:
    connection = http.client.HTTPSConnection(host, timeout=4)
    try:
        connection.request('HEAD', path, headers={'Connection': 'close', 'Cache-Control': 'no-cache'})
        response = connection.getresponse()
        return {'host': host, 'path': path, 'status': response.status,
                'type': (response.getheader('Content-Type') or '')[:128]}
    except (OSError, http.client.HTTPException):
        return {'host': host, 'path': path, 'status': 0, 'type': ''}
    finally:
        connection.close()  # No response.read(): private files are never downloaded.


def probes() -> list[dict]:
    targets = [('redvps.site', path) for path in PUBLIC + PRIVATE]
    targets += [('www.redvps.site', path) for path in ('/.env', '/.git/config', '/data/serverredus.db')]
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(lambda target: head(*target), targets))


def verify_after(before: list[dict], after: list[dict]) -> None:
    indexed = {(row['host'], row['path']): row for row in after}
    for previous in before:
        key = previous['host'], previous['path']
        current = indexed.get(key, {})
        if previous['host'] == 'redvps.site' and previous['path'] in PUBLIC:
            if (not previous['status'] or current.get('status') != previous['status']
                    or current.get('type') != previous['type']):
                raise GuardError('Public route check failed; hardened config retained for manual review.')
        elif current.get('status') not in (403, 404):
            raise GuardError('Private route check failed; hardened config retained for manual review.')


def apply_guard(run_id: str) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', run_id):
        raise GuardError('Invalid run identifier.')
    no_links(TARGET)
    if not ENABLED.is_symlink() or ENABLED.resolve(strict=True) != TARGET:
        raise GuardError('Enabled vhost is not the inspected symlink.')
    info = TARGET.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o022:
        raise GuardError('Unsafe vhost metadata.')
    original = TARGET.read_bytes()
    candidate = render(original)
    command(['/usr/sbin/nginx', '-t'])
    before = probes()
    print(json.dumps({'phase': 'before', 'head_only': before}), flush=True)
    if candidate == original:
        verify_after(before, before)
        return {'status': 'already_guarded', 'head_only': before}
    no_links(BACKUPS)
    BACKUPS.mkdir(mode=0o700, parents=False, exist_ok=True)
    backup_info = BACKUPS.stat()
    if backup_info.st_uid != 0 or stat.S_IMODE(backup_info.st_mode) != 0o700:
        raise GuardError('Backup directory must be root-owned 0700.')
    destination = BACKUPS / run_id
    destination.mkdir(mode=0o700)
    backup = destination / 'config'
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(original)
        output.flush()
        os.fsync(output.fileno())
    if TARGET.stat().st_ino != info.st_ino or TARGET.read_bytes() != original:
        raise GuardError('Vhost changed after inspection; no config replaced.')
    atomic_write(TARGET, candidate, info)
    try:
        command(['/usr/sbin/nginx', '-t'])
    except GuardError:
        if TARGET.read_bytes() != candidate:
            raise GuardError('Vhost changed during validation; manual recovery required.')
        atomic_write(TARGET, original, info)
        raise GuardError('Candidate invalid: original file restored; nginx was NOT reloaded.')
    # Never restore an exposed original after reload: live state may be uncertain.
    command(['/usr/bin/systemctl', 'reload', 'nginx'])
    after = probes()
    print(json.dumps({'phase': 'after', 'head_only': after}), flush=True)
    verify_after(before, after)
    return {'status': 'guarded', 'backup': str(backup), 'head_only': after,
            'config_sha256': hashlib.sha256(candidate).hexdigest(), 'backend_restarted': False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    try:
        if os.name != 'posix' or os.geteuid() != 0:
            raise GuardError('This fixed-target operation requires root on the VPS.')
        with deployment_lock():
            result = apply_guard(args.run_id)
        print(json.dumps(result), flush=True)
        return 0
    except GuardError as exc:
        print(json.dumps({'status': 'ERROR', 'message': str(exc)}), flush=True)
        return 1
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', 'message': 'Operation interrupted; inspect locally.',
                          'type': type(exc).__name__}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
