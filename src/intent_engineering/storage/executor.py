"""Transaction-level execution of complete local ChangeSet semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

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
from intent_engineering.storage.secure import SecureFile
from intent_engineering.storage.transaction import LocalTransaction, LocalTransactionCoordinator
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
        intent_proposal_preimage: bytes | None = None,
        intent_proposal_append: bytes | None = None,
        graph_preimage: bytes | None = None,
        evidence_preimage: bytes | None = None,
        case_preimage: bytes | None = None,
        bind_case_preimage: bool = False,
        rollback_base_exceptions: bool = False,
        read_only_extras: Mapping[str, SecureFile] | None = None,
        extra_preimages: Mapping[str, bytes | None] | None = None,
    ) -> Graph:
        """Prevalidate all groups, then durably commit every declared local effect."""
        if (intent_proposal_preimage is None) != (intent_proposal_append is None) or (
            intent_proposal_append is not None
            and (type(intent_proposal_append) is not bytes or not intent_proposal_append)
        ):
            raise ValueError("invalid intent proposal transaction effect")
        if intent_proposal_append is not None and "intent_proposals" not in self._transactions.target_names:
            raise ValueError("intent proposal transaction target is unavailable")
        if evidence_preimage is not None and "evidence" not in self._transactions.target_names:
            raise ValueError("evidence transaction target is unavailable")
        if case_preimage is not None and not bind_case_preimage:
            raise ValueError("case preimage requires an explicit binding")
        if (read_only_extras is None) != (extra_preimages is None) or (
            read_only_extras is not None and set(read_only_extras) != set(extra_preimages or {})
        ):
            raise ValueError("invalid read-only transaction binding")
        validated: ChangeSet | None = None
        effects: tuple[ReconciliationCase, ...] = ()
        graph: Graph | None = None
        next_graph: Graph | None = None
        known_nodes: set[str] = set()
        case_bytes = b""
        transaction: LocalTransaction | None = None
        cancellation: BaseException | None = None
        try:
            validated = ChangeSet.model_validate(changeset.model_dump())
            effects = self._ordered_effects(validated, created_cases, resolved_cases)
            with self._transactions.transaction(
                rollback_base_exceptions=rollback_base_exceptions,
                extras=read_only_extras,
            ) as transaction:
                if extra_preimages is not None and any(
                    transaction.read_optional(name) != content
                    for name, content in extra_preimages.items()
                ):
                    raise ValueError("read-only transaction binding changed")
                if graph_preimage is not None and transaction.read("graph") != graph_preimage:
                    raise ValueError("graph authorization changed")
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
                if intent_proposal_append is not None:
                    if transaction.read_optional("intent_proposals") != intent_proposal_preimage:
                        raise ValueError("intent proposal ledger changed")
                    transaction.append("intent_proposals", intent_proposal_append)
                if (
                    evidence_preimage is not None
                    and transaction.read_optional("evidence") != evidence_preimage
                ):
                    raise ValueError("evidence authorization changed")
                if bind_case_preimage and transaction.read_optional("cases") != case_preimage:
                    raise ValueError("case authorization changed")
                transaction.write("graph", serialize_graph(next_graph))
                transaction.append("history", serialize_changeset(validated))
                if case_bytes:
                    transaction.append("cases", case_bytes)
        except BaseException as caught:
            if isinstance(caught, Exception) or not rollback_base_exceptions:
                raise
            caught.__traceback__ = None
            cancellation = caught
            self._transactions.recover()
        finally:
            changeset = cast(ChangeSet, None)
            created_cases = ()
            resolved_cases = ()
            intent_proposal_preimage = None
            intent_proposal_append = None
            graph_preimage = None
            evidence_preimage = None
            case_preimage = None
            bind_case_preimage = False
            rollback_base_exceptions = False
            read_only_extras = None
            extra_preimages = None
            validated = None
            effects = ()
            graph = None
            known_nodes.clear()
            case_bytes = b""
            transaction = None
            if cancellation is not None:
                next_graph = None
        if cancellation is not None:
            caught_cancellation = cancellation
            cancellation = None
            raise caught_cancellation.with_traceback(None)
        if next_graph is None:
            raise RuntimeError("changeset execution failed")
        return next_graph
