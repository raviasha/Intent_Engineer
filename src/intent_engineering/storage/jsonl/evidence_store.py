"""Atomic JSONL persistence for immutable evidence and connector ingestion ledgers."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from intent_engineering.core.models import EvidenceIngestion, EvidenceRecord
from intent_engineering.storage._atomic import append_durable_line, same_path_lock
from intent_engineering.storage.secure import SecureFile, coerce_secure_file


class EvidenceStoreError(ValueError):
    """Base class for evidence-store integrity failures."""


class ConflictingEvidenceId(EvidenceStoreError):
    """Raised when an immutable evidence ID is reused with different content."""

    def __init__(self, evidence_id: str) -> None:
        self.evidence_id = evidence_id
        super().__init__(f"conflicting evidence id: {evidence_id}")


class LegacyAssociationRequired(EvidenceStoreError):
    """Raised when a custom connector's legacy ownership cannot be inferred safely."""

    def __init__(self, connector_id: str) -> None:
        self.connector_id = connector_id
        super().__init__(f"explicit legacy association required for connector: {connector_id}")


def parse_evidence_lines(
    content: bytes | None,
) -> tuple[tuple[EvidenceRecord, ...], tuple[EvidenceIngestion, ...], tuple[str, ...]]:
    """Parse legacy raw rows and versioned atomic ingestion envelopes."""
    if content is None:
        return (), (), ()
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise EvidenceStoreError("invalid evidence store encoding") from error
    records: dict[str, EvidenceRecord] = {}
    ingestions: list[EvidenceIngestion] = []
    connector_entries: dict[str, list[EvidenceIngestion]] = defaultdict(list)
    associations: set[tuple[str, str]] = set()
    legacy_ids: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise TypeError("evidence row must be an object")
            if "storage_schema_version" in payload:
                ingestion = EvidenceIngestion.model_validate(payload)
                record = ingestion.evidence
                ledger = connector_entries[ingestion.connector_id]
                if ingestion.sequence != len(ledger) + 1:
                    raise ValueError("non-contiguous connector ingestion sequence")
                if (ingestion.connector_id, record.id) in associations:
                    raise ValueError("duplicate connector evidence association")
                predecessors = [
                    item.evidence
                    for item in ledger
                    if item.evidence.connector_type == record.connector_type
                    and item.evidence.external_object_id == record.external_object_id
                ]
                expected_predecessor = predecessors[-1].id if predecessors else None
                if ingestion.predecessor_id != expected_predecessor:
                    raise ValueError("invalid connector evidence predecessor")
                ledger.append(ingestion)
                ingestions.append(ingestion)
                associations.add((ingestion.connector_id, record.id))
            else:
                record = EvidenceRecord.model_validate(payload)
                legacy_ids.append(record.id)
        except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as error:
            raise EvidenceStoreError(f"invalid evidence record at line {line_number}") from error
        existing = records.get(record.id)
        if existing is not None and existing != record:
            raise ConflictingEvidenceId(record.id)
        records.setdefault(record.id, record)
    return tuple(records.values()), tuple(ingestions), tuple(dict.fromkeys(legacy_ids))


class JsonlEvidenceStore:
    """Persist evidence and connector associations together in one append-only file."""

    def __init__(self, path: Path | SecureFile) -> None:
        self._file = coerce_secure_file(path)
        self.path = self._file.path
        self._by_id: dict[str, EvidenceRecord] = {}
        self._by_external_object_id: dict[str, list[EvidenceRecord]] = defaultdict(list)
        self._ledger_by_connector: dict[str, list[EvidenceIngestion]] = defaultdict(list)
        self._legacy_ids: set[str] = set()
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()

    def _rebuild_index_unlocked(self) -> None:
        """Refresh indexes from disk while the caller holds this store's path lock."""
        self._by_id.clear()
        self._by_external_object_id.clear()
        self._ledger_by_connector.clear()
        self._legacy_ids.clear()
        content = self._file.read_optional()
        records, ingestions, legacy_ids = parse_evidence_lines(content)
        for record in records:
            self._by_id[record.id] = record
            self._by_external_object_id[record.external_object_id].append(record)
        for ingestion in ingestions:
            self._ledger_by_connector[ingestion.connector_id].append(ingestion)
        self._legacy_ids.update(legacy_ids)

    @staticmethod
    def _serialized(value: EvidenceRecord | EvidenceIngestion) -> bytes:
        return (
            json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    def put(self, record: EvidenceRecord) -> bool:
        """Append a legacy/unassociated record for import and migration workflows."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            existing = self._by_id.get(record.id)
            if existing is not None:
                if existing != record:
                    raise ConflictingEvidenceId(record.id)
                return False
            append_durable_line(self._file, self._serialized(record))
            self._by_id[record.id] = record
            self._by_external_object_id[record.external_object_id].append(record)
            self._legacy_ids.add(record.id)
            return True

    def associate(self, connector_id: str, record: EvidenceRecord) -> bool:
        """Atomically append a connector association and return whether evidence is new."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            existing = self._by_id.get(record.id)
            if existing is not None and existing != record:
                raise ConflictingEvidenceId(record.id)
            ledger = self._ledger_by_connector[connector_id]
            if any(item.evidence.id == record.id for item in ledger):
                return False
            predecessors = [
                item.evidence
                for item in ledger
                if item.evidence.connector_type == record.connector_type
                and item.evidence.external_object_id == record.external_object_id
            ]
            ingestion = EvidenceIngestion(
                connector_id=connector_id,
                sequence=len(ledger) + 1,
                predecessor_id=predecessors[-1].id if predecessors else None,
                evidence=record,
            )
            append_durable_line(self._file, self._serialized(ingestion))
            evidence_added = existing is None
            if evidence_added:
                self._by_id[record.id] = record
                self._by_external_object_id[record.external_object_id].append(record)
            ledger.append(ingestion)
            return evidence_added

    def migrate_legacy(self, connector_id: str, connector_type: str) -> None:
        """Materialize only safe built-in legacy ownership; custom mappings require a caller."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            candidates = tuple(
                record
                for record in self._by_id.values()
                if record.id in self._legacy_ids
                and record.connector_type == connector_type
                and not any(
                    item.evidence.id == record.id
                    for item in self._ledger_by_connector.get(connector_id, ())
                )
            )
        if not candidates:
            return
        if connector_id != connector_type or connector_type not in {"markdown", "git"}:
            raise LegacyAssociationRequired(connector_id)
        for record in candidates:
            self.associate(connector_id, record)

    def get(self, evidence_id: str) -> EvidenceRecord:
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return self._by_id[evidence_id]

    def versions(self, external_object_id: str) -> Sequence[EvidenceRecord]:
        """Return global provider versions for read-only legacy consumers."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return tuple(self._by_external_object_id.get(external_object_id, ()))

    def ledger(
        self,
        connector_id: str,
        *,
        connector_type: str | None = None,
    ) -> Sequence[EvidenceIngestion]:
        """Return authenticated per-connector append provenance."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            entries = tuple(self._ledger_by_connector.get(connector_id, ()))
            associated_ids = {item.evidence.id for item in entries}
            if connector_type is not None and any(
                record.id in self._legacy_ids
                and record.id not in associated_ids
                and record.connector_type == connector_type
                for record in self._by_id.values()
            ):
                raise LegacyAssociationRequired(connector_id)
            if connector_type is None:
                return entries
            return tuple(item for item in entries if item.evidence.connector_type == connector_type)

    def chain(
        self,
        connector_id: str,
        connector_type: str,
        external_object_id: str,
    ) -> Sequence[EvidenceIngestion]:
        return tuple(
            item
            for item in self.ledger(connector_id, connector_type=connector_type)
            if item.evidence.external_object_id == external_object_id
        )

    def for_connector(self, connector_id: str) -> Sequence[EvidenceRecord]:
        return tuple(item.evidence for item in self.ledger(connector_id))

    def list(self) -> Sequence[EvidenceRecord]:
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return tuple(self._by_id.values())

    def ingestions(self) -> Sequence[EvidenceIngestion]:
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return tuple(
                item
                for connector_id in sorted(self._ledger_by_connector)
                for item in self._ledger_by_connector[connector_id]
            )
