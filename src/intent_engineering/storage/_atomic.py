"""Small fsync-and-replace helpers shared by canonical local stores."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


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
