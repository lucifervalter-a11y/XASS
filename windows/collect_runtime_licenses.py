"""Collect installed build-environment dependency notices without network access."""
from __future__ import annotations
import argparse
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    target = args.destination
    target.mkdir(parents=True, exist_ok=False)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise SystemExit("Python distribution LICENSE.txt was not found; package its official notice before release")
    shutil.copyfile(python_license, target / "python-LICENSE.txt")
    inventory = []
    for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name, version = distribution.metadata["Name"], distribution.version
        folder = re.sub(r"[^a-zA-Z0-9._-]", "_", f"{name}-{version}")
        notices = []
        for entry in distribution.files or []:
            if any(part == ".." for part in entry.parts):
                continue
            if not any(entry.name.lower().startswith(word) for word in ("license", "copying", "notice", "authors")):
                continue
            source = Path(distribution.locate_file(entry))
            if source.is_file() and not source.is_symlink():
                destination = target / folder / str(entry)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                notices.append(destination.relative_to(target).as_posix())
        inventory.append({"name": name, "version": version,
                          "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License", ""),
                          "notices": notices})
    (target / "python-dependencies.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    # This records missing notices honestly. It is not a licensing/legal approval.
    missing = [item["name"] for item in inventory if not item["notices"]]
    (target / "NOTICE-REVIEW.txt").write_text(
        "Dependency metadata and available notices from the exact build environment.\n"
        "Build-only packages may also be listed. Review redistribution obligations\n"
        "and native-library notices before a public stable release.\n"
        "No packaged notice was found for: " + ", ".join(missing) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
