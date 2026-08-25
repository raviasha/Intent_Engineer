"""Durable, connector-isolated orchestration for local source syncs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from time import perf_counter
from typing import Protocol

import structlog

from intent_engineering.capture.base import Connector, SourceObject
from intent_engineering.capture.checkpoints import checkpoint_after_discovery
from intent_engineering.core.models import (
    ChangeSet,
    DriftObservation,
    EvidenceDelta,
    EvidenceRecord,
    Graph,
    ReconciliationCase,
    SyncCheckpoint,
)
from intent_engineering.extract.base import SemanticReasoner
from intent_engineering.storage.executor import LocalChangeSetExecutor
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


@dataclass
class _ConnectorProgress:
    """Durable work completed before a connector transaction fails or commits."""

    evidence_added: int = 0
    changes_applied: int = 0
    cases_created: int = 0


@dataclass(frozen=True)
class _PendingConnector:
    """Fetched connector work awaiting semantic and checkpoint completion."""

    connector: Connector
    prior: SyncCheckpoint | None
    discovered: Sequence[SourceObject]
    delta: EvidenceDelta
    progress: _ConnectorProgress


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
        changeset_executor: LocalChangeSetExecutor | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._graph_store = graph_store
        self._evidence_store = evidence_store
        self._checkpoint_store = checkpoint_store
        self._case_store = case_store
        self._reasoner = reasoner
        self._case_detector = case_detector
        self._changeset_executor = changeset_executor
        self._clock = clock

    async def run(self, run_id: str, connectors: Sequence[Connector]) -> SyncRunResult:
        """Synchronize connectors independently and aggregate their durable outcomes."""
        connector_ids = tuple(connector.connector_id for connector in connectors)
        duplicate_id = next(
            (
                connector_id
                for connector_id in connector_ids
                if connector_ids.count(connector_id) > 1
            ),
            None,
        )
        if duplicate_id is not None:
            raise ValueError(f"duplicate connector id: {duplicate_id}")
        started = perf_counter()
        results: dict[str, ConnectorRunResult] = {}
        pending: list[_PendingConnector] = []
        for connector in connectors:
            progress = _ConnectorProgress()
            try:
                prior = self._checkpoint_store.get(connector.connector_id)
                connector_type = self._connector_type(connector)
                self._evidence_store.migrate_legacy(connector.connector_id, connector_type)
                consumed_ids = set(prior.consumed_evidence_ids if prior is not None else ())
                connector_ledger = tuple(
                    self._evidence_store.ledger(
                        connector.connector_id,
                        connector_type=connector_type,
                    )
                )
                associated_ids = {item.evidence.id for item in connector_ledger}
                if consumed_ids - associated_ids:
                    raise ValueError("checkpoint consumption association is invalid")
                ingestions = [
                    item
                    for item in connector_ledger
                    if item.evidence.id not in consumed_ids
                ]
                records = [item.evidence for item in ingestions]
                record_ids = {record.id for record in records}
                prior_versions = {
                    item.evidence.external_object_id: item.predecessor_id
                    for item in ingestions
                    if item.predecessor_id is not None
                }
                discovered = await connector.discover(prior.cursor if prior is not None else None)
                for source_object in discovered:
                    raw = await connector.fetch(
                        source_object.external_object_id,
                        source_object.external_version,
                    )
                    record = connector.normalize(raw)
                    if record.connector_type != connector_type:
                        raise ValueError("connector evidence type mismatch")
                    self._persist_evidence(connector.connector_id, record, progress)
                    ingestion = next(
                        item
                        for item in reversed(
                            self._evidence_store.ledger(
                                connector.connector_id,
                                connector_type=connector_type,
                            )
                        )
                        if item.evidence.id == record.id
                    )
                    if ingestion.predecessor_id is not None:
                        prior_versions[record.external_object_id] = ingestion.predecessor_id
                    if record.id not in consumed_ids and record.id not in record_ids:
                        records.append(record)
                        ingestions.append(ingestion)
                        record_ids.add(record.id)
                delta = EvidenceDelta(
                    added=tuple(records),
                    prior_versions=prior_versions,
                    ingestions=tuple(ingestions),
                )
                pending.append(
                    _PendingConnector(
                        connector=connector,
                        prior=prior,
                        discovered=tuple(discovered),
                        delta=delta,
                        progress=progress,
                    )
                )
            except Exception as error:  # noqa: BLE001 - boundary intentionally preserves BaseException
                results[connector.connector_id] = ConnectorRunResult.failed(
                    self._redact_error(error),
                    evidence_added=progress.evidence_added,
                    changes_applied=progress.changes_applied,
                    cases_created=progress.cases_created,
                )

        semantic_successes: list[_PendingConnector] = []
        for pending_item in pending:
            try:
                if pending_item.delta.added:
                    self._apply_reasoning(pending_item.delta, pending_item.progress)
                semantic_successes.append(pending_item)
            except Exception as error:  # noqa: BLE001 - connector isolation boundary
                results[pending_item.connector.connector_id] = ConnectorRunResult.failed(
                    self._redact_error(error),
                    evidence_added=pending_item.progress.evidence_added,
                    changes_applied=pending_item.progress.changes_applied,
                    cases_created=pending_item.progress.cases_created,
                )

        if semantic_successes:
            try:
                combined = self._combined_delta(semantic_successes)
                case_count, change_count = self._apply_detected_cases(combined)
                if case_count:
                    semantic_successes[0].progress.cases_created += case_count
                    semantic_successes[0].progress.changes_applied += change_count
            except Exception as error:  # noqa: BLE001 - shared detector transaction boundary
                for successful_item in semantic_successes:
                    results[successful_item.connector.connector_id] = ConnectorRunResult.failed(
                        self._redact_error(error),
                        evidence_added=successful_item.progress.evidence_added,
                        changes_applied=successful_item.progress.changes_applied,
                        cases_created=successful_item.progress.cases_created,
                    )
                semantic_successes = []

        for successful_item in semantic_successes:
            try:
                prior = successful_item.prior
                checkpoint = checkpoint_after_discovery(
                    successful_item.connector,
                    successful_item.discovered,
                    self._clock(),
                    prior=prior,
                    consumed_evidence_ids=tuple(
                        dict.fromkeys(
                            (
                                *(prior.consumed_evidence_ids if prior is not None else ()),
                                *(record.id for record in successful_item.delta.added),
                            )
                        )
                    ),
                )
                checkpoint_advanced = prior is None or (
                    checkpoint.cursor != prior.cursor
                    or checkpoint.consumed_evidence_ids != prior.consumed_evidence_ids
                )
                if checkpoint_advanced:
                    self._checkpoint_store.compare_and_set(
                        successful_item.connector.connector_id,
                        expected=prior,
                        cursor=checkpoint.cursor,
                        committed_at=checkpoint.committed_at,
                        consumed_evidence_ids=checkpoint.consumed_evidence_ids,
                    )
                results[successful_item.connector.connector_id] = ConnectorRunResult.succeeded(
                    successful_item.progress.evidence_added,
                    successful_item.progress.changes_applied,
                    successful_item.progress.cases_created,
                    checkpoint_advanced,
                )
            except Exception as error:  # noqa: BLE001 - checkpoint is connector-local
                results[successful_item.connector.connector_id] = ConnectorRunResult.failed(
                    self._redact_error(error),
                    evidence_added=successful_item.progress.evidence_added,
                    changes_applied=successful_item.progress.changes_applied,
                    cases_created=successful_item.progress.cases_created,
                )
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

    def _persist_evidence(
        self,
        connector_id: str,
        record: EvidenceRecord,
        progress: _ConnectorProgress,
    ) -> None:
        """Persist one atomic connector association and count only new evidence."""
        if self._evidence_store.associate(connector_id, record):
            progress.evidence_added += 1

    @staticmethod
    def _connector_type(connector: Connector) -> str:
        connector_type = getattr(connector, "connector_type", connector.connector_id)
        if not isinstance(connector_type, str) or not connector_type:
            raise ValueError("connector type must be a non-empty string")
        return connector_type

    def _apply_reasoning(self, delta: EvidenceDelta, progress: _ConnectorProgress) -> None:
        """Reason and apply one connector's graph groups before combined detection."""
        graph = self._graph_store.load()
        assertions = self._reasoner.extract_assertions(delta)
        changeset = ChangeSet.model_validate(self._reasoner.map_to_graph(assertions, graph).model_dump())
        if changeset.reconciliation_cases_created or changeset.reconciliation_cases_resolved:
            raise ValueError("reasoner cannot supply reconciliation payloads")
        if not changeset.is_semantic:
            return
        if self._changeset_executor is None:
            self._graph_store.apply(changeset)
        else:
            self._changeset_executor.apply(changeset)
        progress.changes_applied += int(changeset.is_semantic)

    @staticmethod
    def _combined_delta(items: Sequence[_PendingConnector]) -> EvidenceDelta:
        records = tuple(record for item in items for record in item.delta.added)
        prior_versions: dict[str, str] = {}
        for item in items:
            for object_id, evidence_id in item.delta.prior_versions.items():
                previous = prior_versions.get(object_id)
                if previous is not None and previous != evidence_id:
                    raise ValueError("ambiguous combined evidence predecessor")
                prior_versions[object_id] = evidence_id
        ingestions = tuple(item for pending in items for item in pending.delta.ingestions)
        return EvidenceDelta(
            added=records,
            prior_versions=prior_versions,
            ingestions=ingestions,
        )

    def _apply_detected_cases(
        self,
        delta: EvidenceDelta,
    ) -> tuple[int, int]:
        """Commit all new combined-run cases through one complete ChangeSet."""
        if not delta.added:
            return 0, 0
        graph = self._graph_store.load()
        cases: list[ReconciliationCase] = []
        for observation in sorted(self._case_detector(delta, graph), key=lambda item: item.fingerprint):
            if self._case_store.find_by_fingerprint(observation.fingerprint) is not None:
                continue
            cases.append(
                ReconciliationCase(
                    id=f"case:sha256:{observation.fingerprint}",
                    subject_ref=observation.subject_ref,
                    case_type=observation.case_type,
                    affected_refs=observation.affected_refs,
                    evidence_sides=observation.evidence_sides,
                    detector_id=observation.detector_id,
                    fingerprint=observation.fingerprint,
                    created_at=self._clock(),
                    created_by=f"detector:{observation.detector_id}",
                    requires_human=observation.requires_human,
                )
            )
        if not cases:
            return 0, 0
        if self._changeset_executor is None:
            raise ValueError("case effects require transaction-level executor")
        case_ids = tuple(case.id for case in cases)
        evidence_refs = tuple(sorted({ref for case in cases for ref in case.all_evidence_refs}))
        material = "\x00".join((str(graph.version), *case_ids, *evidence_refs))
        changeset = ChangeSet(
            id=f"changeset:detect:{sha256(material.encode('utf-8')).hexdigest()}",
            actor="detector:sync",
            timestamp=self._clock(),
            baseline_graph_version=graph.version,
            evidence_refs=evidence_refs,
            nodes_added=(),
            nodes_updated=(),
            nodes_superseded=(),
            edges_added=(),
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=case_ids,
            reconciliation_cases_resolved=(),
            validation_status="validated",
        )
        self._changeset_executor.apply(changeset, created_cases=tuple(cases))
        return len(cases), 1

    @staticmethod
    def _redact_error(error: Exception) -> str:
        """Keep operational connector detail out of summaries and logs."""
        del error
        return "connector failed"
