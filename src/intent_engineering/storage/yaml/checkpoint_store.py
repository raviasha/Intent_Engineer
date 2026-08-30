"""Atomically persisted connector checkpoints with optimistic updates."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from intent_engineering.core.models import SyncCheckpoint
from intent_engineering.storage._atomic import atomic_write_bytes, same_path_lock
from intent_engineering.storage.secure import SecureFile, coerce_secure_file


class CheckpointStoreError(ValueError):
    """Base class for checkpoint-store consistency failures."""


class StaleCheckpoint(CheckpointStoreError):
    """Raised when the stored checkpoint no longer matches a CAS expectation."""

    def __init__(self, connector_id: str) -> None:
        self.connector_id = connector_id
        super().__init__(f"stale checkpoint for connector: {connector_id}")


class YamlCheckpointStore:
    """Store one typed checkpoint per connector in atomically replaced YAML."""

    def __init__(self, path: Path | SecureFile) -> None:
        self._file = coerce_secure_file(path)
        self.path = self._file.path

    def _load_all_unlocked(self) -> dict[str, SyncCheckpoint]:
        """Load checkpoints while the caller holds this store's path lock."""
        content = self._file.read_optional()
        if content is None:
            return {}
        loaded = yaml.safe_load(content.decode("utf-8"))
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise CheckpointStoreError("checkpoint YAML must contain a mapping")
        records = loaded.get("checkpoints", {})
        if not isinstance(records, dict):
            raise CheckpointStoreError("checkpoints must contain a mapping")
        return {
            connector_id: SyncCheckpoint.model_validate(cast(dict[str, Any], record))
            for connector_id, record in records.items()
        }

    def _write_all(self, checkpoints: dict[str, SyncCheckpoint]) -> None:
        data = {
            "checkpoints": {
                connector_id: checkpoint.model_dump(mode="json")
                for connector_id, checkpoint in sorted(checkpoints.items())
            }
        }
        content = cast(str, yaml.safe_dump(data, allow_unicode=True, sort_keys=True)).encode(
            "utf-8"
        )
        atomic_write_bytes(self._file, content)

    def get(self, connector_id: str) -> SyncCheckpoint | None:
        """Return the current durable checkpoint for a connector."""
        with same_path_lock(self._file):
            return self._load_all_unlocked().get(connector_id)

    def compare_and_set(
        self,
        connector_id: str,
        expected: SyncCheckpoint | None,
        cursor: str | None,
        committed_at: datetime,
        consumed_evidence_ids: Sequence[str] = (),
    ) -> SyncCheckpoint:
        """Atomically persist a new cursor only when the expected value still matches."""
        with same_path_lock(self._file):
            checkpoints = self._load_all_unlocked()
            if checkpoints.get(connector_id) != expected:
                raise StaleCheckpoint(connector_id)
            checkpoint = SyncCheckpoint(
                connector_id=connector_id,
                cursor=cursor,
                committed_at=committed_at,
                consumed_evidence_ids=tuple(consumed_evidence_ids),
            )
            checkpoints[connector_id] = checkpoint
            self._write_all(checkpoints)
            return checkpoint

    def close(self) -> None:
        """Release the store's held descriptor."""
        self._file.close()
