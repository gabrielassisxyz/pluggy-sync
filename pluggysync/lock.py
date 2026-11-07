"""One writer per database file, across processes.

A manual run overlapping a scheduled one would otherwise sweep the other's live
run as a crash. The lock is `flock` on `<realpath(db)>.lock`, so two spellings
of one path contend for one lock, and the kernel drops it when the holder dies,
however it dies. Writers take it exclusive; read-only modes take it shared.
"""

from __future__ import annotations

import fcntl
import os


class LockHeld(Exception):
    """Another process holds the lock; the caller exits instead of waiting."""

    def __init__(self, path: str) -> None:
        super().__init__(f"another process holds {path}")
        self.path = path


class LockRefused(Exception):
    """The lock file is not the plain private file this process created."""


def lock_path(db_path: str) -> str:
    return os.path.realpath(db_path) + ".lock"


class ProcessLock:
    def __init__(self, fd: int, path: str) -> None:
        self._fd = fd
        self.path = path

    @classmethod
    def exclusive(cls, db_path: str) -> ProcessLock:
        return cls._take(db_path, fcntl.LOCK_EX)

    @classmethod
    def shared(cls, db_path: str) -> ProcessLock:
        return cls._take(db_path, fcntl.LOCK_SH)

    @classmethod
    def _take(cls, db_path: str, operation: int) -> ProcessLock:
        path = lock_path(db_path)
        # O_NOFOLLOW: a symlink planted at the lock path would move the lock
        # onto some other file, and two processes would each hold "the" lock.
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            links = os.fstat(fd).st_nlink
            if links > 1:
                raise LockRefused(f"{path} has {links} links; a lock file must have one")
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockHeld(path) from None
        except BaseException:
            os.close(fd)
            raise
        return cls(fd, path)

    def release(self) -> None:
        if self._fd < 0:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = -1
