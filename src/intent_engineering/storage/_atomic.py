"""Small fsync-and-replace helpers shared by canonical local stores."""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock, get_ident
from typing import BinaryIO


class _PathLock:
    """Pair a process-local re-entrant lock with a POSIX advisory file lock."""

    def __init__(self) -> None:
        self.thread_lock = RLock()
        self.process_id = os.getpid()
        self.owner_pid: int | None = None
        self.owner_thread_id: int | None = None
        self.depth = 0
        self.lock_file: BinaryIO | None = None

    def reset_if_inherited(self, current_pid: int) -> None:
        """Discard copied process state after a fork before acquiring a new lock."""
        if self.process_id == current_pid:
            return
        if self.lock_file is not None:
            self.lock_file.close()
        self.thread_lock = RLock()
        self.process_id = current_pid
        self.owner_pid = None
        self.owner_thread_id = None
        self.depth = 0
        self.lock_file = None


_PATH_LOCKS: dict[Path, _PathLock] = {}
_PATH_LOCKS_GUARD = Lock()


@contextmanager
def same_path_lock(path: Path) -> Iterator[None]:
    """Serialize cooperating threads and processes that mutate one durable path."""
    resolved_path = path.resolve()
    current_pid = os.getpid()
    current_thread_id = get_ident()
    with _PATH_LOCKS_GUARD:
        path_lock = _PATH_LOCKS.setdefault(resolved_path, _PathLock())
        path_lock.reset_if_inherited(current_pid)

    lock_path = resolved_path.with_name(f".{resolved_path.name}.lock")
    with path_lock.thread_lock:
        if path_lock.owner_pid == current_pid and path_lock.owner_thread_id == current_thread_id:
            path_lock.depth += 1
            try:
                yield
            finally:
                path_lock.depth -= 1
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except BaseException:
            lock_file.close()
            raise
        path_lock.owner_pid = current_pid
        path_lock.owner_thread_id = current_thread_id
        path_lock.depth = 1
        path_lock.lock_file = lock_file
        try:
            yield
        finally:
            path_lock.depth -= 1
            if path_lock.depth == 0:
                path_lock.owner_pid = None
                path_lock.owner_thread_id = None
                path_lock.lock_file = None
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Durably replace ``path`` without exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def append_durable_line(path: Path, line: bytes) -> None:
    """Append one complete JSONL record and flush it durably."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as output:
        output.write(line)
        output.flush()
        os.fsync(output.fileno())
