"""Small fsync-and-replace helpers shared by canonical local stores."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock, get_ident

from intent_engineering.storage.secure import SecureFile, coerce_secure_file


class _PathLock:
    """Pair a process-local re-entrant lock with a POSIX advisory file lock."""

    def __init__(self) -> None:
        self.thread_lock = RLock()
        self.process_id = os.getpid()
        self.owner_pid: int | None = None
        self.owner_thread_id: int | None = None
        self.depth = 0
        self.lock_fd: int | None = None

    def reset_if_inherited(self, current_pid: int) -> None:
        """Discard copied process state after a fork before acquiring a new lock."""
        if self.process_id == current_pid:
            return
        self.drop_inherited_state()

    def drop_inherited_state(self) -> None:
        """Release child-side descriptors and discard copied lock ownership."""
        if self.lock_fd is not None:
            os.close(self.lock_fd)
        self.thread_lock = RLock()
        self.process_id = os.getpid()
        self.owner_pid = None
        self.owner_thread_id = None
        self.depth = 0
        self.lock_fd = None


_PATH_LOCKS: dict[tuple[int, int, str], _PathLock] = {}
_PATH_LOCKS_GUARD = Lock()


def _reset_path_locks_after_fork() -> None:
    """Discard copied lock state in a forked child without changing the parent."""
    global _PATH_LOCKS, _PATH_LOCKS_GUARD
    for path_lock in _PATH_LOCKS.values():
        path_lock.drop_inherited_state()
    _PATH_LOCKS = {}
    _PATH_LOCKS_GUARD = Lock()


os.register_at_fork(after_in_child=_reset_path_locks_after_fork)


@contextmanager
def same_path_lock(path: Path | SecureFile) -> Iterator[None]:
    """Serialize cooperating threads and processes that mutate one durable path."""
    secure_file = coerce_secure_file(path)
    current_pid = os.getpid()
    current_thread_id = get_ident()
    try:
        with _PATH_LOCKS_GUARD:
            path_lock = _PATH_LOCKS.setdefault(secure_file.lock_key, _PathLock())
            path_lock.reset_if_inherited(current_pid)

        with path_lock.thread_lock:
            if (
                path_lock.owner_pid == current_pid
                and path_lock.owner_thread_id == current_thread_id
            ):
                path_lock.depth += 1
                try:
                    yield
                finally:
                    path_lock.depth -= 1
                return

            lock_fd = secure_file.open_lock()
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except BaseException:
                os.close(lock_fd)
                raise
            path_lock.owner_pid = current_pid
            path_lock.owner_thread_id = current_thread_id
            path_lock.depth = 1
            path_lock.lock_fd = lock_fd
            try:
                yield
            finally:
                path_lock.depth -= 1
                if path_lock.depth == 0:
                    path_lock.owner_pid = None
                    path_lock.owner_thread_id = None
                    path_lock.lock_fd = None
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
    finally:
        secure_file.close()


def atomic_write_bytes(path: Path | SecureFile, content: bytes) -> None:
    """Durably replace ``path`` without exposing a partially written file."""
    secure_file = coerce_secure_file(path)
    try:
        secure_file.atomic_write(content)
    finally:
        secure_file.close()


def append_durable_line(path: Path | SecureFile, line: bytes) -> None:
    """Append one complete JSONL record and flush it durably."""
    secure_file = coerce_secure_file(path)
    try:
        secure_file.append(line)
    finally:
        secure_file.close()
