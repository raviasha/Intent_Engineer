"""Bounded public reader for the protected shared-state Git ref."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Self

from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.team_state.models import (
    MAX_BUNDLE_BYTES,
    MAX_MANIFEST_BYTES,
    STATE_REF,
    RemoteStateSnapshot,
    TeamStateManifest,
    canonical_manifest_bytes,
)
from intent_engineering.team_state.restore import _GitRefReader, _origin_repository


class GitRefReader:
    """Read one immutable protected-ref snapshot without checking it out."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise TypeError("shared-state ref unavailable")
        self._root = Path(os.path.abspath(root))
        self._reader = _GitRefReader(self._root)
        self._snapshot: RemoteStateSnapshot | None = None

    def close(self) -> None:
        """Release any transport resources owned by the underlying reader."""
        self._snapshot = None
        self._reader.close()

    def fetch_manifest(self, remote: str, ref: str) -> RemoteStateSnapshot:
        """Return the canonical manifest at the one supported remote-tracking ref."""
        if (
            type(remote) is not str
            or remote != "origin"
            or type(ref) is not str
            or ref != STATE_REF
        ):
            raise ValueError("shared-state ref unavailable")
        try:
            commit = self._reader.commit()
            content = self._reader.blob(commit, "manifest.json", MAX_MANIFEST_BYTES)
            loads_strict_object(content.decode("utf-8"))
            manifest = TeamStateManifest.model_validate_json(content)
            if content != canonical_manifest_bytes(manifest):
                raise ValueError("noncanonical shared-state manifest")
            snapshot = RemoteStateSnapshot(
                repository_id=_origin_repository(self._root),
                ref=STATE_REF,
                commit=commit,
                manifest=manifest,
                manifest_bytes=content,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as error:
            error.__traceback__ = None
            raise ValueError("shared-state ref unavailable") from None
        self._snapshot = snapshot
        return snapshot

    def read_blob(self, ref: str, path: str) -> bytes:
        """Read a regular blob from the commit pinned by :meth:`fetch_manifest`."""
        snapshot = self._snapshot
        if type(ref) is not str or ref != STATE_REF or snapshot is None:
            raise ValueError("shared-state ref unavailable")
        try:
            return self._reader.blob(snapshot.commit, path, MAX_BUNDLE_BYTES)
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as error:
            error.__traceback__ = None
            raise ValueError("shared-state blob unavailable") from None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


__all__ = ["GitRefReader"]
