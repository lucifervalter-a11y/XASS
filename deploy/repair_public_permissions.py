#!/usr/bin/env python3
"""Repair only tracked public web assets, never private/runtime directories."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys


def repair_public_permissions(root: Path) -> tuple[int, int]:
    root = root.absolute()
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError("The checkout must not be a symlink or junction.")
    listing = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, check=True, timeout=30).stdout
    files: set[Path] = set()
    directories: set[Path] = set()
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        relative = PurePosixPath(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative):
            raise ValueError("Unexpected tracked path.")
        public = (
            (relative.suffix == ".php" and (len(relative.parts) == 1 or relative.parts[0] == "projects"))
            or relative.parts[0] == "assets"
            or str(relative) in {"manifest.webmanifest", "sw.js"}
        )
        if not public:
            continue
        target = root.joinpath(*relative.parts)
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Public files must be regular files without hardlinks or symlinks.")
        files.add(target)
        for parent in target.parents:
            if parent == root:
                break  # Never chmod the checkout itself or anything above it.
            if root not in parent.parents:
                raise ValueError("Public path escaped the checkout.")
            directories.add(parent)
    # Validate the complete set before changing any permission.
    for directory in directories:
        if (not stat.S_ISDIR(directory.lstat().st_mode) or directory.is_symlink()
                or (hasattr(directory, "is_junction") and directory.is_junction())):
            raise ValueError("Public parent directories must not contain links.")
    for directory in sorted(directories, key=lambda item: len(item.parts)):
        directory.chmod(0o755)
    for target in files:
        target.chmod(0o644)
    return len(files), len(directories)


def main() -> int:
    try:
        files, directories = repair_public_permissions(Path.cwd())
        print(f"[INFO] Public permissions repaired: {files} tracked files, {directories} public directories.")
        return 0
    except Exception:
        print("[ERROR] Public permission repair refused: check tracked paths, symlinks and ownership.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
