"""Atomic local application service for reconciliation resolution."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from intent_engineering.core.models import (
    ChangeSet,
    ReconciliationCase,
    ReconciliationStatus,
    ResolutionAction,
)
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import (
    JsonlCaseStore,
    validate_case_appends,
)
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.transaction import (
    LocalTransactionCoordinator,
    TransactionRecoveryError,
)
from intent_engineering.storage.yaml.graph_store import YamlGraphStore


class ResolutionUnavailable(ValueError):
    """A deliberately non-enumerating resolution failure."""


class LocalResolutionService:
    """Resolve one human-reviewed case over crash-consistent local state."""

    def __init__(
        self,
        graph_store: YamlGraphStore,
        evidence_store: JsonlEvidenceStore,
        case_store: JsonlCaseStore,
        actor: str,
        *,
        transactions: LocalTransactionCoordinator | None = None,
        principals: frozenset[str] | None = None,
    ) -> None:
        self._graph_store = graph_store
        self._evidence_store = evidence_store
        self._case_store = case_store
        self._actor = actor
        self._principals = frozenset({actor}) if principals is None else principals
        if (
            type(self._principals) is not frozenset
            or actor not in self._principals
            or any(type(principal) is not str or not principal for principal in self._principals)
        ):
            raise ValueError("invalid resolution principals")
        self._transactions = transactions or LocalTransactionCoordinator(
            graph_store._history_store._file.sibling(".local-transaction.json"),
            {
                "graph": graph_store._file,
                "history": graph_store._history_store._file,
                "cases": case_store._file,
            },
        )
        self._executor = LocalChangeSetExecutor(
            graph_store,
            case_store,
            self._transactions,
        )

    def resolve(
        self,
        case_id: str,
        action: ResolutionAction,
        *,
        approve: str | None = None,
        at: datetime | None = None,
    ) -> tuple[ReconciliationCase, ChangeSet | None, str | None]:
        """Prevalidate all state, then atomically commit every canonical effect."""
        try:
            case = self._case_store.get(case_id)
            graph = self._graph_store.load()
            records = tuple(
                self._evidence_store.get(reference) for reference in case.all_evidence_refs
            )
            if not refs_allowed(case.all_evidence_refs, records, self._principals):
                raise ResolutionUnavailable("resolution unavailable")
            timestamp = at or datetime.now(UTC)
            if case.status is ReconciliationStatus.OPEN and action in {
                ResolutionAction.DEFER,
                ResolutionAction.MARK_FALSE_POSITIVE,
            }:
                target = (
                    ReconciliationStatus.DEFERRED
                    if action is ResolutionAction.DEFER
                    else ReconciliationStatus.FALSE_POSITIVE
                )
                updated = transition_case(case, target, self._actor, timestamp)
                self._append_case_versions((updated,))
                return updated, None, None
            if case.status is ReconciliationStatus.OPEN:
                proposed = transition_case(
                    case,
                    ReconciliationStatus.PROPOSED,
                    self._actor,
                    timestamp,
                )
                reviewed = transition_case(
                    proposed,
                    ReconciliationStatus.NEEDS_HUMAN,
                    self._actor,
                    timestamp,
                )
                changeset = self._canonical_changeset(reviewed, graph.version, action)
                self._append_case_versions((proposed, reviewed))
                return (
                    reviewed,
                    changeset,
                    self._approval_hash(reviewed, graph.version, action, changeset),
                )
            if action in {ResolutionAction.DEFER, ResolutionAction.MARK_FALSE_POSITIVE}:
                raise ResolutionUnavailable("resolution unavailable")
            if case.status is not ReconciliationStatus.NEEDS_HUMAN or approve is None:
                raise ResolutionUnavailable("resolution unavailable")
            changeset = self._canonical_changeset(case, graph.version, action)
            expected = self._approval_hash(case, graph.version, action, changeset)
            if approve != expected:
                raise ResolutionUnavailable("resolution unavailable")
            resolved = transition_case(
                case,
                ReconciliationStatus.RESOLVED,
                self._actor,
                timestamp,
                action,
                changeset.id,
            )
            self._executor.apply(changeset, resolved_cases=(resolved,))
            return resolved, changeset, None
        except ResolutionUnavailable:
            raise
        except Exception as error:
            raise ResolutionUnavailable("resolution unavailable") from error

    def _append_case_versions(self, cases: Sequence[ReconciliationCase]) -> None:
        with self._transactions.transaction() as transaction:
            serialized = validate_case_appends(
                transaction.read_optional("cases"),
                cases,
            )
            transaction.append("cases", serialized)

    @staticmethod
    def _approval_hash(
        case: ReconciliationCase,
        graph_version: int,
        action: ResolutionAction,
        changeset: ChangeSet,
    ) -> str:
        payload = {
            "action": action.value,
            "case": case.model_dump(mode="json"),
            "changeset": changeset.model_dump(mode="json"),
            "graph_version": graph_version,
        }
        return sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _canonical_changeset(
        self,
        case: ReconciliationCase,
        graph_version: int,
        action: ResolutionAction,
    ) -> ChangeSet:
        return self._changeset(case, graph_version, action, case.history[-1].at)

    def recover(self) -> None:
        """Recover raw preimages without exposing paths or content in failures."""
        try:
            self._transactions.recover()
        except TransactionRecoveryError as error:
            raise ResolutionUnavailable("resolution unavailable") from error

    def _paths(self) -> tuple[Path, ...]:
        """Return path labels for compatibility with local diagnostics and tests."""
        return (
            self._graph_store.path,
            self._graph_store._history_store.path,
            self._case_store.path,
        )

    def _journal_path(self) -> Path:
        """Return the journal's diagnostic label; persistence remains descriptor-rooted."""
        return self._transactions.journal_path

    def _changeset(
        self,
        case: ReconciliationCase,
        version: int,
        action: ResolutionAction,
        timestamp: datetime,
    ) -> ChangeSet:
        material = f"{case.id}\x00{version}\x00{action.value}\x00" + "\x00".join(
            case.all_evidence_refs
        )
        return ChangeSet(
            id=f"changeset:resolve:{sha256(material.encode('utf-8')).hexdigest()}",
            actor=self._actor,
            timestamp=timestamp,
            baseline_graph_version=version,
            evidence_refs=case.all_evidence_refs,
            nodes_added=(),
            nodes_updated=(),
            nodes_superseded=(),
            edges_added=(),
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(case.id,),
            validation_status="validated",
        )
