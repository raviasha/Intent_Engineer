"""Atomically persisted connector checkpoints with optimistic updates."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from intent_engineering.core.models import SyncCheckpoint
from intent_engineering.storage._atomic import atomic_write_bytes


class CheckpointStoreError(ValueError):
    """Base class for checkpoint-store consistency failures."""


class StaleCheckpoint(CheckpointStoreError):
    """Raised when the stored checkpoint no longer matches a CAS expectation."""

    def __init__(self, connector_id: str) -> None:
        self.connector_id = connector_id
        super().__init__(f"stale checkpoint for connector: {connector_id}")


class YamlCheckpointStore:
    """Store one typed checkpoint per connector in atomically replaced YAML."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load_all(self) -> dict[str, SyncCheckpoint]:
        if not self.path.exists():
            return {}
        loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise CheckpointStoreError(f"checkpoint YAML must contain a mapping: {self.path}")
        records = loaded.get("checkpoints", {})
        if not isinstance(records, dict):
            raise CheckpointStoreError(f"checkpoints must contain a mapping: {self.path}")
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
        content = cast(str, yaml.safe_dump(data, allow_unicode=True, sort_keys=True)).encode("utf-8")
        atomic_write_bytes(self.path, content)

    def get(self, connector_id: str) -> SyncCheckpoint | None:
        """Return the current durable checkpoint for a connector."""
        return self._load_all().get(connector_id)

    def compare_and_set(
        self,
        connector_id: str,
        expected: SyncCheckpoint | None,
        cursor: str | None,
        committed_at: datetime,
    ) -> SyncCheckpoint:
        """Atomically persist a new cursor only when the expected value still matches."""
        checkpoints = self._load_all()
        if checkpoints.get(connector_id) != expected:
            raise StaleCheckpoint(connector_id)
        checkpoint = SyncCheckpoint(
            connector_id=connector_id,
            cursor=cursor,
            committed_at=committed_at,
        )
        checkpoints[connector_id] = checkpoint
        self._write_all(checkpoints)
        return checkpoint
