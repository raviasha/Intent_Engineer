"""Provider-neutral ports for canonical local persistence."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from intent_engineering.core.models import ChangeSet, EvidenceRecord, Graph, SyncCheckpoint


class GraphStore(Protocol):
    """Durable canonical graph storage with optimistic ChangeSet application."""

    def initialize(self, graph: Graph) -> None:
        raise NotImplementedError

    def load(self) -> Graph:
        raise NotImplementedError

    def apply(self, changeset: ChangeSet) -> Graph:
        raise NotImplementedError

    def history(self, subject_id: str) -> Sequence[ChangeSet]:
        raise NotImplementedError


class EvidenceStore(Protocol):
    """Immutable, version-addressed evidence persistence."""

    def put(self, record: EvidenceRecord) -> bool:
        raise NotImplementedError

    def get(self, evidence_id: str) -> EvidenceRecord:
        raise NotImplementedError

    def versions(self, external_object_id: str) -> Sequence[EvidenceRecord]:
        raise NotImplementedError


class CheckpointStore(Protocol):
    """Optimistic connector-cursor persistence."""

    def get(self, connector_id: str) -> SyncCheckpoint | None:
        raise NotImplementedError

    def compare_and_set(
        self,
        connector_id: str,
        expected: SyncCheckpoint | None,
        cursor: str | None,
        committed_at: datetime,
    ) -> SyncCheckpoint:
        raise NotImplementedError
