"""Append-only, immutable JSONL evidence persistence."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from intent_engineering.core.models import EvidenceRecord
from intent_engineering.storage._atomic import append_durable_line, same_path_lock
from intent_engineering.storage.secure import SecureFile, coerce_secure_file


class EvidenceStoreError(ValueError):
    """Base class for evidence-store integrity failures."""


class ConflictingEvidenceId(EvidenceStoreError):
    """Raised when an immutable evidence ID is reused with different content."""

    def __init__(self, evidence_id: str) -> None:
        self.evidence_id = evidence_id
        super().__init__(f"conflicting evidence id: {evidence_id}")


class JsonlEvidenceStore:
    """Durably append immutable evidence records and rebuild indexes on open."""

    def __init__(self, path: Path | SecureFile) -> None:
        self._file = coerce_secure_file(path)
        self.path = self._file.path
        self._by_id: dict[str, EvidenceRecord] = {}
        self._by_external_object_id: dict[str, list[EvidenceRecord]] = defaultdict(list)
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()

    def _rebuild_index_unlocked(self) -> None:
        """Refresh indexes from disk while the caller holds this store's path lock."""
        self._by_id.clear()
        self._by_external_object_id.clear()
        content = self._file.read_optional()
        if content is None:
            return
        try:
            lines = content.decode("utf-8").splitlines(keepends=True)
        except UnicodeError as error:
            raise EvidenceStoreError("invalid evidence store encoding") from error
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = EvidenceRecord.model_validate_json(line)
            except (json.JSONDecodeError, ValueError) as error:
                raise EvidenceStoreError(
                    f"invalid evidence record at line {line_number}"
                ) from error
            existing = self._by_id.get(record.id)
            if existing is not None and existing != record:
                raise ConflictingEvidenceId(record.id)
            if existing is not None:
                continue
            self._by_id[record.id] = record
            self._by_external_object_id[record.external_object_id].append(record)

    def put(self, record: EvidenceRecord) -> bool:
        """Append a new record, returning false only for an exact existing record."""
        serialized = json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            existing = self._by_id.get(record.id)
            if existing is not None:
                legacy_equivalent = (
                    existing.ingested_by is None
                    and record.ingested_by is not None
                    and existing == record.model_copy(update={"ingested_by": None})
                )
                if existing != record and not legacy_equivalent:
                    raise ConflictingEvidenceId(record.id)
                return False
            append_durable_line(self._file, serialized)
            self._by_id[record.id] = record
            self._by_external_object_id[record.external_object_id].append(record)
            return True

    def get(self, evidence_id: str) -> EvidenceRecord:
        """Return an immutable record by its stable evidence ID."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return self._by_id[evidence_id]

    def versions(self, external_object_id: str) -> Sequence[EvidenceRecord]:
        """Return source versions in their durable append order."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return tuple(self._by_external_object_id.get(external_object_id, ()))

    def list(self) -> Sequence[EvidenceRecord]:
        """Return all immutable records in durable append order."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return tuple(self._by_id.values())

    def for_connector(self, connector_id: str) -> Sequence[EvidenceRecord]:
        """Return records in append order for their provider-neutral ingestion boundary."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return tuple(
                record
                for record in self._by_id.values()
                if record.ingested_by == connector_id
                or (record.ingested_by is None and record.connector_type == connector_id)
            )
