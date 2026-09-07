#!/usr/bin/env python3
"""XASS portable server migration CLI (restore/inspect use Python stdlib only)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.server_migration import MigrationError, export_server_archive, inspect_server_archive, restore_server_archive


def main() -> int:
    parser = argparse.ArgumentParser(description="Export and restore a complete XASS server, including keys and uploads")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Create a private .tar.gz archive (contains secrets)")
    export.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    export.add_argument("--output", type=Path, required=True)
    inspect = commands.add_parser("inspect", help="Verify every checksum without restoring")
    inspect.add_argument("archive", type=Path)
    restore = commands.add_parser("restore", help="Restore into an empty, dedicated directory")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--target", type=Path, required=True)
    for command in (inspect, restore):
        command.add_argument("--max-gib", type=int, default=100, help="Maximum uncompressed size (default: 100 GiB)")
    args = parser.parse_args()
    try:
        if args.command == "export":
            # Defer application dependencies: a fresh server can inspect/restore with stock Python.
            from app.config import Settings
            root = args.root.resolve()
            output = args.output.absolute()
            if output.is_dir():
                output /= "xass-server-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".tar.gz"
            os.chdir(root)
            result = export_server_archive(root, output, Settings(_env_file=root / ".env"))
        else:
            if args.max_gib < 1:
                raise MigrationError("--max-gib must be positive")
            if args.command == "inspect":
                result = inspect_server_archive(args.archive, max_bytes=args.max_gib * 1024**3)
            else:
                result = restore_server_archive(args.archive, args.target, postgres_url=os.environ.get("XASS_RESTORE_DATABASE_URL"), max_bytes=args.max_gib * 1024**3)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ImportError:
        print("Export needs XASS dependencies: run with the source server's .venv/bin/python. Restore and inspect need only Python 3.11+.", file=sys.stderr)
    except (MigrationError, OSError, ValueError, sqlite3.Error) as exc:
        # Validation errors from Settings can contain secret inputs, so don't print their repr.
        detail = str(exc) if isinstance(exc, MigrationError) else "Filesystem/database/configuration error. Check paths, permissions, free space and settings."
        print(f"Migration failed: {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
