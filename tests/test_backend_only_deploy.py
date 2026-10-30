from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backend_only_deploy", REPO / "deploy/backend_only_deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class EnvironmentTests(unittest.TestCase):
    def test_only_discord_keys_replaced_all_other_values_preserved_and_old_default_pinned(self):
        original = (b"# private\r\nBOT_TOKEN=private-sentinel\r\nDATABASE_URL=sqlite:///live.db\r\n"
                    b"discord_public_key=old\r\nexport DISCORD_PUBLIC_KEY=duplicate\r\n"
                    b"DISCORD_LISTEN_URL=https://old.invalid\r\nUNKNOWN=value without newline")
        actual = deploy.configured_env(original, "http://127.0.0.1:8787").decode()
        self.assertIn("BOT_TOKEN=private-sentinel\r\nDATABASE_URL=sqlite:///live.db\r\n", actual)
        self.assertIn("UNKNOWN=value without newline\n", actual)
        self.assertEqual(actual.count("DISCORD_PUBLIC_KEY="), 1)
        self.assertIn(f"DISCORD_PUBLIC_KEY={deploy.PUBLIC_KEY}\n", actual)
        self.assertIn('HERMES_URL="http://127.0.0.1:8787"\n', actual)
        self.assertNotIn("duplicate", actual)

    def test_existing_hermes_alias_or_explicit_empty_setting_is_not_overwritten(self):
        for line in ("HERMES_URL=http://custom.invalid:8787", "HERMES_WEBUI_URL=http://old.invalid",
                     "export hermes_url='http://custom.invalid'", "HERMES_URL="):
            with self.subTest(line=line):
                actual = deploy.configured_env((line + "\nOTHER=keep\n").encode(), "http://127.0.0.1:8787").decode()
                self.assertTrue(actual.startswith(line + "\nOTHER=keep\n"))
                self.assertNotIn('HERMES_URL="http://127.0.0.1:8787"', actual)

    def test_unsafe_hermes_value_is_rejected_before_mutation(self):
        for value in ("http://a\nBOT_TOKEN=changed", "x" * 2049, None):
            with self.subTest(value=type(value)), self.assertRaises(deploy.DeployError):
                deploy.configured_env(b"BOT_TOKEN=keep\n", value)


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "server"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.write("app/a.py", "old app\n")
        self.write("index.php", "<?php echo 'old';\n")
        self.write("proxy.php", "<?php echo 'old proxy';\n")
        self.write("pc_client/agent.py", "stable client\n")
        self.write("agent/agent.py", "stable heartbeat\n")
        self.write("requirements.txt", "unchanged\n")
        self.write("bootstrap_server_dependencies.py", "unchanged\n")
        self.write("deploy/predeploy_backup.py", "reviewed backup\n")
        self.write("deploy/repair_public_permissions.py", "reviewed permissions\n")
        self.before = self.commit("baseline")
        self.write("app/a.py", "verified app\n")
        self.write("index.php", "<?php echo 'verified';\n")
        self.write("proxy.php", "<?php echo 'verified proxy';\n")
        self.write("pc_client/agent.py", "unreleased client\n")
        self.source = self.commit("source")
        self.git("checkout", "-q", "--detach", self.before)
        self.write("app/a.py", "verified app\n")
        self.write("index.php", "<?php echo 'verified';\n")
        self.write("proxy.php", "<?php echo 'verified proxy';\n")
        self.write("deploy/backend_only_deploy.py", "reviewed driver\n")
        self.target = self.commit("server overlay")
        self.git("checkout", "-q", "--detach", self.before)
        self.original = b"BOT_TOKEN=private-sentinel\nDATABASE_URL=sqlite:///live.db\n"
        self.write(".env", self.original)
        self.write("data/live.db", b"live-before")
        self.write("data/music/song.wav", b"original-music")
        self.write("data/releases/XASS-Setup.exe", b"stable-installer")
        self.write("data/releases/XASS-Setup.json", b"stable-installer-metadata")
        for name, value in (("BEFORE", self.before), ("SOURCE", self.source)):
            patched = patch.object(deploy, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.item = deploy.Deployment(self.root, self.target, "fixture-run-1")

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode() if isinstance(value, str) else value)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True).stdout

    def commit(self, message):
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD").decode().strip()

    def changed_target(self, path, value):
        # Keep runtime sentinels private and untracked in this disposable fixture.
        self.git("checkout", "-q", "--detach", self.target)
        self.write(path, value)
        self.git("add", "--", path)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "changed target")
        self.item.target = self.git("rev-parse", "HEAD").decode().strip()
        self.git("checkout", "-q", "--detach", self.before)


class ScopeTests(GitFixture):
    def test_exact_backend_source_and_preserved_source_clients_are_accepted(self):
        self.item.validate()
        self.assertEqual(self.item.head(), self.before)
        self.assertEqual((self.root / ".env").read_bytes(), self.original)

    def test_protected_client_dependency_release_and_workflow_paths_rejected(self):
        for path in ("pc_client/agent.py", "requirements.txt", "data/releases/other.exe",
                     "agent/new.py", ".github/workflows/windows-agent.yml"):
            with self.subTest(path=path):
                self.changed_target(path, "not approved\n")
                with self.assertRaises(deploy.DeployError):
                    self.item.validate()

    def test_unverified_backend_bytes_rejected(self):
        self.changed_target("app/a.py", "unverified backend\n")
        with self.assertRaises(deploy.DeployError):
            self.item.validate()

    def test_dirty_or_newer_live_checkout_rejected(self):
        self.write("app/a.py", "user edits\n")
        with self.assertRaises(deploy.DeployError):
            self.item.validate()
        self.write("app/a.py", "old app\n")
        self.git("checkout", "-q", "--detach", self.target)
        with self.assertRaises(deploy.DeployError):
            self.item.validate()

    def test_full_client_source_revision_or_invalid_backup_identifier_rejected(self):
        self.item.target = self.source
        with self.assertRaises(deploy.DeployError):
            self.item.validate()
        self.item.target = self.target
        self.item.run_id = "../outside"
        with self.assertRaises(deploy.DeployError):
            self.item.validate()


class RecoveryTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.item.python = Mock(side_effect=self.fake_python)
        self.item.environment_access = Mock()
        self.item.restart = Mock()
        self.item.health = Mock()

    def fake_python(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "-c":
            h = hashlib.sha256((self.root / "pc_client/agent.py").read_bytes()).hexdigest()
            return json.dumps({"source_revision": h, "installers": ["stable-installer", "stable-metadata"],
                               "hermes_url": "http://127.0.0.1:8787"}).encode()
        if args[0] == "deploy/predeploy_backup.py":
            self.item.backup.mkdir(parents=True)
            (self.item.backup / ".env").write_bytes((self.root / ".env").read_bytes())
            (self.item.backup / "COMPLETE.json").write_text(json.dumps({
                "format": "xass-predeploy-rollback", "version": 1,
                "revision": self.before, "database": "sqlite"}))
            # Represent messages arriving after the online DB snapshot.
            self.write("data/live.db", b"new-messages-after-snapshot")
        return b""

    def execute_output(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            self.item.execute()
        return captured.getvalue()

    def assert_runtime_untouched(self):
        self.assertEqual((self.root / "data/live.db").read_bytes(), b"new-messages-after-snapshot")
        self.assertEqual((self.root / "data/music/song.wav").read_bytes(), b"original-music")
        self.assertEqual((self.root / "pc_client/agent.py").read_bytes(), b"stable client\n")
        self.assertEqual((self.root / "data/releases/XASS-Setup.exe").read_bytes(), b"stable-installer")

    def test_success_keeps_clients_original_music_and_new_database_writes(self):
        output = self.execute_output()
        self.assertIn("[OK]", output)
        self.assertNotIn("private-sentinel", output)
        self.assertEqual(self.item.head(), self.target)
        self.assertIn(deploy.PUBLIC_KEY.encode(), (self.root / ".env").read_bytes())
        receipt = json.loads((self.item.backup / "DEPLOYED.json").read_text())
        self.assertEqual(receipt["backend_source_revision"], self.source)
        self.assertFalse(receipt["database_restored"])
        self.item.restart.assert_called_once_with()
        self.item.health.assert_called_once_with(public=True)
        self.assert_runtime_untouched()

    def test_public_route_failure_restores_code_and_exact_env_never_database(self):
        self.item.health.side_effect = [deploy.DeployError("bad public route"), None]
        with self.assertRaisesRegex(deploy.DeployError, "rolled back"):
            self.execute_output()
        self.assertEqual(self.item.head(), self.before)
        self.assertEqual((self.root / ".env").read_bytes(), self.original)
        self.assertEqual(self.item.restart.call_count, 2)
        self.assertFalse((self.item.backup / "DEPLOYED.json").exists())
        self.assert_runtime_untouched()

    def test_interrupt_after_activation_also_rolls_back(self):
        self.item.health.side_effect = [KeyboardInterrupt(), None]
        with self.assertRaisesRegex(deploy.DeployError, "rolled back"):
            self.execute_output()
        self.assertEqual(self.item.head(), self.before)
        self.assertEqual((self.root / ".env").read_bytes(), self.original)
        self.assert_runtime_untouched()

    def test_rollback_restores_original_symbolic_branch_without_moving_it(self):
        self.git("switch", "-qc", "live-baseline")
        self.item.health.side_effect = [deploy.DeployError("bad public route"), None]
        with self.assertRaisesRegex(deploy.DeployError, "rolled back"):
            self.execute_output()
        self.assertEqual(self.git("symbolic-ref", "HEAD").decode().strip(), "refs/heads/live-baseline")
        self.assertEqual(self.item.head(), self.before)

    def test_no_complete_backup_never_starts_mutation(self):
        original = self.fake_python
        def incomplete(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[0] == "deploy/predeploy_backup.py":
                (self.item.backup / "COMPLETE.json").unlink()
            return value
        self.item.python.side_effect = incomplete
        with self.assertRaises(FileNotFoundError):
            self.execute_output()
        self.assertFalse(self.item.mutation_started)
        self.assertEqual(self.item.head(), self.before)
        self.assertEqual((self.root / ".env").read_bytes(), self.original)
        self.item.restart.assert_not_called()

    def test_changed_source_package_rejected_before_restart_and_rolled_back(self):
        before = self.item.snapshot()
        self.item.snapshot = Mock(side_effect=[before, {**before, "source_revision": "0" * 64}])
        with self.assertRaisesRegex(deploy.DeployError, "rolled back"):
            self.execute_output()
        self.assertEqual(self.item.head(), self.before)
        self.item.restart.assert_called_once_with()  # rollback restart only

    def test_external_configuration_edit_is_not_overwritten_by_rollback(self):
        def changed(*, public):
            self.write(".env", b"BOT_TOKEN=new-user-setting\n")
            raise deploy.DeployError("failure")
        self.item.health.side_effect = changed
        with self.assertRaisesRegex(deploy.DeployError, "manual review"):
            self.execute_output()
        self.assertEqual((self.root / ".env").read_bytes(), b"BOT_TOKEN=new-user-setting\n")
        self.assertEqual(self.item.head(), self.target)
        self.assert_runtime_untouched()

    def test_protect_config_is_explicit_head_only_permission_repair_without_restart_or_code_changes(self):
        self.item.environment_access.side_effect = lambda: setattr(self.item, "env_metadata", (self.root / ".env").stat())
        original_command = self.item.command
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            if args[0] == "curl":
                return b"403" if "--head" in args else b'{"status":"ok"}'
            return original_command(*args, **kwargs)
        self.item.command = command
        with patch.object(deploy.os, "fchmod", create=True) as chmod, redirect_stdout(io.StringIO()):
            self.item.protect_config()
        self.assertEqual(chmod.call_args.args[1], 0o600)
        self.item.restart.assert_not_called()
        self.item.python.assert_not_called()
        self.assertEqual(self.item.head(), self.before)
        self.assertEqual((self.root / ".env").read_bytes(), self.original)
        private_requests = [args for args in calls if args[-1] == deploy.LISTEN_URL + "/.env"]
        self.assertEqual(len(private_requests), 1)
        self.assertIn("--head", private_requests[0])
        self.assertFalse(self.item.backup.exists())

    def test_protect_config_never_reverts_hardening_when_public_head_is_still_200(self):
        self.item.environment_access.side_effect = lambda: setattr(self.item, "env_metadata", (self.root / ".env").stat())
        original_command = self.item.command
        self.item.command = lambda *args, **kwargs: b"200" if args[0] == "curl" else original_command(*args, **kwargs)
        with patch.object(deploy.os, "fchmod", create=True) as chmod, redirect_stdout(io.StringIO()), self.assertRaises(deploy.DeployError):
            self.item.protect_config()
        self.assertEqual(chmod.call_count, 1)
        self.assertEqual(chmod.call_args.args[1], 0o600)
        self.item.restart.assert_not_called()
        self.assertEqual(self.item.head(), self.before)

    @unittest.skipUnless(os.name == "posix", "POSIX owner/mode normalization runs on Linux")
    def test_legacy_world_readable_env_is_hardened_and_rollback_keeps_hardening(self):
        path = self.root / ".env"
        path.chmod(0o664)
        original_metadata = path.stat()
        def preflight():
            self.item.env_metadata = original_metadata
            self.item.env_mode_before = 0o664
            self.item.env_mode_after = 0o600
        self.item.environment_access.side_effect = preflight
        def health(*, public):
            current = path.stat()
            self.assertEqual(current.st_mode & 0o777, 0o600)
            self.assertEqual((current.st_uid, current.st_gid), (original_metadata.st_uid, original_metadata.st_gid))
            if public:
                self.assertIn(deploy.PUBLIC_KEY.encode(), path.read_bytes())
                raise deploy.DeployError("injected public verification failure")
            self.assertEqual(path.read_bytes(), self.original)
        self.item.health.side_effect = health
        with self.assertRaisesRegex(deploy.DeployError, "rolled back"):
            self.execute_output()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_bytes(), self.original)
        self.assert_runtime_untouched()


class TransportTests(unittest.TestCase):
    def test_configuration_owner_service_user_and_private_mode_checked_before_mutation(self):
        item = deploy.Deployment(REPO, "a" * 40, "test")
        item.command = Mock(return_value=b"1001\n")
        info = SimpleNamespace(st_uid=1001, st_gid=1002, st_mode=0o100640)
        with patch.object(deploy.Path, "stat", return_value=info), \
             patch.object(deploy.os, "geteuid", return_value=1001, create=True), \
             patch.object(deploy.os, "getegid", return_value=1002, create=True), \
             patch.object(deploy.os, "getgroups", return_value=[1002], create=True):
            item.environment_access()
            self.assertIs(item.env_metadata, info)
            item.command.return_value = b"33\n"
            with self.assertRaises(deploy.DeployError):
                item.environment_access()
            item.command.return_value = b"1001\n"
            info.st_uid = 33
            with self.assertRaises(deploy.DeployError):
                item.environment_access()
            info.st_uid = 1001
            for mode in (0o644, 0o664):
                info.st_mode = 0o100000 | mode
                item.environment_access()
                self.assertEqual(item.env_mode_before, mode)
                self.assertEqual(item.env_mode_after, 0o600)
            info.st_mode = 0o100666
            with self.assertRaises(deploy.DeployError):
                item.environment_access()

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and mode are verified on Linux")
    def test_atomic_configuration_replace_preserves_owner_group_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_bytes(b"OLD=value\n")
            path.chmod(0o640)
            before = path.stat()
            deploy.atomic_private(path, b"NEW=value\n", metadata=before)
            after = path.stat()
            self.assertEqual((after.st_uid, after.st_gid, after.st_mode),
                             (before.st_uid, before.st_gid, before.st_mode))
            self.assertEqual(path.read_bytes(), b"NEW=value\n")

    def test_only_backend_service_restart_and_health_real_401_required(self):
        item = deploy.Deployment(REPO, "a" * 40, "test")
        item.command = Mock(return_value=b"")
        item.restart()
        self.assertEqual(item.command.call_args.args[:5],
                         ("sudo", "-n", "systemctl", "restart", "serverredus-backend"))
        item.command = Mock(side_effect=[b'{"status":"ok"}', b"401", b'{"status":"ok"}'])
        item.health(public=True)
        self.assertIn("--noproxy", item.command.call_args_list[0].args)
        self.assertIn("--data-binary", item.command.call_args_list[1].args)
        item.command = Mock(side_effect=[b'{"status":"ok"}', b"200"])
        with self.assertRaises(deploy.DeployError):
            item.health(public=True)

    def test_dispatch_defaults_readonly_and_has_pinned_ci_scope_no_release_publication(self):
        workflow = (REPO / ".github/workflows/server-route-diagnostics.yml").read_text()
        self.assertIn("default: inspect", workflow)
        self.assertIn("DEPLOY_BACKEND_ONLY_KEEP_CLIENTS", workflow)
        self.assertIn("HARDEN_PRIVATE_CONFIG", workflow)
        self.assertIn("options: [inspect, protect-config, protect-storage, protect-web, deploy-backend-only]", workflow)
        self.assertIn("HARDEN_PRIVATE_STORAGE", workflow)
        self.assertIn("--protect-storage", workflow)
        self.assertIn("HARDEN_PRIVATE_WEB_FILES", workflow)
        self.assertIn("deploy/nginx_private_guard.py' | sudo -n python3 - --run-id", workflow)
        self.assertIn("target != os.environ['WORKFLOW_SHA']", workflow)
        self.assertIn("actions/workflows/checks.yml/runs?head_sha=", workflow)
        self.assertIn("actions/runs/41105958562", workflow)
        self.assertIn("group: xass-production", workflow)
        self.assertIn("bash deploy/github_ssh.sh exec", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("gh release", workflow)
        self.assertNotIn("deploy/update.sh", workflow)


class StorageProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "server"
        self.root.mkdir()
        self.item = deploy.Deployment(self.root, "a" * 40, "storage-test")
        self.item.head = Mock(return_value=deploy.BEFORE)
        self.item.clean = Mock()
        self.item.environment_access = Mock()
        self.item.python = Mock(return_value=b"FIXED_SQLITE_CONFIRMED\n")
        self.item.health = Mock()
        self.item.restart = Mock()
        self.private_status = b"403"
        self.calls = []
        self.metadata = {}
        for index, relative in enumerate(("data/serverredus.db", "data/serverredus.db-wal", "data/serverredus.db-shm", ".git")):
            self.metadata[self.root / relative] = dict(st_dev=1, st_ino=100 + index, st_uid=1000, st_gid=1000,
                st_mode=0o40755 if relative == ".git" else 0o100664, st_nlink=2 if relative == ".git" else 1)
        self.descriptors = {}
        self.changed = []
        self.closes = []
        self.extra_validation = None

    def command(self, *args, **kwargs):
        self.calls.append(args)
        self.assertEqual(args[0], "curl")
        if "--head" not in args:
            self.assertEqual(args[-1], deploy.LISTEN_URL + "/health")
            return b'{"status":"ok"}'
        path = args[-1].removeprefix(deploy.LISTEN_URL)
        private = path in ("/.env", "/.git/config") or path.startswith("/data/")
        return self.private_status + b"\ttext/html" if private else b"200\ttext/html"

    def perform(self):
        self.item.command = Mock(side_effect=self.command)
        original_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            if path in self.metadata:
                info = self.metadata[path]
                if info is None:
                    raise FileNotFoundError(2, "missing optional fixture")
                return SimpleNamespace(**info)
            return original_lstat(path, *args, **kwargs)
        def open_target(path, flags):
            self.assertIn(path, self.metadata)
            self.assertFalse(flags & os.O_CREAT)
            if hasattr(os, "O_NOFOLLOW"):
                self.assertTrue(flags & os.O_NOFOLLOW)
            if hasattr(os, "O_NONBLOCK"):
                self.assertTrue(flags & os.O_NONBLOCK)
            descriptor = 100 + len(self.descriptors)
            self.descriptors[descriptor] = path
            if self.extra_validation:
                self.extra_validation(path)
            return descriptor
        def chmod(descriptor, mode):
            self.changed.append((self.descriptors[descriptor], mode))
            self.metadata[self.descriptors[descriptor]]["st_mode"] = (self.metadata[self.descriptors[descriptor]]["st_mode"] & ~0o7777) | mode
        with patch.object(deploy.os, "geteuid", return_value=1000, create=True), \
                patch.object(deploy, "no_links") as links, patch.object(Path, "lstat", lstat), \
                patch.object(deploy.os, "open", side_effect=open_target), \
                patch.object(deploy.os, "fstat", side_effect=lambda descriptor: SimpleNamespace(**self.metadata[self.descriptors[descriptor]])), \
                patch.object(deploy.os, "fchmod", side_effect=chmod, create=True), \
                patch.object(deploy.os, "close", side_effect=lambda descriptor: self.closes.append(descriptor)), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("NO storage/config bytes may be read")), \
                patch.object(deploy.os, "read", side_effect=AssertionError("NO descriptor data may be read")), \
                patch.object(deploy, "private_read", side_effect=AssertionError("NO private reads")), \
                redirect_stdout(io.StringIO()) as stdout:
            self.item.protect_storage()
        return stdout.getvalue(), links

    def test_fixed_owner_only_targets_no_reads_writes_restart_or_checkout(self):
        output, links = self.perform()
        self.assertIn("[OK]", output)
        self.assertEqual(self.changed, [(path, 0o700 if path.name == ".git" else 0o600) for path in self.metadata])
        self.assertEqual(len(self.closes), 4)
        self.item.clean.assert_called_once_with()
        self.item.environment_access.assert_called_once_with()
        self.item.restart.assert_not_called()
        self.item.python.assert_called_once_with("-c", deploy.STORAGE_CONFIG_CHECK, timeout=20)
        for path in self.metadata:
            self.assertIn(((path,),), [(call.args,) for call in links.call_args_list])
        for args in self.calls:
            if args[-1] != deploy.LISTEN_URL + "/health":
                self.assertIn("--head", args)
                self.assertIn("/dev/null", args)

    def test_optional_sidecars_may_be_absent(self):
        self.metadata[self.root / "data/serverredus.db-wal"] = None
        self.metadata[self.root / "data/serverredus.db-shm"] = None
        self.perform()
        self.assertEqual(len(self.changed), 2)
        self.assertFalse(any("-wal" in args[-1] or "-shm" in args[-1] for args in self.calls))

    def test_foreign_owner_or_link_or_wrong_type_rejects_everything_before_chmod(self):
        git_path = self.root / ".git"
        initial = dict(self.metadata[git_path])
        for change in ({"st_uid": 33}, {"st_mode": 0o120777}, {"st_mode": 0o100644}):
            self.metadata[git_path] = {**initial, **change}
            self.descriptors.clear()
            with self.subTest(change=change), self.assertRaises(deploy.DeployError):
                self.perform()
            self.assertEqual(self.changed, [])

    def test_database_hardlink_or_fifo_is_rejected(self):
        path = self.root / "data/serverredus.db"
        original = dict(self.metadata[path])
        for change in ({"st_nlink": 2}, {"st_mode": 0o010600}):
            self.metadata[path] = {**original, **change}
            with self.subTest(change=change), self.assertRaises(deploy.DeployError):
                self.perform()
            self.assertEqual(self.changed, [])

    def test_descriptor_identity_drift_is_rejected_before_any_chmod(self):
        self.extra_validation = lambda path: self.metadata[path].update(st_ino=999)
        with self.assertRaises(deploy.DeployError):
            self.perform()
        self.assertFalse(self.changed)
        self.assertEqual(len(self.closes), 1)

    def test_public_probe_failure_keeps_restricted_modes(self):
        self.private_status = b"200"
        with self.assertRaises(deploy.DeployError):
            self.perform()
        self.assertEqual(len(self.changed), 4)
        self.assertEqual(self.metadata[self.root / ".git"]["st_mode"] & 0o777, 0o700)
        self.assertEqual(self.metadata[self.root / "data/serverredus.db"]["st_mode"] & 0o777, 0o600)
        self.item.restart.assert_not_called()

    def test_wrong_revision_configured_database_or_service_owner_aborts_without_chmod(self):
        self.item.head.return_value = "b" * 40
        with self.assertRaises(deploy.DeployError):
            self.perform()
        self.item.head.return_value = deploy.BEFORE
        self.item.python.return_value = b"OTHER_DATABASE"
        with self.assertRaises(deploy.DeployError):
            self.perform()
        self.item.python.return_value = b"FIXED_SQLITE_CONFIRMED"
        self.item.environment_access.side_effect = deploy.DeployError("different owner")
        with self.assertRaises(deploy.DeployError):
            self.perform()
        self.assertFalse(self.changed)

    def test_sqlite_config_check_accepts_only_exact_database_and_never_connects(self):
        valid = "sqlite:///" + (self.root / "data/serverredus.db").as_posix()
        settings = SimpleNamespace(database_url=valid)
        fake_config = SimpleNamespace(Settings=lambda: settings)
        with patch.dict(sys.modules, {"app.config": fake_config}), patch.object(Path, "cwd", return_value=self.root):
            for url in (valid, valid + "?mode=ro", valid.replace("serverredus.db", "different.db"), "postgresql://example/database"):
                settings.database_url = url
                with self.subTest(url=url), redirect_stdout(io.StringIO()) as output:
                    if url == valid:
                        exec(deploy.STORAGE_CONFIG_CHECK, {})
                        self.assertEqual(output.getvalue().strip(), "FIXED_SQLITE_CONFIRMED")
                    else:
                        with self.assertRaises(SystemExit):
                            exec(deploy.STORAGE_CONFIG_CHECK, {})
                        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
