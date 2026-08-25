"""Append-only JSONL ChangeSet history persistence."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

from intent_engineering.core.models import ChangeSet
from intent_engineering.storage._atomic import append_durable_line, same_path_lock
from intent_engineering.storage.secure import SecureFile, coerce_secure_file


class HistoryStoreError(ValueError):
    """Raised when persisted ChangeSet history cannot be reconstructed."""


def serialize_changeset(changeset: ChangeSet) -> bytes:
    """Return one canonical JSONL representation for a validated ChangeSet."""
    return json.dumps(
        changeset.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def _subjects(changeset: ChangeSet) -> Iterable[str]:
    yield from (item.id for item in changeset.nodes_added)
    yield from (item.node_id for item in changeset.nodes_updated)
    yield from changeset.nodes_superseded
    yield from (item.id for item in changeset.edges_added)
    yield from (item.edge_id for item in changeset.edges_updated)
    yield from changeset.edges_superseded
    yield from (item.subject_ref for item in changeset.confidence_changes)
    yield from (item.claim_id for item in changeset.implementation_status_changes)
    yield from changeset.reconciliation_cases_created
    yield from changeset.reconciliation_cases_resolved


class JsonlHistoryStore:
    """Durably append ChangeSets and index their affected subject identities."""

    def __init__(self, path: Path | SecureFile) -> None:
        self._file = coerce_secure_file(path)
        self.path = self._file.path
        self._by_subject: dict[str, list[ChangeSet]] = defaultdict(list)
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()

    def _rebuild_index_unlocked(self) -> None:
        """Refresh indexes from disk while the caller holds this store's path lock."""
        self._by_subject.clear()
        content = self._file.read_optional()
        if content is None:
            return
        try:
            lines = content.decode("utf-8").splitlines(keepends=True)
        except UnicodeError as error:
            raise HistoryStoreError("invalid history store encoding") from error
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                changeset = ChangeSet.model_validate_json(line)
            except (json.JSONDecodeError, ValueError) as error:
                raise HistoryStoreError(
                    f"invalid history record at line {line_number}"
                ) from error
            self._index(changeset)

    def _index(self, changeset: ChangeSet) -> None:
        for subject_id in set(_subjects(changeset)):
            self._by_subject[subject_id].append(changeset)

    def append(self, changeset: ChangeSet) -> None:
        """Durably append one successfully applied ChangeSet."""
        serialized = serialize_changeset(changeset)
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            append_durable_line(self._file, serialized)
            self._index(changeset)

    def history(self, subject_id: str) -> Sequence[ChangeSet]:
        """Return all durable ChangeSets that changed a supplied subject."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return tuple(self._by_subject.get(subject_id, ()))
