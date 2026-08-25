"""Small fsync-and-replace helpers shared by canonical local stores."""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock


class _PathLock:
    """Pair a process-local re-entrant lock with a POSIX advisory file lock."""

    def __init__(self) -> None:
        self.thread_lock = RLock()


_PATH_LOCKS: dict[Path, _PathLock] = {}
_PATH_LOCKS_GUARD = Lock()


@contextmanager
def same_path_lock(path: Path) -> Iterator[None]:
    """Serialize cooperating threads and processes that mutate one durable path."""
    resolved_path = path.resolve()
    with _PATH_LOCKS_GUARD:
        path_lock = _PATH_LOCKS.setdefault(resolved_path, _PathLock())

    lock_path = resolved_path.with_name(f".{resolved_path.name}.lock")
    with path_lock.thread_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
