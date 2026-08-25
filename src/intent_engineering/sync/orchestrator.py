"""Durable, connector-isolated orchestration for local source syncs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Protocol

import structlog

from intent_engineering.capture.base import Connector, ConnectorError
from intent_engineering.capture.checkpoints import checkpoint_after_discovery
from intent_engineering.core.models import (
    ChangeSet,
    DriftObservation,
    EvidenceDelta,
    Graph,
    ReconciliationCase,
)
from intent_engineering.extract.base import SemanticReasoner
from intent_engineering.storage.interfaces import (
    CaseStore,
    CheckpointStore,
    EvidenceStore,
    GraphStore,
)
from intent_engineering.sync.models import ConnectorRunResult, SyncRunResult

logger = structlog.get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CaseDetector(Protocol):
    """Turn one durable evidence delta and graph state into drift observations."""

    def __call__(self, delta: EvidenceDelta, graph: Graph) -> Sequence[DriftObservation]:
        """Return provider-neutral observations ready for durable case creation."""
        raise NotImplementedError


def _no_cases(delta: EvidenceDelta, graph: Graph) -> Sequence[DriftObservation]:
    """Default to no derived cases until an application supplies semantic comparisons."""
    return ()


class SyncOrchestrator:
    """Advance each connector only after all of its durable work has completed."""

    def __init__(
        self,
        *,
        graph_store: GraphStore,
        evidence_store: EvidenceStore,
        checkpoint_store: CheckpointStore,
        case_store: CaseStore,
        reasoner: SemanticReasoner,
        case_detector: CaseDetector | Callable[[EvidenceDelta, Graph], Sequence[DriftObservation]] = _no_cases,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._graph_store = graph_store
        self._evidence_store = evidence_store
        self._checkpoint_store = checkpoint_store
        self._case_store = case_store
        self._reasoner = reasoner
        self._case_detector = case_detector
        self._clock = clock

    async def run(self, run_id: str, connectors: Sequence[Connector]) -> SyncRunResult:
        """Synchronize connectors independently and aggregate their durable outcomes."""
        started = perf_counter()
        results: dict[str, ConnectorRunResult] = {}
        for connector in connectors:
            prior = self._checkpoint_store.get(connector.connector_id)
            try:
                discovered = await connector.discover(prior.cursor if prior is not None else None)
                records = []
                for item in discovered:
                    raw = await connector.fetch(item.external_object_id, item.external_version)
                    records.append(connector.normalize(raw))
                added = tuple(record for record in records if self._evidence_store.put(record))
                delta = EvidenceDelta(added=added, prior_versions={})
                changes_applied, cases_created = self._apply_delta(delta)
                checkpoint = checkpoint_after_discovery(
                    connector,
                    discovered,
                    self._clock(),
                    prior=prior,
                )
                checkpoint_advanced = prior is None or checkpoint.cursor != prior.cursor
                if checkpoint_advanced:
                    self._checkpoint_store.compare_and_set(
                        connector.connector_id,
                        expected=prior,
                        cursor=checkpoint.cursor,
                        committed_at=checkpoint.committed_at,
                    )
                results[connector.connector_id] = ConnectorRunResult.succeeded(
                    len(added),
                    changes_applied,
                    cases_created,
                    checkpoint_advanced,
                )
            except ConnectorError as error:
                results[connector.connector_id] = ConnectorRunResult.failed(self._redact_error(error))
        result = SyncRunResult.from_connector_results(
            run_id,
            results,
            duration_ms=round((perf_counter() - started) * 1000),
        )
        logger.info(
            "sync_completed",
            run_id=result.run_id,
            status=result.status,
            evidence_added=result.evidence_added,
            changes_applied=result.changes_applied,
            cases_created=result.cases_created,
            duration_ms=result.duration_ms,
        )
        return result

    def _apply_delta(self, delta: EvidenceDelta) -> tuple[int, int]:
        """Reason, validate/apply semantic changes, then persist detected cases."""
        graph = self._graph_store.load()
        assertions = self._reasoner.extract_assertions(delta)
        changeset = ChangeSet.model_validate(self._reasoner.map_to_graph(assertions, graph).model_dump())
        next_graph = self._graph_store.apply(changeset) if changeset.is_semantic else graph
        cases_created = self._persist_detected_cases(delta, next_graph)
        return int(changeset.is_semantic), cases_created

    def _persist_detected_cases(self, delta: EvidenceDelta, graph: Graph) -> int:
        """Persist each new deterministic fingerprint once, after graph application."""
        created = 0
        for observation in sorted(self._case_detector(delta, graph), key=lambda item: item.fingerprint):
            if self._case_store.find_by_fingerprint(observation.fingerprint) is not None:
                continue
            case = ReconciliationCase(
                id=f"case:sha256:{observation.fingerprint}",
                subject_ref=observation.subject_ref,
                case_type=observation.case_type,
                affected_refs=observation.affected_refs,
                evidence_sides=observation.evidence_sides,
                detector_id=observation.detector_id,
                fingerprint=observation.fingerprint,
                created_at=self._clock(),
                requires_human=observation.requires_human,
            )
            created += int(self._case_store.put(case))
        return created

    @staticmethod
    def _redact_error(error: ConnectorError) -> str:
        """Keep operational connector detail out of summaries and logs."""
        del error
        return "connector failed"
