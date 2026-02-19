from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CommitInfo:
    full_hash: str
    short_hash: str
    author: str
    date_iso: str
    subject: str


@dataclass(slots=True)
class UpdateStatus:
    branch: str
    current: CommitInfo | None
    remote: CommitInfo | None
    has_updates: bool
    commits: list[CommitInfo]
    release: dict[str, Any] | None
    changelog_excerpt: str | None
    errors: list[str]


@dataclass(slots=True)
class UpdateRunResult:
    ok: bool
    branch: str
    before: CommitInfo | None
    after: CommitInfo | None
    remote: CommitInfo | None
    changed_files: list[str]
    steps: list[str]
    restart_required: bool
    restart_performed: bool
    error: str | None


@dataclass(slots=True)
class RollbackResult:
    ok: bool
    target_commit: str | None
    before: CommitInfo | None
    after: CommitInfo | None
    steps: list[str]
    restart_required: bool
    restart_performed: bool
    error: str | None


class UpdateError(RuntimeError):
    pass


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _repo_root() -> Path:
    # app/services/updater.py -> app/services -> app -> project root
    return Path(__file__).resolve().parents[2]


def _sanitize_log(text: str) -> str:
    return text.replace("\r", "").rstrip("\n")


def _append_log(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = _now_utc().isoformat()
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {_sanitize_log(text)}\n")


def _run_command(
    args: list[str],
    *,
    cwd: Path,
    log_path: Path | None = None,
    check: bool = True,
    timeout_sec: int | None = 300,
) -> subprocess.CompletedProcess[str]:
    if log_path:
        _append_log(log_path, f"$ {' '.join(args)}")
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        cmd_text = " ".join(args)
        timeout_msg = f"Command timed out after {int(timeout_sec or 0)}s: {cmd_text}"
        if log_path:
            _append_log(log_path, timeout_msg)
        raise UpdateError(timeout_msg) from exc
    if log_path:
        if completed.stdout:
            _append_log(log_path, completed.stdout.strip())
        if completed.stderr:
            _append_log(log_path, completed.stderr.strip())
    if check and completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise UpdateError(stderr)
    return completed


def _is_git_repo(cwd: Path) -> bool:
    completed = _run_command(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        check=False,
    )
    return completed.returncode == 0 and (completed.stdout or "").strip().lower() == "true"


def _command_exists(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def _venv_python(repo_root: Path) -> list[str]:
    windows_python = repo_root / ".venv" / "Scripts" / "python.exe"
    if windows_python.exists():
        return [str(windows_python)]
    linux_python = repo_root / ".venv" / "bin" / "python"
    if linux_python.exists():
        return [str(linux_python)]
    return ["python"]


def _parse_commit_line(raw: str) -> CommitInfo | None:
    text = raw.strip()
    if not text:
        return None
    parts = text.split("\x1f")
    if len(parts) < 5:
        return None
    return CommitInfo(
        full_hash=parts[0].strip(),
        short_hash=parts[1].strip(),
        author=parts[2].strip(),
        date_iso=parts[3].strip(),
        subject=parts[4].strip(),
    )


def get_current_branch(*, repo_root: Path | None = None) -> str:
    root = repo_root or _repo_root()
    completed = _run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root,
        check=False,
    )
    branch = (completed.stdout or "").strip()
    return branch or "main"


def fetch_remote(branch: str, *, repo_root: Path | None = None, log_path: Path | None = None) -> bool:
    root = repo_root or _repo_root()
    completed = _run_command(
        ["git", "fetch", "--prune", "origin", branch],
        cwd=root,
        log_path=log_path,
        check=False,
    )
    return completed.returncode == 0


def _remote_branch_exists(branch: str, *, repo_root: Path) -> bool:
    completed = _run_command(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=repo_root, check=False)
    return completed.returncode == 0


def resolve_branch(settings: Settings, *, repo_root: Path | None = None) -> str:
    root = repo_root or _repo_root()
    preferred = (settings.update_branch or "").strip() or get_current_branch(repo_root=root)
    current = get_current_branch(repo_root=root)
    candidates: list[str] = []
    for item in (preferred, current, "main", "master"):
        value = (item or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    for item in candidates:
        if _remote_branch_exists(item, repo_root=root):
            return item
    return preferred or "main"


def get_commit_info(ref: str, *, repo_root: Path | None = None) -> CommitInfo | None:
    root = repo_root or _repo_root()
    completed = _run_command(
        ["git", "show", "-s", "--format=%H%x1f%h%x1f%an%x1f%cI%x1f%s", ref],
        cwd=root,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return _parse_commit_line(completed.stdout)


def get_remote_commit(branch: str, *, repo_root: Path | None = None) -> CommitInfo | None:
    return get_commit_info(f"origin/{branch}", repo_root=repo_root)


def get_commits_between(
    start_ref: str,
    end_ref: str,
    *,
    repo_root: Path | None = None,
    limit: int = 30,
) -> list[CommitInfo]:
    root = repo_root or _repo_root()
    completed = _run_command(
        [
            "git",
            "log",
            "--reverse",
            f"--max-count={max(1, int(limit))}",
            "--format=%H%x1f%h%x1f%an%x1f%cI%x1f%s",
            f"{start_ref}..{end_ref}",
        ],
        cwd=root,
        check=False,
    )
    if completed.returncode != 0:
        return []
    commits: list[CommitInfo] = []
    for line in (completed.stdout or "").splitlines():
        parsed = _parse_commit_line(line)
        if parsed:
            commits.append(parsed)
    return commits


def get_changed_files_between(start_ref: str, end_ref: str, *, repo_root: Path | None = None) -> list[str]:
    root = repo_root or _repo_root()
    completed = _run_command(
        ["git", "diff", "--name-only", f"{start_ref}..{end_ref}"],
        cwd=root,
        check=False,
    )
    if completed.returncode != 0:
        return []
    files = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    return files


def pull_fast_forward(branch: str, *, repo_root: Path | None = None, log_path: Path | None = None) -> None:
    root = repo_root or _repo_root()
    _run_command(["git", "pull", "--ff-only", "origin", branch], cwd=root, log_path=log_path, check=True)


def _install_requirements_if_needed(
    changed_files: list[str],
    *,
    repo_root: Path,
    log_path: Path | None,
) -> bool:
    if "requirements.txt" not in changed_files:
        return False
    requirements_path = repo_root / "requirements.txt"
    if not requirements_path.exists():
        return False
    cmd = [*_venv_python(repo_root), "-m", "pip", "install", "-r", "requirements.txt"]
    _run_command(cmd, cwd=repo_root, log_path=log_path, check=True)
    return True


def _install_node_if_needed(changed_files: list[str], *, repo_root: Path, log_path: Path | None) -> bool:
    files = set(changed_files)
    if "pnpm-lock.yaml" in files and (repo_root / "pnpm-lock.yaml").exists() and _command_exists("pnpm"):
        _run_command(["pnpm", "install", "--frozen-lockfile"], cwd=repo_root, log_path=log_path, check=True)
        return True
    if "package-lock.json" in files and (repo_root / "package-lock.json").exists() and _command_exists("npm"):
        _run_command(["npm", "ci"], cwd=repo_root, log_path=log_path, check=True)
        return True
    if "yarn.lock" in files and (repo_root / "yarn.lock").exists() and _command_exists("yarn"):
        _run_command(["yarn", "install", "--frozen-lockfile"], cwd=repo_root, log_path=log_path, check=True)
        return True
    return False


def _run_migrations_if_needed(changed_files: list[str], *, repo_root: Path, log_path: Path | None) -> bool:
    lowered = [item.lower() for item in changed_files]
    may_need_migrations = any("migrations/" in item or item.endswith("/alembic.ini") or "alembic/" in item for item in lowered)
    if not may_need_migrations:
        return False

    py_cmd = _venv_python(repo_root)
    alembic_ini = repo_root / "alembic.ini"
    if alembic_ini.exists():
        _run_command([*py_cmd, "-m", "alembic", "upgrade", "head"], cwd=repo_root, log_path=log_path, check=True)
        return True

    manage_py = repo_root / "manage.py"
    if manage_py.exists():
        _run_command([*py_cmd, "manage.py", "migrate", "--noinput"], cwd=repo_root, log_path=log_path, check=True)
        return True
    return False


def run_post_update_steps(changed_files: list[str], *, repo_root: Path | None = None, log_path: Path | None = None) -> list[str]:
    root = repo_root or _repo_root()
    steps: list[str] = []

    if _install_requirements_if_needed(changed_files, repo_root=root, log_path=log_path):
        steps.append("pip install -r requirements.txt")
    if _install_node_if_needed(changed_files, repo_root=root, log_path=log_path):
        steps.append("node deps installed")
    if _run_migrations_if_needed(changed_files, repo_root=root, log_path=log_path):
        steps.append("migrations applied")
    return steps


def restart_service(
    settings: Settings,
    *,
    repo_root: Path | None = None,
    log_path: Path | None = None,
) -> str:
    root = repo_root or _repo_root()
    mode = (settings.service_restart_mode or "systemd").strip().lower()
    if mode in {"", "none"}:
        return "restart skipped"

    if mode == "systemd":
        service_name = (settings.systemd_service_name or "serverredus-backend").strip()
        if _command_exists("sudo"):
            completed = _run_command(
                ["sudo", "-n", "systemctl", "restart", service_name],
                cwd=root,
                log_path=log_path,
                check=False,
            )
            if completed.returncode == 0:
                return f"systemd restart: {service_name}"
        _run_command(["systemctl", "restart", service_name], cwd=root, log_path=log_path, check=True)
        return f"systemd restart: {service_name}"

    if mode == "docker_compose":
        cmd = ["docker", "compose"]
        compose_file = (settings.docker_compose_file or "").strip()
        if compose_file:
            cmd.extend(["-f", compose_file])
        cmd.extend(["up", "-d", "--build"])
        service_name = (settings.docker_compose_service or "").strip()
        if service_name:
            cmd.append(service_name)
        _run_command(cmd, cwd=root, log_path=log_path, check=True)
        return "docker compose up -d --build"

    if mode == "pm2":
        process_name = (settings.pm2_process_name or "all").strip() or "all"
        _run_command(["pm2", "restart", process_name], cwd=root, log_path=log_path, check=True)
        return f"pm2 restart {process_name}"

    if mode == "custom":
        custom_cmd = (settings.custom_restart_cmd or "").strip()
        if not custom_cmd:
            raise UpdateError("CUSTOM_RESTART_CMD is empty")
        args = shlex.split(custom_cmd, posix=(os.name != "nt"))
        if not args:
            raise UpdateError("CUSTOM_RESTART_CMD is invalid")
        _run_command(args, cwd=root, log_path=log_path, check=True)
        return f"custom restart: {custom_cmd}"

    raise UpdateError(f"Unknown restart mode: {mode}")


def _state_path(settings: Settings, repo_root: Path) -> Path:
    raw = (settings.update_state_path or "").strip()
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        return path
    return repo_root / "data" / "update_state.json"


def _load_state(settings: Settings, repo_root: Path) -> dict[str, Any]:
    state_path = _state_path(settings, repo_root)
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _save_state(settings: Settings, repo_root: Path, payload: dict[str, Any]) -> None:
    state_path = _state_path(settings, repo_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _log_path(settings: Settings, repo_root: Path) -> Path:
    raw = (settings.update_log_path or "").strip()
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        return path
    return repo_root / "data" / "logs" / "update.log"


def get_latest_release_notes(settings: Settings) -> dict[str, Any] | None:
    repo = (settings.github_repo or "").strip()
    if not repo or "/" not in repo:
        return None
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    token = (settings.github_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=15, headers=headers, follow_redirects=True) as client:
            response = client.get(url)
    except Exception:
        return None
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        logger.warning("GitHub release API error: %s", response.status_code)
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "tag": str(payload.get("tag_name") or "").strip(),
        "name": str(payload.get("name") or "").strip(),
        "body": str(payload.get("body") or "").strip(),
        "published_at": str(payload.get("published_at") or "").strip(),
        "url": str(payload.get("html_url") or "").strip(),
    }


def read_changelog_excerpt(*, repo_root: Path | None = None, max_lines: int = 80) -> str | None:
    root = repo_root or _repo_root()
    changelog_path = root / "CHANGELOG.md"
    if not changelog_path.exists():
        return None
    try:
        raw = changelog_path.read_text(encoding="utf-8")
    except Exception:
        return None
    lines = [line.rstrip() for line in raw.splitlines()]
    if not lines:
        return None
    excerpt = "\n".join(lines[: max(5, max_lines)]).strip()
    return excerpt or None


def get_update_status(settings: Settings) -> UpdateStatus:
    root = _repo_root()
    errors: list[str] = []
    release = get_latest_release_notes(settings)
    changelog_excerpt = read_changelog_excerpt(repo_root=root) if release is None else None

    if not _is_git_repo(root):
        branch = (settings.update_branch or "").strip() or "main"
        errors.append("Каталог приложения не является git-репозиторием (.git отсутствует).")
        errors.append("Источник данных для /update: локальный git (HEAD и origin/<ветка>).")
        return UpdateStatus(
            branch=branch,
            current=None,
            remote=None,
            has_updates=False,
            commits=[],
            release=release,
            changelog_excerpt=changelog_excerpt,
            errors=errors,
        )

    try:
        branch = resolve_branch(settings, repo_root=root)
        fetch_ok = fetch_remote(branch, repo_root=root)
        if not fetch_ok:
            errors.append(f"Не удалось выполнить git fetch origin {branch}.")
        current = get_commit_info("HEAD", repo_root=root)
        remote = get_remote_commit(branch, repo_root=root)
        if current is None:
            errors.append("Не удалось определить текущий commit (HEAD).")
        if remote is None:
            errors.append(f"Не удалось определить commit origin/{branch}.")
        has_updates = bool(current and remote and current.full_hash != remote.full_hash)
        commits = get_commits_between("HEAD", f"origin/{branch}", repo_root=root, limit=30) if has_updates else []
    except Exception as exc:
        errors.append(str(exc))
        branch = (settings.update_branch or "").strip() or get_current_branch(repo_root=root)
        current = get_commit_info("HEAD", repo_root=root)
        remote = None
        has_updates = False
        commits = []

    return UpdateStatus(
        branch=branch,
        current=current,
        remote=remote,
        has_updates=has_updates,
        commits=commits,
        release=release,
        changelog_excerpt=changelog_excerpt,
        errors=errors,
    )


def run_update(settings: Settings, *, execute_restart: bool = True) -> UpdateRunResult:
    root = _repo_root()
    log_path = _log_path(settings, root)
    steps: list[str] = []
    before = get_commit_info("HEAD", repo_root=root)
    remote = None
    branch = (settings.update_branch or "").strip() or get_current_branch(repo_root=root)
    changed_files: list[str] = []
    restart_mode = (settings.service_restart_mode or "systemd").strip().lower()
    restart_required = False
    restart_performed = False
    if not _is_git_repo(root):
        error = "Каталог приложения не является git-репозиторием (.git отсутствует)."
        _append_log(log_path, f"=== update skipped: {error} ===")
        return UpdateRunResult(
            ok=False,
            branch=branch,
            before=before,
            after=before,
            remote=None,
            changed_files=[],
            steps=[],
            restart_required=False,
            restart_performed=False,
            error=error,
        )
    try:
        _append_log(log_path, "=== update start ===")
        branch = resolve_branch(settings, repo_root=root)
        fetch_remote(branch, repo_root=root, log_path=log_path)
        remote = get_remote_commit(branch, repo_root=root)
        if before is None or remote is None:
            raise UpdateError("Cannot resolve current/remote commit")
        if before.full_hash == remote.full_hash:
            _append_log(log_path, "No updates available")
            return UpdateRunResult(
                ok=True,
                branch=branch,
                before=before,
                after=before,
                remote=remote,
                changed_files=[],
                steps=["no updates"],
                restart_required=False,
                restart_performed=False,
                error=None,
            )

        changed_files = get_changed_files_between("HEAD", f"origin/{branch}", repo_root=root)
        pull_fast_forward(branch, repo_root=root, log_path=log_path)
        steps.extend(run_post_update_steps(changed_files, repo_root=root, log_path=log_path))
        restart_required = restart_mode not in {"", "none"}
        if restart_required and execute_restart:
            restart_note = restart_service(settings, repo_root=root, log_path=log_path)
            steps.append(restart_note)
            restart_performed = True
        elif restart_required:
            steps.append("restart deferred")
        after = get_commit_info("HEAD", repo_root=root)
        state = _load_state(settings, root)
        state.update(
            {
                "updated_at": _now_utc().isoformat(),
                "branch": branch,
                "previous_head": before.full_hash,
                "last_known_good": after.full_hash if after else before.full_hash,
            }
        )
        _save_state(settings, root, state)
        _append_log(log_path, "=== update success ===")
        return UpdateRunResult(
            ok=True,
            branch=branch,
            before=before,
            after=after,
            remote=remote,
            changed_files=changed_files,
            steps=steps,
            restart_required=restart_required,
            restart_performed=restart_performed,
            error=None,
        )
    except Exception as exc:
        _append_log(log_path, f"=== update failed: {exc} ===")
        return UpdateRunResult(
            ok=False,
            branch=branch,
            before=before,
            after=get_commit_info("HEAD", repo_root=root),
            remote=remote,
            changed_files=changed_files,
            steps=steps,
            restart_required=restart_required,
            restart_performed=restart_performed,
            error=str(exc),
        )


def rollback(settings: Settings, target_commit: str | None = None, *, execute_restart: bool = True) -> RollbackResult:
    root = _repo_root()
    log_path = _log_path(settings, root)
    before = get_commit_info("HEAD", repo_root=root)
    steps: list[str] = []
    restart_mode = (settings.service_restart_mode or "systemd").strip().lower()
    restart_required = restart_mode not in {"", "none"}
    restart_performed = False
    if not _is_git_repo(root):
        error = "Каталог приложения не является git-репозиторием (.git отсутствует)."
        _append_log(log_path, f"=== rollback skipped: {error} ===")
        return RollbackResult(
            ok=False,
            target_commit=None,
            before=before,
            after=before,
            steps=[],
            restart_required=False,
            restart_performed=False,
            error=error,
        )
    state = _load_state(settings, root)
    candidate = (target_commit or "").strip() or str(state.get("previous_head") or "").strip() or str(state.get("last_known_good") or "").strip()
    if not candidate:
        return RollbackResult(
            ok=False,
            target_commit=None,
            before=before,
            after=before,
            steps=[],
            restart_required=False,
            restart_performed=False,
            error="Rollback commit is not known",
        )
    try:
        _append_log(log_path, f"=== rollback start -> {candidate} ===")
        _run_command(["git", "reset", "--hard", candidate], cwd=root, log_path=log_path, check=True)
        steps.append(f"git reset --hard {candidate}")
        if restart_required and execute_restart:
            restart_note = restart_service(settings, repo_root=root, log_path=log_path)
            steps.append(restart_note)
            restart_performed = True
        elif restart_required:
            steps.append("restart deferred")
        after = get_commit_info("HEAD", repo_root=root)
        if after:
            state.update(
                {
                    "rolled_back_at": _now_utc().isoformat(),
                    "rolled_back_to": after.full_hash,
                    "last_known_good": after.full_hash,
                }
            )
            _save_state(settings, root, state)
        _append_log(log_path, "=== rollback success ===")
        return RollbackResult(
            ok=True,
            target_commit=candidate,
            before=before,
            after=after,
            steps=steps,
            restart_required=restart_required,
            restart_performed=restart_performed,
            error=None,
        )
    except Exception as exc:
        _append_log(log_path, f"=== rollback failed: {exc} ===")
        return RollbackResult(
            ok=False,
            target_commit=candidate,
            before=before,
            after=get_commit_info("HEAD", repo_root=root),
            steps=steps,
            restart_required=restart_required,
            restart_performed=restart_performed,
            error=str(exc),
        )


def read_update_log_tail(settings: Settings, *, lines: int = 40) -> str:
    root = _repo_root()
    path = _log_path(settings, root)
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    all_lines = content.splitlines()
    if lines <= 0:
        selected = all_lines
    else:
        selected = all_lines[-lines:]
    return "\n".join(selected).strip()
