"""Optional native-local/cast audio arbitration. No socket, PID or shell.

The two OS-held locks are released even after a crash. A cast first reserves
priority, then waits for the local host to cancel decoding and release audio.
No agent audio starts until that silence has been established.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path


def safe_path(path: Path) -> Path:
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink() or (component.exists() and getattr(component.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Unsafe music storage")
    return path


class FileLock:
    def __init__(self, path: Path):
        self.path = safe_path(path)
        self.stream = None

    def acquire(self) -> bool:
        if self.stream is not None:
            return True
        safe_path(self.path)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, 2) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def release(self) -> None:
        stream, self.stream = self.stream, None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class AudioOwnership:
    def __init__(self, root: Path, *, local: bool):
        self.folder = safe_path(root) / "native-music"
        self.local = local
        self.priority = FileLock(self.folder / "cast.lock")
        self.audio = FileLock(self.folder / "audio.lock")
        self.guard = threading.RLock()
        # Updated agents establish the lock namespace before first playback,
        # so opening the native UI during an existing cast is also safe.
        self.folder.mkdir(exist_ok=True)

    def cast_pending(self) -> bool:
        with self.guard:
            if not self.folder.exists():
                return False
            # Serialize probes with this local owner's brief acquisition of
            # priority. Otherwise its watcher can mistake itself for a cast.
            probe = FileLock(self.folder / "cast.lock")
            acquired = probe.acquire()
            probe.release()
            return not acquired

    def acquire(self) -> None:
        with self.guard:
            if not self.folder.exists():  # Legacy installations keep their behavior.
                return
            if self.audio.stream is not None:
                return
            priority_deadline = time.monotonic() + (0 if self.local else 3)
            while not self.priority.acquire():
                if time.monotonic() >= priority_deadline:
                    raise ValueError("Дождитесь остановки трансляции с телефона")
                time.sleep(0.01)
            try:
                deadline = time.monotonic() + (0 if self.local else 3)
                while not self.audio.acquire():
                    if time.monotonic() >= deadline:
                        raise ValueError("Другой плеер ещё использует звук. Остановите его и повторите")
                    time.sleep(0.025)
            except Exception:
                self.priority.release()
                raise
            if self.local:
                self.priority.release()

    def release(self) -> None:
        with self.guard:
            self.audio.release()
            self.priority.release()
