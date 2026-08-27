"""Evidence-grounded PRD bootstrap proposals and governed baseline activation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from intent_engineering.core.graph.applier import apply_changeset
from intent_engineering.core.models import (
    ChangeSet,
    Edge,
    EvidenceRecord,
    Graph,
    Node,
    ProjectConfig,
    RelationType,
    SourceMode,
    SourceRoleAssignment,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.intent_workflow.models import (
    IntentProposal,
    ProposalDecisionRecord,
    ProposalDecisionV2,
    ProposalKind,
)
from intent_engineering.intent_workflow.proposal_store import (
    IntentLedgerRecord,
    IntentProposalStore,
)
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.evidence_store import (
    JsonlEvidenceStore,
    parse_evidence_lines,
)
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import YamlGraphStore, parse_graph

_MAX_CANDIDATES = 10_000
_MAX_REFERENCES = 10_000
_MAX_REVIEW_ITEMS = 256


class BootstrapError(ValueError):
    """One fixed, context-free bootstrap failure."""

    def __init__(self) -> None:
        super().__init__("intent bootstrap unavailable")


class _BootstrapSnapshotChanged(ValueError):
    """Private retry signal for an exact optimistic-preimage mismatch."""


class _BootstrapModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


def _require_utc(value: datetime) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must use UTC")
    return value.astimezone(UTC)


class BootstrapSubmission(_BootstrapModel):
    """Typed assertions supplied by the active agent over captured evidence."""

    schema_version: Literal[1] = 1
    baseline_graph_version: Annotated[int, Field(ge=0)]
    actor: Annotated[str, Field(min_length=1, max_length=256)]
    timestamp: datetime
    evidence_refs: Annotated[tuple[str, ...], Field(max_length=_MAX_REFERENCES)]
    source_roles: Annotated[
        tuple[SourceRoleAssignment, ...], Field(max_length=_MAX_REFERENCES)
    ]
    candidate_nodes: Annotated[tuple[Node, ...], Field(max_length=_MAX_CANDIDATES)]
    candidate_edges: Annotated[tuple[Edge, ...], Field(max_length=_MAX_CANDIDATES)]
    core_node_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_CANDIDATES)]
    provisional_node_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_CANDIDATES)]
    assumptions: Annotated[tuple[str, ...], Field(max_length=_MAX_REVIEW_ITEMS)] = ()
    unanswered_questions: Annotated[
        tuple[str, ...], Field(max_length=_MAX_REVIEW_ITEMS)
    ] = ()
    conflicting_authors: Annotated[
        tuple[str, ...], Field(max_length=_MAX_REVIEW_ITEMS)
    ] = ()
    destructive: bool = False

    @field_validator("timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator(
        "evidence_refs",
        "core_node_ids",
        "provisional_node_ids",
        "conflicting_authors",
    )
    @classmethod
    def require_unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("bootstrap identifiers must be unique")
        return values

    @field_validator("source_roles")
    @classmethod
    def normalize_source_roles(
        cls, source_roles: tuple[SourceRoleAssignment, ...]
    ) -> tuple[SourceRoleAssignment, ...]:
        normalized = tuple(sorted(source_roles, key=lambda item: (item.connector_id, item.scope)))
        pairs = tuple((item.connector_id, item.scope) for item in normalized)
        if len(pairs) != len(set(pairs)):
            raise ValueError("duplicate source role assignment")
        return normalized


class BootstrapReview(_BootstrapModel):
    """Detached hybrid review of core and provisional proposal nodes."""

    schema_version: Literal[1] = 1
    status: Literal["proposed"] = "proposed"
    proposal_id: str
    proposal_digest: str
    baseline_graph_version: int
    core_nodes: tuple[Node, ...]
    provisional_nodes: tuple[Node, ...]
    all_nodes: tuple[Node, ...]
    candidate_edges: tuple[Edge, ...]
    candidate_changeset: ChangeSet
    assumptions: tuple[str, ...]
    unanswered_questions: tuple[str, ...]

    @model_validator(mode="after")
    def validate_projection(self) -> BootstrapReview:
        if self.all_nodes != (*self.core_nodes, *self.provisional_nodes):
            raise ValueError("invalid bootstrap review projection")
        if (
            {node.id for node in self.all_nodes}
            != {node.id for node in self.candidate_changeset.nodes_added}
            or self.candidate_edges != self.candidate_changeset.edges_added
        ):
            raise ValueError("invalid bootstrap review mutation")
        return self


def _canonical_digest(material: object) -> str:
    encoded = json.dumps(
        material,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _changeset_id(prefix: str, changeset: ChangeSet) -> str:
    material = changeset.model_dump(mode="json", exclude={"id"})
    return f"changeset:{prefix}:{_canonical_digest(material)}"


def _proposal_id(values: dict[str, object]) -> str:
    return f"proposal:{_canonical_digest(values)}"


def _decision_id(values: dict[str, object]) -> str:
    return f"proposal-decision:{_canonical_digest(values)}"


def _review_for(proposal: IntentProposal) -> BootstrapReview:
    by_id = {node.id: node for node in proposal.changeset.nodes_added}
    core = tuple(by_id[node_id] for node_id in proposal.core_node_ids)
    provisional = tuple(by_id[node_id] for node_id in proposal.provisional_node_ids)
    review = BootstrapReview(
        proposal_id=proposal.id,
        proposal_digest=proposal.digest,
        baseline_graph_version=proposal.baseline_graph_version,
        core_nodes=core,
        provisional_nodes=provisional,
        all_nodes=(*core, *provisional),
        candidate_edges=proposal.changeset.edges_added,
        candidate_changeset=proposal.changeset,
        assumptions=proposal.assumptions,
        unanswered_questions=proposal.unanswered_questions,
    )
    return BootstrapReview.model_validate_json(review.model_dump_json())


def _fixed_failure() -> None:
    raise BootstrapError() from None


def _raise_signal(signal: BaseException) -> None:
    raise signal.with_traceback(None)


class BootstrapService:
    """Validate agent candidates, hold proposals, and atomically activate reviewed cores."""

    def __init__(
        self,
        *,
        graph_store: YamlGraphStore,
        evidence_store: JsonlEvidenceStore,
        proposal_store: IntentProposalStore,
        changeset_executor: LocalChangeSetExecutor,
        transactions: LocalTransactionCoordinator,
        config: ProjectConfig,
    ) -> None:
        required = {"graph", "history", "cases", "evidence", "intent_proposals"}
        if not required.issubset(transactions.target_names):
            raise ValueError("bootstrap requires shared transaction targets")
        self._graph_store = graph_store
        self._evidence_store = evidence_store
        self._store = proposal_store
        self._executor = changeset_executor
        self._transactions = transactions
        self._config = ProjectConfig.model_validate_json(config.model_dump_json())

    def _snapshot(
        self,
    ) -> tuple[
        Graph,
        tuple[EvidenceRecord, ...],
        dict[str, frozenset[str]],
        bytes,
        bytes | None,
    ]:
        snapshot = self._transactions.snapshot()
        graph_content = snapshot.content.get("graph")
        if graph_content is None:
            raise ValueError("missing graph")
        graph = parse_graph(graph_content)
        records, ingestions, _ = parse_evidence_lines(snapshot.content.get("evidence"))
        connectors: dict[str, set[str]] = {}
        for ingestion in ingestions:
            connectors.setdefault(ingestion.evidence.id, set()).add(ingestion.connector_id)
        return (
            graph,
            records,
            {
                evidence_id: frozenset(connector_ids)
                for evidence_id, connector_ids in connectors.items()
            },
            graph_content,
            snapshot.content.get("evidence"),
        )

    @staticmethod
    def _validate_principals(principals: frozenset[str]) -> None:
        if type(principals) is not frozenset or not principals or any(
            type(principal) is not str or not principal for principal in principals
        ):
            raise ValueError("invalid principals")

    def _configured_role(
        self,
        connector_id: str,
        locator: str,
    ) -> SourceRoleAssignment | None:
        assignments = tuple(
            assignment
            for assignment in self._config.source_roles
            if assignment.connector_id == connector_id
        )
        exact = tuple(assignment for assignment in assignments if assignment.scope == locator)
        if exact:
            return exact[0]
        inherited = tuple(
            assignment
            for assignment in assignments
            if assignment.inherited
            and locator.startswith(f"{assignment.scope.rstrip('/')}/")
        )
        return max(inherited, key=lambda assignment: len(assignment.scope), default=None)

    def _validate_submission(
        self,
        submission: BootstrapSubmission,
        graph: Graph,
        evidence: Sequence[EvidenceRecord],
        connectors: dict[str, frozenset[str]],
        principals: frozenset[str],
    ) -> ChangeSet:
        if submission.baseline_graph_version != graph.version:
            raise ValueError("stale baseline")
        if not submission.evidence_refs or not refs_allowed(
            submission.evidence_refs, evidence, principals
        ):
            raise ValueError("unavailable evidence")
        configured_roles = set(self._config.source_roles)
        submitted_roles = set(submission.source_roles)
        if not submitted_roles or not submitted_roles.issubset(configured_roles):
            raise ValueError("source role mismatch")
        evidence_by_id = {record.id: record for record in evidence}
        used_roles: set[SourceRoleAssignment] = set()
        for evidence_id in submission.evidence_refs:
            record = evidence_by_id[evidence_id]
            matched = tuple(
                role
                for connector_id in connectors.get(evidence_id, frozenset())
                if (role := self._configured_role(connector_id, record.source_locator))
                is not None
                and role in submitted_roles
            )
            if not matched:
                raise ValueError("evidence source role mismatch")
            used_roles.update(matched)
        if used_roles != submitted_roles:
            raise ValueError("unused source role assignment")

        candidate_ids = tuple(node.id for node in submission.candidate_nodes)
        edge_ids = tuple(edge.id for edge in submission.candidate_edges)
        if len(candidate_ids) != len(set(candidate_ids)) or len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate semantic identity")
        core_ids = set(submission.core_node_ids)
        provisional_ids = set(submission.provisional_node_ids)
        if (
            not core_ids
            or core_ids & provisional_ids
            or core_ids | provisional_ids != set(candidate_ids)
        ):
            raise ValueError("invalid bootstrap classification")
        allowed_evidence = set(submission.evidence_refs)
        known_nodes = {node.id for node in graph.nodes} | set(candidate_ids)
        for node in submission.candidate_nodes:
            graph.type_registry.assert_registered(node.type)
            node_evidence_refs = node.evidence_refs
            if (
                not node.id
                or not node.label
                or node.status != "proposed"
                or node.source_mode is not SourceMode.INFERRED
                or not node_evidence_refs
                or len(node_evidence_refs) > _MAX_REFERENCES
                or len(node_evidence_refs) != len(set(node_evidence_refs))
                or not set(node_evidence_refs).issubset(allowed_evidence)
                or node.intent_fidelity_confidence is None
                or not node.confidence_basis
                or node.last_reassessed_at is None
                or node.created_by != submission.actor
                or node.last_modified_by != submission.actor
                or node.created_at != submission.timestamp
                or node.last_modified_at != submission.timestamp
            ):
                raise ValueError("invalid candidate provenance")
            _require_utc(node.created_at)
            _require_utc(node.last_modified_at)
            _require_utc(node.last_reassessed_at)
        for edge in submission.candidate_edges:
            if (
                not edge.id
                or edge.status != "proposed"
                or edge.from_id not in known_nodes
                or edge.to_id not in known_nodes
                or edge.created_by != submission.actor
                or edge.last_modified_by != submission.actor
                or edge.created_at != submission.timestamp
                or edge.last_modified_at != submission.timestamp
            ):
                raise ValueError("invalid candidate edge")
            _require_utc(edge.created_at)
            _require_utc(edge.last_modified_at)

        changeset = ChangeSet(
            id="",
            actor=submission.actor,
            timestamp=submission.timestamp,
            baseline_graph_version=submission.baseline_graph_version,
            evidence_refs=submission.evidence_refs,
            nodes_added=submission.candidate_nodes,
            nodes_updated=(),
            nodes_superseded=(),
            edges_added=submission.candidate_edges,
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(),
            validation_status="validated",
        )
        changeset = changeset.model_copy(
            update={"id": _changeset_id("bootstrap-proposal", changeset)}
        )
        apply_changeset(graph, changeset)
        return changeset

    @staticmethod
    def _proposal(submission: BootstrapSubmission, changeset: ChangeSet) -> IntentProposal:
        material = {
            "schema_version": 1,
            "kind": ProposalKind.BOOTSTRAP.value,
            "proposed_by": submission.actor,
            "proposed_at": submission.timestamp.isoformat().replace("+00:00", "Z"),
            "baseline_graph_version": submission.baseline_graph_version,
            "evidence_refs": list(submission.evidence_refs),
            "source_roles": [item.model_dump(mode="json") for item in submission.source_roles],
            "changeset": changeset.model_dump(mode="json"),
            "core_node_ids": list(submission.core_node_ids),
            "provisional_node_ids": list(submission.provisional_node_ids),
            "assumptions": list(submission.assumptions),
            "unanswered_questions": list(submission.unanswered_questions),
            "conflicting_authors": list(submission.conflicting_authors),
            "destructive": submission.destructive,
        }
        return IntentProposal(
            id=_proposal_id(material),
            kind=ProposalKind.BOOTSTRAP,
            proposed_by=submission.actor,
            proposed_at=submission.timestamp,
            baseline_graph_version=submission.baseline_graph_version,
            evidence_refs=submission.evidence_refs,
            source_roles=submission.source_roles,
            changeset=changeset,
            core_node_ids=submission.core_node_ids,
            provisional_node_ids=submission.provisional_node_ids,
            assumptions=submission.assumptions,
            unanswered_questions=submission.unanswered_questions,
            conflicting_authors=submission.conflicting_authors,
            destructive=submission.destructive,
        )

    def _propose(
        self,
        submission: BootstrapSubmission,
        principals: frozenset[str],
    ) -> BootstrapReview:
        if type(submission) is not BootstrapSubmission:
            raise ValueError("invalid bootstrap submission")
        self._validate_principals(principals)
        validated = BootstrapSubmission.model_validate_json(
            BootstrapSubmission.model_dump_json(submission)
        )
        graph, evidence, connectors, graph_content, evidence_content = self._snapshot()
        changeset = self._validate_submission(
            validated, graph, evidence, connectors, principals
        )
        proposal = self._proposal(validated, changeset)
        stored = {item.id: item for item in self._store.list()}.get(proposal.id)
        if stored is not None:
            if stored != proposal:
                raise ValueError("conflicting proposal")
            return _review_for(stored)
        ledger = self._store.bytes()
        record = IntentLedgerRecord(sequence=len(ledger.splitlines()), proposal=proposal)
        frame = (
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        try:
            with self._transactions.transaction(
                rollback_base_exceptions=True
            ) as transaction:
                if (
                    transaction.read("graph") != graph_content
                    or transaction.read_optional("evidence") != evidence_content
                    or transaction.read("intent_proposals") != ledger
                ):
                    raise _BootstrapSnapshotChanged("bootstrap snapshot changed")
                transaction.append("intent_proposals", frame)
        except _BootstrapSnapshotChanged:
            snapshot = self._transactions.snapshot()
            if (
                snapshot.content.get("graph") != graph_content
                or snapshot.content.get("evidence") != evidence_content
                or snapshot.content.get("intent_proposals") != ledger + frame
            ):
                raise
            stored = {item.id: item for item in self._store.list()}.get(proposal.id)
            if stored != proposal:
                raise
            return _review_for(stored)
        return _review_for(proposal)

    def propose(
        self,
        submission: BootstrapSubmission,
        principals: frozenset[str],
    ) -> BootstrapReview:
        result: BootstrapReview | None = None
        signal: BaseException | None = None
        failed = False
        try:
            result = self._propose(submission, principals)
        except Exception:  # noqa: BLE001 - expose one fixed public failure
            failed = True
        except BaseException as caught:  # noqa: BLE001 - cancellation must preserve identity
            self._transactions.recover()
            caught.__traceback__ = None
            signal = caught
        finally:
            submission = cast(BootstrapSubmission, None)
            principals = frozenset()
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        if failed or result is None:
            _fixed_failure()
        return cast(BootstrapReview, result)

    def _review(self, proposal_id: str, principals: frozenset[str]) -> BootstrapReview:
        proposal: IntentProposal | None = None
        evidence: tuple[EvidenceRecord, ...] = ()
        result: BootstrapReview | None = None
        signal: BaseException | None = None
        try:
            self._validate_principals(principals)
            proposal = self._store.get(proposal_id)
            if proposal.kind is not ProposalKind.BOOTSTRAP:
                raise ValueError("wrong proposal kind")
            _, evidence, _, _, _ = self._snapshot()
            if not refs_allowed(proposal.evidence_refs, evidence, principals):
                raise ValueError("unavailable evidence")
            result = _review_for(proposal)
        except BaseException as caught:  # noqa: BLE001 - scrub private cancellation frames
            caught.__traceback__ = None
            signal = caught
        finally:
            proposal_id = ""
            principals = frozenset()
            proposal = None
            evidence = ()
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        if result is None:
            raise ValueError("bootstrap review unavailable")
        return result

    def review(self, proposal_id: str, principals: frozenset[str]) -> BootstrapReview:
        result: BootstrapReview | None = None
        signal: BaseException | None = None
        failed = False
        try:
            result = self._review(proposal_id, principals)
        except Exception:  # noqa: BLE001 - expose one fixed public failure
            failed = True
        except BaseException as caught:  # noqa: BLE001 - cancellation must preserve identity
            caught.__traceback__ = None
            signal = caught
        finally:
            proposal_id = ""
            principals = frozenset()
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        if failed or result is None:
            _fixed_failure()
        return cast(BootstrapReview, result)

    @staticmethod
    def _activation_changeset(
        proposal: IntentProposal,
        confirmed_node_ids: tuple[str, ...],
        actor: str,
        at: datetime,
    ) -> ChangeSet:
        selected = set(confirmed_node_ids)
        nodes = tuple(
            node.model_copy(
                update={
                    "status": "active",
                    "last_modified_by": actor,
                    "last_modified_at": at,
                }
            )
            for node in proposal.changeset.nodes_added
            if node.id in selected
        )
        graph_before_ids = {
            edge.from_id
            for edge in proposal.changeset.edges_added
            if edge.from_id not in proposal.core_node_ids
            and edge.from_id not in proposal.provisional_node_ids
        } | {
            edge.to_id
            for edge in proposal.changeset.edges_added
            if edge.to_id not in proposal.core_node_ids
            and edge.to_id not in proposal.provisional_node_ids
        }
        allowed_endpoints = selected | graph_before_ids
        edges = tuple(
            edge.model_copy(
                update={
                    "status": "active",
                    "last_modified_by": actor,
                    "last_modified_at": at,
                }
            )
            for edge in proposal.changeset.edges_added
            if edge.from_id in allowed_endpoints
            and edge.to_id in allowed_endpoints
            and (edge.from_id in selected or edge.to_id in selected)
        )
        evidence_refs = tuple(
            dict.fromkeys(reference for node in nodes for reference in node.evidence_refs)
        )
        changeset = ChangeSet(
            id="",
            actor=actor,
            timestamp=at,
            baseline_graph_version=proposal.baseline_graph_version,
            evidence_refs=evidence_refs,
            nodes_added=nodes,
            nodes_updated=(),
            nodes_superseded=(),
            edges_added=edges,
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(),
            validation_status="validated",
        )
        return changeset.model_copy(
            update={"id": _changeset_id("bootstrap-activation", changeset)}
        )

    @staticmethod
    def _decision(
        proposal: IntentProposal,
        changeset: ChangeSet,
        actor: str,
        at: datetime,
    ) -> ProposalDecisionV2:
        confirmed_node_ids = tuple(node.id for node in changeset.nodes_added)
        material = {
            "schema_version": 2,
            "proposal_id": proposal.id,
            "proposal_digest": proposal.digest,
            "actor": actor,
            "actor_aliases": [actor],
            "decided_at": at.isoformat().replace("+00:00", "Z"),
            "action": "confirm",
            "baseline_graph_version": proposal.baseline_graph_version,
            "confirmed_node_ids": list(confirmed_node_ids),
            "activation_changeset_id": changeset.id,
        }
        return ProposalDecisionV2(
            id=_decision_id(material),
            proposal_id=proposal.id,
            proposal_digest=proposal.digest,
            actor=actor,
            actor_aliases=(actor,),
            decided_at=at,
            action="confirm",
            baseline_graph_version=proposal.baseline_graph_version,
            confirmed_node_ids=confirmed_node_ids,
            activation_changeset_id=changeset.id,
        )

    @staticmethod
    def _decision_frame(ledger: bytes, decision: ProposalDecisionRecord) -> bytes:
        record = IntentLedgerRecord(
            sequence=len(ledger.splitlines()),
            decision=decision,
        )
        return (
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    def _replay_result(
        self,
        proposal: IntentProposal,
        changeset: ChangeSet,
        decision: ProposalDecisionV2,
        existing: ProposalDecisionRecord,
    ) -> Graph:
        graph = self._graph_store.load()
        if (
            not isinstance(existing, ProposalDecisionV2)
            or existing != decision
            or existing.confirmed_node_ids
            != tuple(node.id for node in changeset.nodes_added)
            or existing.activation_changeset_id != changeset.id
            or graph.version != proposal.baseline_graph_version + 1
        ):
            raise ValueError("conflicting decision")
        history = self._graph_store.history(changeset.nodes_added[0].id)
        if changeset not in history:
            raise ValueError("activation history unavailable")
        expected = {node.id: node for node in changeset.nodes_added}
        current = {node.id: node for node in graph.nodes}
        expected_edges = {edge.id: edge for edge in changeset.edges_added}
        current_edges = {edge.id: edge for edge in graph.edges}
        if any(
            current.get(node_id) != node for node_id, node in expected.items()
        ) or any(
            current_edges.get(edge_id) != edge
            for edge_id, edge in expected_edges.items()
        ):
            raise ValueError("activation state unavailable")
        return Graph.model_validate_json(graph.model_dump_json())

    def _activate(
        self,
        proposal_id: str,
        confirmed_node_ids: tuple[str, ...],
        actor: str,
        at: datetime,
    ) -> Graph:
        if type(proposal_id) is not str or type(actor) is not str or type(at) is not datetime:
            raise ValueError("invalid activation input")
        at = _require_utc(at)
        if actor != self._config.local_actor:
            raise ValueError("unauthorized contributor")
        if (
            type(confirmed_node_ids) is not tuple
            or not confirmed_node_ids
            or any(type(node_id) is not str or not node_id for node_id in confirmed_node_ids)
            or len(confirmed_node_ids) != len(set(confirmed_node_ids))
        ):
            raise ValueError("invalid core selection")
        proposal = self._store.get(proposal_id)
        if (
            proposal.kind is not ProposalKind.BOOTSTRAP
            or proposal.destructive
            or proposal.conflicting_authors
            or any(
                edge.relation in {RelationType.CONTRADICTS, RelationType.SUPERSEDES}
                for edge in proposal.changeset.edges_added
            )
            or not set(confirmed_node_ids).issubset(set(proposal.core_node_ids))
            or at < proposal.proposed_at
        ):
            raise ValueError("proposal cannot be activated")
        _, evidence, _, _, _ = self._snapshot()
        if not refs_allowed(proposal.evidence_refs, evidence, frozenset({actor})):
            raise ValueError("contributor cannot access proposal evidence")
        changeset = self._activation_changeset(proposal, confirmed_node_ids, actor, at)
        decision = self._decision(proposal, changeset, actor, at)
        existing = self._store.decision_for(proposal.id)
        if existing is not None:
            return self._replay_result(proposal, changeset, decision, existing)
        if self._graph_store.load().version != proposal.baseline_graph_version:
            raise ValueError("stale activation")
        ledger = self._store.bytes()
        frame = self._decision_frame(ledger, decision)

        try:
            return self._executor.apply(
                changeset,
                intent_proposal_preimage=ledger,
                intent_proposal_append=frame,
                rollback_base_exceptions=True,
            )
        except Exception:
            existing = self._store.decision_for(proposal.id)
            if existing is None:
                raise
            return self._replay_result(proposal, changeset, decision, existing)

    def activate(
        self,
        proposal_id: str,
        *,
        confirmed_node_ids: tuple[str, ...],
        actor: str,
        at: datetime,
    ) -> Graph:
        result: Graph | None = None
        signal: BaseException | None = None
        failed = False
        try:
            result = self._activate(proposal_id, confirmed_node_ids, actor, at)
        except Exception:  # noqa: BLE001 - expose one fixed public failure
            failed = True
        except BaseException as caught:  # noqa: BLE001 - cancellation must preserve identity
            caught.__traceback__ = None
            signal = caught
        finally:
            proposal_id = ""
            confirmed_node_ids = ()
            actor = ""
            at = cast(datetime, None)
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        if failed or result is None:
            _fixed_failure()
        return cast(Graph, result)


__all__ = [
    "BootstrapError",
    "BootstrapReview",
    "BootstrapService",
    "BootstrapSubmission",
]
