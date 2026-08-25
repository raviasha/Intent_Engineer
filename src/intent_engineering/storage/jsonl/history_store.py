"""Append-only JSONL ChangeSet history persistence."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

from intent_engineering.core.models import ChangeSet
from intent_engineering.storage._atomic import append_durable_line


class HistoryStoreError(ValueError):
    """Raised when persisted ChangeSet history cannot be reconstructed."""


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

    def __init__(self, path: Path) -> None:
        self.path = path
        self._by_subject: dict[str, list[ChangeSet]] = defaultdict(list)
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    changeset = ChangeSet.model_validate_json(line)
                except (json.JSONDecodeError, ValueError) as error:
                    raise HistoryStoreError(
                        f"invalid history record at line {line_number} in {self.path}"
                    ) from error
                self._index(changeset)

    def _index(self, changeset: ChangeSet) -> None:
        for subject_id in set(_subjects(changeset)):
            self._by_subject[subject_id].append(changeset)

    def append(self, changeset: ChangeSet) -> None:
        """Durably append one successfully applied ChangeSet."""
        serialized = json.dumps(
            changeset.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        append_durable_line(self.path, serialized)
        self._index(changeset)

    def history(self, subject_id: str) -> Sequence[ChangeSet]:
        """Return all durable ChangeSets that changed a supplied subject."""
        return tuple(self._by_subject.get(subject_id, ()))
