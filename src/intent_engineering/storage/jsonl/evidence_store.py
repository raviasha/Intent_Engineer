"""Append-only, immutable JSONL evidence persistence."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from intent_engineering.core.models import EvidenceRecord
from intent_engineering.storage._atomic import append_durable_line


class EvidenceStoreError(ValueError):
    """Base class for evidence-store integrity failures."""


class ConflictingEvidenceId(EvidenceStoreError):
    """Raised when an immutable evidence ID is reused with different content."""

    def __init__(self, evidence_id: str) -> None:
        self.evidence_id = evidence_id
        super().__init__(f"conflicting evidence id: {evidence_id}")


class JsonlEvidenceStore:
    """Durably append immutable evidence records and rebuild indexes on open."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._by_id: dict[str, EvidenceRecord] = {}
        self._by_external_object_id: dict[str, list[EvidenceRecord]] = defaultdict(list)
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    record = EvidenceRecord.model_validate_json(line)
                except (json.JSONDecodeError, ValueError) as error:
                    raise EvidenceStoreError(
                        f"invalid evidence record at line {line_number} in {self.path}"
                    ) from error
                existing = self._by_id.get(record.id)
                if existing is not None:
                    if existing != record:
                        raise ConflictingEvidenceId(record.id)
                    continue
                self._by_id[record.id] = record
                self._by_external_object_id[record.external_object_id].append(record)

    def put(self, record: EvidenceRecord) -> bool:
        """Append a new record, returning false only for an exact existing record."""
        existing = self._by_id.get(record.id)
        if existing is not None:
            if existing != record:
                raise ConflictingEvidenceId(record.id)
            return False
        serialized = json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        append_durable_line(self.path, serialized)
        self._by_id[record.id] = record
        self._by_external_object_id[record.external_object_id].append(record)
        return True

    def get(self, evidence_id: str) -> EvidenceRecord:
        """Return an immutable record by its stable evidence ID."""
        return self._by_id[evidence_id]

    def versions(self, external_object_id: str) -> Sequence[EvidenceRecord]:
        """Return source versions in their durable append order."""
        return tuple(self._by_external_object_id.get(external_object_id, ()))
