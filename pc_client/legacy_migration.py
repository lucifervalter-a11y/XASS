from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import psutil


LEGACY_STARTUP_NAMES = (
    "ServerredusPCAgent.vbs",
    "XASS Agent.vbs",
    "XASS Agent.lnk",
    "Serverredus PC Agent.lnk",
)
LEGACY_RUNTIME_NAMES = (".command-results.json", ".installed-revision")


@dataclass
class MigrationResult:
    config_source: str = ""
    config_migrated: bool = False
    processes_stopped: int = 0
    startup_entries_removed: int = 0
    runtime_files_removed: int = 0


def default_data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "XASS"


def default_legacy_roots() -> list[Path]:
    documents = Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents"
    return [
        documents / "xass" / "pc_client",
        documents / "XASS" / "pc_client",
        documents / "serverredus" / "pc_client",
        documents / "Serverredus" / "pc_client",
    ]


def _normalized(path: Path) -> str:
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path.absolute()).casefold()


def _valid_legacy_config(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(payload, dict) and bool(
        str(payload.get("server_url") or "").strip() and str(payload.get("api_key") or "").strip()
    )


def migrate_config(legacy_roots: Iterable[Path], destination: Path) -> str:
    if _valid_legacy_config(destination):
        return ""
    for root in legacy_roots:
        candidate = root / "config.json"
        if not _valid_legacy_config(candidate):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(".json.migrating")
        shutil.copy2(candidate, temp)
        temp.replace(destination)
        return str(candidate)
    return ""


def stop_legacy_processes(legacy_roots: Iterable[Path]) -> int:
    roots = [_normalized(root) for root in legacy_roots if root]
    targets: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            if process.pid == os.getpid():
                continue
            command = " ".join(process.info.get("cmdline") or []).casefold()
            if not any(name in command for name in ("desktop_app.py", "client_agent.py", "run_agent.bat")):
                continue
            if not any(root in command for root in roots):
                continue
            process.terminate()
            targets.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    _gone, alive = psutil.wait_procs(targets, timeout=4)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
    return len(targets)


def remove_legacy_startup(startup_dir: Path) -> int:
    removed = 0
    for name in LEGACY_STARTUP_NAMES:
        path = startup_dir / name
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def remove_legacy_runtime(legacy_roots: Iterable[Path]) -> int:
    removed = 0
    for root in legacy_roots:
        for name in LEGACY_RUNTIME_NAMES:
            path = root / name
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        updates = root / ".updates"
        try:
            if updates.is_dir():
                shutil.rmtree(updates)
                removed += 1
        except OSError:
            continue
    return removed


def run_migration(
    *,
    data_root: Path | None = None,
    legacy_roots: Iterable[Path] | None = None,
    startup_dir: Path | None = None,
) -> MigrationResult:
    target_root = data_root or default_data_root()
    roots = list(legacy_roots or default_legacy_roots())
    if startup_dir is None:
        startup_dir = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    result = MigrationResult()
    result.processes_stopped = stop_legacy_processes(roots)
    result.config_source = migrate_config(roots, target_root / "config.json")
    result.config_migrated = bool(result.config_source)
    result.startup_entries_removed = remove_legacy_startup(startup_dir)
    result.runtime_files_removed = remove_legacy_runtime(roots)
    target_root.mkdir(parents=True, exist_ok=True)
    report = {**asdict(result), "completed_at": datetime.now(timezone.utc).isoformat()}
    (target_root / "migration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    try:
        result = run_migration()
        if "--quiet" not in sys.argv:
            print(json.dumps(asdict(result), ensure_ascii=False))
        return 0
    except Exception as exc:
        try:
            root = default_data_root()
            root.mkdir(parents=True, exist_ok=True)
            (root / "migration-error.txt").write_text(str(exc), encoding="utf-8")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
