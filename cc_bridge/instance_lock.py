from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import TextIO

_HELD_LOCKS: set[Path] = set()
_HELD_LOCKS_LOCK = threading.Lock()


class InstanceLock:
    """Small cross-platform non-blocking process lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: TextIO | None = None
        self._locked = False

    def acquire(self) -> bool:
        if self._locked:
            return True
        lock_key = self.path.resolve()
        with _HELD_LOCKS_LOCK:
            if lock_key in _HELD_LOCKS:
                return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False

        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nstarted_at={time.time():.3f}\n")
        handle.flush()
        self._file = handle
        self._locked = True
        with _HELD_LOCKS_LOCK:
            _HELD_LOCKS.add(lock_key)
        return True

    def release(self) -> None:
        handle = self._file
        if not handle:
            return
        lock_key = self.path.resolve()
        try:
            if self._locked:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False
            self._file = None
            with _HELD_LOCKS_LOCK:
                _HELD_LOCKS.discard(lock_key)
            handle.close()
            try:
                self.path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "InstanceLock":
        if not self.acquire():
            raise RuntimeError(f"Another CC Bridge instance is already running: {self.path}")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()
