"""Transaction-level execution of complete local ChangeSet semantics."""

from __future__ import annotations

from collections.abc import Sequence

from intent_engineering.core.graph.applier import apply_changeset_with_case_effects
from intent_engineering.core.models import (
    ChangeSet,
    Graph,
    ReconciliationCase,
    ReconciliationStatus,
)
from intent_engineering.storage.jsonl.case_store import (
    JsonlCaseStore,
    validate_case_appends,
)
from intent_engineering.storage.jsonl.history_store import serialize_changeset
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import (
    YamlGraphStore,
    parse_graph,
    serialize_graph,
)


class CaseEffectMismatch(ValueError):
    """Raised when supplied case versions do not exactly realize declared effects."""

    def __init__(self) -> None:
        super().__init__("case effects do not match ChangeSet")


class LocalChangeSetExecutor:
    """Commit graph, history, and reconciliation effects as one local transaction."""

    def __init__(
        self,
        graph_store: YamlGraphStore,
        case_store: JsonlCaseStore,
        transactions: LocalTransactionCoordinator,
    ) -> None:
        if not {"graph", "history", "cases"}.issubset(transactions.target_names):
            raise ValueError("local executor requires graph, history, and cases targets")
        self._graph_store = graph_store
        self._case_store = case_store
        self._transactions = transactions

    @staticmethod
    def _ordered_effects(
        changeset: ChangeSet,
        created_cases: Sequence[ReconciliationCase],
        resolved_cases: Sequence[ReconciliationCase],
    ) -> tuple[ReconciliationCase, ...]:
        created = {case.id: case for case in created_cases}
        resolved = {case.id: case for case in resolved_cases}
        if (
            len(created) != len(created_cases)
            or len(resolved) != len(resolved_cases)
            or tuple(created) != changeset.reconciliation_cases_created
            or tuple(resolved) != changeset.reconciliation_cases_resolved
        ):
            raise CaseEffectMismatch()
        if any(case.status is not ReconciliationStatus.OPEN for case in created.values()):
            raise CaseEffectMismatch()
        if any(
            case.status is not ReconciliationStatus.RESOLVED
            or case.resolved_by_changeset != changeset.id
            for case in resolved.values()
        ):
            raise CaseEffectMismatch()
        return (*created.values(), *resolved.values())

    def apply(
        self,
        changeset: ChangeSet,
        *,
        created_cases: Sequence[ReconciliationCase] = (),
        resolved_cases: Sequence[ReconciliationCase] = (),
    ) -> Graph:
        """Prevalidate all groups, then durably commit every declared local effect."""
        validated = ChangeSet.model_validate(changeset.model_dump())
        effects = self._ordered_effects(validated, created_cases, resolved_cases)
        with self._transactions.transaction() as transaction:
            graph = parse_graph(transaction.read("graph"))
            next_graph = apply_changeset_with_case_effects(graph, validated)
            known_nodes = {node.id for node in next_graph.nodes}
            if any(
                case.subject_ref not in known_nodes
                or any(reference not in known_nodes for reference in case.affected_refs)
                for case in effects
            ):
                raise CaseEffectMismatch()
            case_bytes = validate_case_appends(
                transaction.read_optional("cases"),
                effects,
            )
            transaction.write("graph", serialize_graph(next_graph))
            transaction.append("history", serialize_changeset(validated))
            if case_bytes:
                transaction.append("cases", case_bytes)
            return next_graph
