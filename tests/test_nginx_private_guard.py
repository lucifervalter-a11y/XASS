from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('nginx_private_guard', Path(__file__).resolve().parents[1] / 'deploy/nginx_private_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

CONFIG = b'''server {
    server_name redvps.site www.redvps.site;
    root /home/red/serverredus;
    index index.php profile.php index.html;
    location = /profile/now-playing/external {
        proxy_pass http://127.0.0.1:8000/profile/now-playing/external;
        proxy_http_version 1.1;
    }
    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }
    location ~ \\.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php-fpm.sock;
    }
    listen 443 ssl;
    include /etc/letsencrypt/options-ssl-nginx.conf;
}
server {
    if ($host = redvps.site) {
        return 301 https://$host$request_uri;
    }
    listen 80;
    return 404;
}
'''


class NginxPrivateGuardTests(unittest.TestCase):
    def test_render_preserves_all_existing_routes_and_is_idempotent(self):
        candidate = guard.render(CONFIG)
        self.assertEqual(candidate.replace(guard.GUARD.encode(), b''), CONFIG)
        self.assertEqual(guard.render(candidate), candidate)
        self.assertLess(candidate.index(guard.BEGIN.encode()), candidate.index(b'location ~ \\.php$'))

    def test_strict_config_drift_is_rejected(self):
        changes = [CONFIG.replace(b'root /home/red/serverredus;', b'root /other;'),
                   CONFIG.replace(b'location / {', b'location ^~ / {'),
                   CONFIG.replace(b'index index.php', b'error_page 404 /index.php;\nindex index.php'),
                   CONFIG.replace(b'index index.php', b'alias /private;\nindex index.php'),
                   CONFIG.replace(b'include snippets/fastcgi-php.conf;', b'include /unknown;'),
                   CONFIG + b'location /other { return 200; }\n', CONFIG[:-2],
                   CONFIG.replace(b'\n', b'\r\n'), CONFIG + guard.BEGIN.encode()]
        for data in changes:
            with self.subTest(data=data[:20]), self.assertRaises(guard.GuardError):
                guard.render(data)

    def test_first_regex_denies_private_paths_without_breaking_public_paths(self):
        patterns = re.findall(r'location ~ "([^"]+)"', guard.GUARD)
        def match(path):
            return next((number for number, pattern in enumerate(patterns) if re.search(pattern, path)), None)
        for path in ('/.env', '/.git/config', '/assets/.env', '/.well-known/.env', '/a/.well-known/x',
                     '/data/serverredus.db', '/data/serverredus.db-wal', '/data/serverredus.db-shm',
                     '/data/avatars/.secret.png', '/data/avatars/evil.php', '/app/main.py', '/deploy/a.php',
                     '/pc_client/a.py', '/scripts/a.sh', '/server.db', '/server.db-wal', '/settings.json'):
            with self.subTest(path=path):
                self.assertIsNotNone(match(path))
                self.assertNotEqual(match(path), 1)
        for path in ('/data/avatars/avatar-20261030-123456-abcd1234.png', '/data/avatars/old-name.jpeg'):
            self.assertEqual(match(path), 1)
        for path in ('/api/music', '/agent/update-manifest', '/agent/installer/x.exe', '/telegram/webhook/example',
                     '/assets/miniapp.js', '/projects/app/download.zip', '/miniapp.php',
                     '/.well-known/acme-challenge/token', '/.well-known/apple-app-site-association'):
            with self.subTest(path=path):
                self.assertIsNone(match(path))

    def test_head_never_reads_a_body_or_follows_redirect(self):
        connection = Mock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.getheader.return_value = 'application/octet-stream'
        with patch.object(guard.http.client, 'HTTPSConnection', return_value=connection):
            self.assertEqual(guard.head('redvps.site', '/.env')['status'], 302)
        self.assertEqual(connection.request.call_args.args[:2], ('HEAD', '/.env'))
        connection.getresponse.return_value.read.assert_not_called()
        connection.close.assert_called_once()

    def test_command_never_prints_nginx_error_contents(self):
        result = Mock(returncode=1, stderr=b'SECRET config contents', stdout=b'PRIVATE')
        with patch.object(guard.subprocess, 'run', return_value=result), self.assertRaises(guard.GuardError) as caught:
            guard.command(['/usr/sbin/nginx', '-t'])
        self.assertNotIn('SECRET', str(caught.exception))

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership/mode verification')
    def test_atomic_replace_preserves_owner_group_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'site'
            path.write_bytes(CONFIG)
            path.chmod(0o640)
            original_info = path.stat()
            candidate = guard.render(CONFIG)
            guard.atomic_write(path, candidate, original_info)
            self.assertEqual(path.read_bytes(), candidate)
            result = path.stat()
            self.assertEqual((result.st_uid, result.st_gid, result.st_mode & 0o777),
                             (original_info.st_uid, original_info.st_gid, 0o640))
            self.assertEqual([item.name for item in path.parent.iterdir()], ['site'])

    def test_after_checks_private_and_public_status_and_type(self):
        before = [{'host': 'redvps.site', 'path': '/', 'status': 200, 'type': 'text/html'},
                  {'host': 'redvps.site', 'path': '/health', 'status': 405, 'type': 'application/json'},
                  {'host': 'redvps.site', 'path': '/.env', 'status': 200, 'type': 'application/octet-stream'}]
        after = [dict(row) for row in before]
        after[2]['status'] = 404
        guard.verify_after(before, after)
        for row, key, value in ((2, 'status', 200), (0, 'status', 500), (1, 'type', 'text/html')):
            changed = [dict(item) for item in after]
            changed[row][key] = value
            with self.assertRaises(guard.GuardError):
                guard.verify_after(before, changed)

    def simulate(self, failure=None):
        target = Mock()
        enabled = Mock()
        enabled.is_symlink.return_value = True
        enabled.resolve.return_value = target
        info = Mock(st_mode=0o100644, st_nlink=1, st_uid=0, st_gid=0, st_ino=3)
        target.stat.return_value = info
        state = {'body': CONFIG}
        reads = 0
        def read():
            nonlocal reads
            reads += 1
            if failure == 'drift' and reads == 2:
                state['body'] = CONFIG + b'# concurrent administrator change\n'
            return state['body']
        target.read_bytes.side_effect = read
        calls = []
        def command(args):
            calls.append(args)
            if failure == 'syntax' and len(calls) == 2:
                raise guard.GuardError('syntax failed')
            if failure == 'reload' and len(calls) == 3:
                raise guard.GuardError('reload failed')
        def write(path, body, original_info):
            self.assertIs(path, target)
            self.assertIs(original_info, info)
            state['body'] = body
        backup = Mock()
        backup.stat.return_value = Mock(st_uid=0, st_mode=0o40700)
        backup.__truediv__ = Mock(return_value=backup)
        with tempfile.TemporaryDirectory() as tmp:
            # Use a real private backup on either platform; metadata is checked independently.
            actual_backup = Path(tmp) / 'backups'
            actual_backup.mkdir(mode=0o700)
            actual_info = actual_backup.stat()
            fake_stat = Mock(st_uid=0, st_mode=0o40700)
            original_stat = Path.stat
            def stat_override(path, *args, **kwargs):
                return fake_stat if path == actual_backup else original_stat(path, *args, **kwargs)
            with patch.object(guard, 'TARGET', target), patch.object(guard, 'ENABLED', enabled), \
                    patch.object(guard, 'BACKUPS', actual_backup), patch.object(guard, 'no_links'), \
                    patch.object(guard, 'command', side_effect=command), \
                    patch.object(guard, 'atomic_write', side_effect=write), \
                    patch.object(guard, 'probes', return_value=[]), \
                    patch.object(guard, 'verify_after', side_effect=guard.GuardError('probe failed') if failure == 'probe' else None), \
                    patch.object(Path, 'stat', stat_override), \
                    patch.object(guard.os, 'O_NOFOLLOW', getattr(guard.os, 'O_NOFOLLOW', 0), create=True), \
                    redirect_stdout(io.StringIO()):
                if failure:
                    with self.assertRaises(guard.GuardError):
                        guard.apply_guard('unit-test')
                else:
                    self.assertEqual(guard.apply_guard('unit-test')['status'], 'guarded')
                self.assertEqual((actual_backup / 'unit-test/config').read_bytes(), CONFIG)
        return state, calls

    def test_success_tests_before_after_then_only_reloads_nginx(self):
        state, calls = self.simulate()
        self.assertEqual(state['body'], guard.render(CONFIG))
        self.assertEqual(calls, [['/usr/sbin/nginx', '-t'], ['/usr/sbin/nginx', '-t'], ['/usr/bin/systemctl', 'reload', 'nginx']])

    def test_invalid_candidate_restores_original_without_reload(self):
        state, calls = self.simulate('syntax')
        self.assertEqual(state['body'], CONFIG)
        self.assertEqual(len(calls), 2)

    def test_concurrent_config_change_is_not_overwritten_or_reloaded(self):
        state, calls = self.simulate('drift')
        self.assertEqual(state['body'], CONFIG + b'# concurrent administrator change\n')
        self.assertEqual(calls, [['/usr/sbin/nginx', '-t']])

    def test_reload_or_postcheck_failure_never_reopens_private_files(self):
        for failure in ('reload', 'probe'):
            with self.subTest(failure=failure):
                state, calls = self.simulate(failure)
                self.assertEqual(state['body'], guard.render(CONFIG))

    def test_invalid_identifiers_rejected_before_any_filesystem(self):
        for value in ('../escape', '', '/root', 'a/b', 'a b', 'a' * 81):
            with self.subTest(value=value), patch.object(guard, 'no_links') as paths, self.assertRaises(guard.GuardError):
                guard.apply_guard(value)
            paths.assert_not_called()


if __name__ == '__main__':
    unittest.main()
