"""Windows supervision fixture: a loader and a temporary child both write files.

Uses only a caller-created test directory. Never starts an installer or accesses
an installed application, user preferences, registry, model, or running agent.
"""
from pathlib import Path
import os
import subprocess
import sys
import time


def write_for(root, role, duration):
    (root / (role + ".pid")).write_text(str(os.getpid()), encoding="ascii")
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        with (root / (role + ".writes")).open("ab") as stream:
            stream.write(b"writer\n")
            stream.flush()
            os.fsync(stream.fileno())
        time.sleep(0.02)
    (root / (role + ".done")).write_text("finished", encoding="ascii")


if __name__ == "__main__":
    role, directory, parent_seconds, child_seconds = sys.argv[1:]
    root = Path(directory)
    if role == "parent":
        subprocess.Popen([sys.executable, __file__, "child", str(root), parent_seconds, child_seconds],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not (root / "child.writes").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Writer child did not start")
            time.sleep(0.01)
    write_for(root, role, float(parent_seconds if role == "parent" else child_seconds))
