"""Attributed clarification and governed local graph-proposal confirmation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Literal, cast

from pydantic import ConfigDict, Field

from intent_engineering.capture.mcp.profile_loader import (
    load_connector_config_bytes,
    load_strict_yaml_mapping_bytes,
)
from intent_engineering.cli.writes import MutationPolicy
from intent_engineering.core.graph.applier import (
    apply_changeset,
    apply_changeset_with_case_effects,
)
from intent_engineering.core.models import (
    ChangeSet,
    ClassificationEvent,
    EvidenceRecord,
    EvidenceSide,
    Graph,
    JsonValue,
    Node,
    ProjectConfig,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    RelationType,
    ResolutionAction,
    SourceMode,
    SourceRoleAssignment,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.models import (
    ClarificationAnswer,
    ClarificationConflict,
    ClarificationEvent,
    ClarificationIntentProposal,
    ClarificationProposalSubmission,
    ClarificationQuestion,
    ClarificationQuestionInput,
    ClarificationSession,
    IntentProposal,
    ProposalDecisionRecord,
    ProposalDecisionV3,
    ProposalKind,
    TaskEnvelope,
)
from intent_engineering.intent_workflow.proposal_store import (
    IntentLedgerRecord,
    IntentProposalStore,
    serialize_intent_ledger_record,
)
from intent_engineering.mutations.models import identity_aliases_for
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import (
    JsonlCaseStore,
    parse_case_versions,
    validate_case_appends,
)
from intent_engineering.storage.jsonl.evidence_store import (
    JsonlEvidenceStore,
    parse_evidence_lines,
)
from intent_engineering.storage.jsonl.history_store import serialize_changeset
from intent_engineering.storage.secure import SecureFile
from intent_engineering.storage.transaction import (
    LocalTransactionCoordinator,
    LocalTransactionSnapshot,
)
from intent_engineering.storage.yaml.graph_store import YamlGraphStore, parse_graph

_MAX_ANSWER_BYTES = 16 * 1024
_MAX_QUESTIONS = 16
_SERVICE_ACTOR = "intent:clarification"

type _EpistemicAssertion = tuple[
    str,
    SourceMode,
    float,
    tuple[str, ...],
    datetime,
    tuple[str, ...],
]


@dataclass(frozen=True)
class _AuthenticatedConfirmation:
    snapshot: LocalTransactionSnapshot
    config: ProjectConfig
    policy: MutationPolicy
    graph: Graph
    evidence: tuple[EvidenceRecord, ...]
    actor_aliases: tuple[str, ...]
    proposer_aliases: tuple[str, ...]
    conflict_aliases: tuple[str, ...]
    review_case: ReconciliationCase | None


def _required_node_epistemics(node: Node) -> tuple[SourceMode, float]:
    if node.source_mode is None or node.intent_fidelity_confidence is None:
        raise ValueError("semantic assertion lacks epistemic metadata")
    return node.source_mode, node.intent_fidelity_confidence


def _evidence_attribution(
    evidence_refs: tuple[str, ...],
    evidence_index: Mapping[str, EvidenceRecord],
) -> tuple[datetime, tuple[str, ...]]:
    records = tuple(evidence_index[reference] for reference in evidence_refs)
    authors = tuple(sorted({record.author for record in records if record.author is not None}))
    if not records or not authors:
        raise ValueError("semantic assertion evidence is not attributable")
    return max(record.observed_at for record in records), authors


def _group_epistemic_sides(
    assertions: tuple[_EpistemicAssertion, ...],
    *,
    label: str,
    current: bool,
) -> tuple[EvidenceSide, ...]:
    groups: dict[
        tuple[SourceMode, float, tuple[str, ...], datetime, tuple[str, ...]],
        list[str],
    ] = {}
    for subject, mode, confidence, refs, observed_at, authors in sorted(assertions):
        groups.setdefault((mode, confidence, refs, observed_at, authors), []).append(subject)
    multiple = len(groups) > 1
    sides: list[EvidenceSide] = []
    for (mode, confidence, refs, observed_at, authors), subjects in groups.items():
        subject_refs = tuple(subjects)
        sides.append(
            EvidenceSide(
                label=(label if not multiple else f"{label}:{','.join(subject_refs)}"),
                claim=(
                    (
                        "Current canonical graph assertions for: "
                        if current
                        else "Proposed semantic change supported by assertions for: "
                    )
                    + ", ".join(subject_refs)
                ),
                evidence_refs=refs,
                observed_at=observed_at,
                authors=authors,
                confidence=confidence,
                source_mode=mode,
                current=current,
            )
        )
    return tuple(sides)


class ClarificationError(ValueError):
    """Fixed context-free failure for clarification and local proposal governance."""

    def __init__(self) -> None:
        super().__init__("intent clarification unavailable")


class ProposalConfirmationStatus(StrEnum):
    APPLIED = "applied"
    REVIEW_REQUIRED = "review_required"


class ProposalConfirmationResult(StrictModel):
    """Bounded result of one local proposal confirmation attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    status: ProposalConfirmationStatus
    proposal_id: str
    graph_version: int = Field(ge=0)
    decision_id: str | None = None
    case_id: str | None = None


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _utc(value: datetime) -> datetime:
    offset = value.utcoffset() if type(value) is datetime and value.tzinfo is not None else None
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must use UTC")
    return value.astimezone(UTC)


def _session_id(material: Mapping[str, object]) -> str:
    return f"clarification:{_digest(material)}"


def _event(
    session: ClarificationSession,
    event_type: Literal["opened", "answered", "conflicted", "proposed", "closed"],
    actor: str,
    at: datetime,
    predecessor: str | None,
    proposal_id: str | None = None,
    decision_id: str | None = None,
    activation_changeset_id: str | None = None,
) -> ClarificationEvent:
    bare_session = session.model_copy(update={"latest_event_id": None})
    material = {
        "schema_version": 1,
        "event_type": event_type,
        "session": bare_session.model_dump(mode="json", exclude={"latest_event_id"}),
        "actor": actor,
        "at": at.isoformat().replace("+00:00", "Z"),
        "predecessor_event_id": predecessor,
        "proposal_id": proposal_id,
        "decision_id": decision_id,
        "activation_changeset_id": activation_changeset_id,
    }
    event_id = f"clarification-event:{_digest(material)}"
    bound = bare_session.model_copy(update={"latest_event_id": event_id})
    return ClarificationEvent(
        id=event_id,
        event_type=event_type,
        session=bound,
        actor=actor,
        at=at,
        predecessor_event_id=predecessor,
        proposal_id=proposal_id,
        decision_id=decision_id,
        activation_changeset_id=activation_changeset_id,
    )


def _proposal_id(material: Mapping[str, object]) -> str:
    return f"proposal:{_digest(material)}"


def _decision_id(material: Mapping[str, object]) -> str:
    return f"proposal-decision:{_digest(material)}"


def _review_case_fingerprint(proposal: IntentProposal, reasons: tuple[str, ...]) -> str:
    return _digest(
        {
            "proposal_id": proposal.id,
            "proposal_digest": proposal.digest,
            "baseline_graph_version": proposal.baseline_graph_version,
            "reasons": list(reasons),
        }
    ).removeprefix("sha256:")


def _changeset_id(prefix: str, changeset: ChangeSet) -> str:
    return f"changeset:{prefix}:{_digest(changeset.model_dump(mode='json', exclude={'id'}))}"


def _records(
    snapshot: LocalTransactionSnapshot,
) -> tuple[
    Graph,
    tuple[EvidenceRecord, ...],
    dict[str, str | None],
]:
    graph_bytes = snapshot.content.get("graph")
    if graph_bytes is None:
        raise ValueError("graph unavailable")
    graph = parse_graph(graph_bytes)
    evidence, ingestions, _ = parse_evidence_lines(snapshot.content.get("evidence"))
    predecessors = {item.evidence.id: item.predecessor_id for item in ingestions}
    return graph, evidence, predecessors


def _principals(value: object) -> frozenset[str]:
    if (
        type(value) is not frozenset
        or not value
        or any(type(item) is not str or not item for item in cast(frozenset[object], value))
    ):
        raise ValueError("invalid clarification principals")
    return cast(frozenset[str], value)


class ClarificationCoordinator:
    """Persist focused questions/answers and assemble one typed graph proposal."""

    def __init__(
        self,
        *,
        graph_store: YamlGraphStore,
        evidence_store: JsonlEvidenceStore,
        proposal_store: IntentProposalStore,
        transactions: LocalTransactionCoordinator,
        config: ProjectConfig,
        capture: ConversationCapture | None = None,
    ) -> None:
        required = {"graph", "evidence", "intent_proposals"}
        if not required.issubset(transactions.target_names):
            raise ValueError("clarification requires shared transaction targets")
        self._graph_store = graph_store
        self._evidence_store = evidence_store
        self._store = proposal_store
        self._transactions = transactions
        self._config = ProjectConfig.model_validate_json(config.model_dump_json())
        self._capture = capture or ConversationCapture(evidence_store)

    @staticmethod
    def _evidence_index(
        evidence: Sequence[EvidenceRecord],
    ) -> dict[str, EvidenceRecord]:
        return {record.id: record for record in evidence}

    def _configured_role(
        self,
        connector_id: str,
        locator: str,
    ) -> SourceRoleAssignment | None:
        assignments = tuple(
            item for item in self._config.source_roles if item.connector_id == connector_id
        )
        exact = tuple(item for item in assignments if item.scope == locator)
        if exact:
            return exact[0]
        inherited = tuple(
            item
            for item in assignments
            if item.inherited and locator.startswith(f"{item.scope.rstrip('/')}/")
        )
        return max(inherited, key=lambda item: len(item.scope), default=None)

    def _validate_source_roles(
        self,
        submission: ClarificationProposalSubmission,
        evidence: tuple[EvidenceRecord, ...],
        snapshot: LocalTransactionSnapshot,
    ) -> None:
        configured = set(self._config.source_roles)
        submitted = set(submission.source_roles)
        if not submitted.issubset(configured):
            raise ValueError("unconfigured clarification source role")
        _, ingestions, _ = parse_evidence_lines(snapshot.content.get("evidence"))
        connector_ids: dict[str, set[str]] = {}
        for ingestion in ingestions:
            connector_ids.setdefault(ingestion.evidence.id, set()).add(ingestion.connector_id)
        used: set[SourceRoleAssignment] = set()
        for record in evidence:
            if record.id not in submission.evidence_refs:
                continue
            for connector_id in connector_ids.get(record.id, set()):
                role = self._configured_role(connector_id, record.source_locator)
                if role is not None and role in submitted:
                    used.add(role)
        if used != submitted:
            raise ValueError("unused clarification source role")

    @staticmethod
    def _validate_candidate_provenance(
        submission: ClarificationProposalSubmission,
        graph: Graph,
    ) -> None:
        changeset = submission.changeset
        allowed = set(submission.evidence_refs)
        current_nodes = {node.id: node for node in graph.nodes}
        current_edges = {edge.id: edge for edge in graph.edges}
        if not any(
            (
                changeset.nodes_added,
                changeset.nodes_updated,
                changeset.nodes_superseded,
                changeset.edges_added,
                changeset.edges_updated,
                changeset.edges_superseded,
                changeset.confidence_changes,
                changeset.implementation_status_changes,
            )
        ):
            raise ValueError("empty clarification proposal")
        for node in changeset.nodes_added:
            if (
                node.status != "proposed"
                or node.source_mode is not SourceMode.INFERRED
                or node.created_by != submission.actor
                or node.last_modified_by != submission.actor
                or node.created_at != submission.timestamp
                or node.last_modified_at != submission.timestamp
                or not node.evidence_refs
                or not set(node.evidence_refs).issubset(allowed)
            ):
                raise ValueError("invalid clarification node provenance")
        for node_update in changeset.nodes_updated:
            current_node = current_nodes.get(node_update.node_id)
            replacement_node = node_update.replacement
            if (
                current_node is None
                or replacement_node.created_by != current_node.created_by
                or replacement_node.created_at != current_node.created_at
                or replacement_node.source_mode != current_node.source_mode
                or replacement_node.intent_fidelity_confidence
                != current_node.intent_fidelity_confidence
                or replacement_node.confidence_basis != current_node.confidence_basis
                or replacement_node.last_reassessed_at != current_node.last_reassessed_at
                or replacement_node.implementation_status != current_node.implementation_status
                or replacement_node.last_modified_by != submission.actor
                or replacement_node.last_modified_at != submission.timestamp
                or not set(current_node.evidence_refs).issubset(replacement_node.evidence_refs)
                or not allowed.issubset(replacement_node.evidence_refs)
            ):
                raise ValueError("invalid clarification node update provenance")
        for edge in changeset.edges_added:
            if (
                edge.status != "proposed"
                or edge.created_by != submission.actor
                or edge.last_modified_by != submission.actor
                or edge.created_at != submission.timestamp
                or edge.last_modified_at != submission.timestamp
            ):
                raise ValueError("invalid clarification edge provenance")
        for edge_update in changeset.edges_updated:
            current_edge = current_edges.get(edge_update.edge_id)
            replacement_edge = edge_update.replacement
            if (
                current_edge is None
                or replacement_edge.created_by != current_edge.created_by
                or replacement_edge.created_at != current_edge.created_at
                or replacement_edge.last_modified_by != submission.actor
                or replacement_edge.last_modified_at != submission.timestamp
            ):
                raise ValueError("invalid clarification edge update provenance")
        if any(
            item.actor != submission.actor
            or item.timestamp != submission.timestamp
            or not set(item.evidence_refs).issubset(allowed)
            for item in changeset.confidence_changes
        ) or any(
            not set(item.evidence_refs).issubset(allowed)
            for item in changeset.implementation_status_changes
        ):
            raise ValueError("invalid clarification change provenance")

    def _open(
        self,
        task: TaskEnvelope,
        *,
        classification_evidence_ref: str,
        questions: tuple[ClarificationQuestionInput, ...],
        opened_by: str,
        opened_at: datetime,
        principals: frozenset[str],
    ) -> ClarificationSession:
        if type(task) is not TaskEnvelope:
            raise ValueError("invalid task")
        task = TaskEnvelope.model_validate_json(task.model_dump_json())
        opened_at = _utc(opened_at)
        principals = _principals(principals)
        if (
            type(questions) is not tuple
            or not 1 <= len(questions) <= _MAX_QUESTIONS
            or any(type(item) is not ClarificationQuestionInput for item in questions)
            or len({item.id for item in questions}) != len(questions)
            or opened_by not in principals
            or task.actor not in principals
            or task.repository_id != self._config.project_id
            or opened_at < task.created_at
        ):
            raise ValueError("invalid clarification opening")
        snapshot = self._transactions.snapshot()
        graph, evidence, predecessors = _records(snapshot)
        index = self._evidence_index(evidence)
        request = index.get(task.request_evidence_ref)
        classification = index.get(classification_evidence_ref)
        if (
            graph.version != task.graph_version
            or request is None
            or classification is None
            or request.author != task.actor
            or request.payload.get("role") != "human"
            or classification.payload.get("role") != "agent"
            or not refs_allowed(
                (task.request_evidence_ref, classification_evidence_ref), evidence, principals
            )
            or predecessors.get(classification_evidence_ref) != task.request_evidence_ref
        ):
            raise ValueError("unavailable clarification evidence")
        captured: list[EvidenceRecord] = []
        for offset, question in enumerate(questions, start=1):
            captured.append(
                self._capture.record_turn(
                    conversation_ref=task.conversation_ref,
                    role="agent",
                    author=opened_by,
                    content=question.prompt,
                    captured_at=opened_at + timedelta(microseconds=offset),
                    acl=tuple(sorted(principals)),
                )
            )
        fresh = self._transactions.snapshot()
        fresh_graph, fresh_evidence, fresh_predecessors = _records(fresh)
        if fresh_graph != graph or any(
            record.id not in self._evidence_index(fresh_evidence) for record in captured
        ):
            raise ValueError("clarification evidence changed")
        durable_questions = tuple(
            ClarificationQuestion(
                id=question.id,
                prompt_digest=_digest(question.prompt),
                evidence_ref=record.id,
                author=opened_by,
                asked_at=record.observed_at,
                predecessor_evidence_ref=fresh_predecessors.get(record.id),
                required=question.required,
            )
            for question, record in zip(questions, captured, strict=True)
        )
        if durable_questions[0].predecessor_evidence_ref != classification_evidence_ref or any(
            current.predecessor_evidence_ref != previous.evidence_ref
            for previous, current in pairwise(durable_questions)
        ):
            raise ValueError("clarification evidence chronology changed")
        session_material = {
            "schema_version": 1,
            "task_id": task.id,
            "conversation_ref": task.conversation_ref,
            "request_evidence_ref": task.request_evidence_ref,
            "classification_evidence_ref": classification_evidence_ref,
            "opened_by": opened_by,
            "opened_at": opened_at.isoformat().replace("+00:00", "Z"),
            "baseline_graph_version": graph.version,
            "questions": [item.model_dump(mode="json") for item in durable_questions],
        }
        session = ClarificationSession(
            id=_session_id(session_material),
            task_id=task.id,
            conversation_ref=task.conversation_ref,
            request_evidence_ref=task.request_evidence_ref,
            classification_evidence_ref=classification_evidence_ref,
            opened_by=opened_by,
            opened_at=opened_at,
            baseline_graph_version=graph.version,
            questions=durable_questions,
        )
        opened = _event(session, "opened", opened_by, opened_at, None)
        self._store.append_clarification(opened)
        return self._store.session(session.id)

    def open(self, task: TaskEnvelope, **kwargs: object) -> ClarificationSession:
        result: ClarificationSession | None = None
        signal: BaseException | None = None
        failed = False
        try:
            result = self._open(task, **kwargs)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - fixed opaque boundary; no logging
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve exact cancellation identity
            caught.__traceback__ = None
            signal = caught
        finally:
            task = cast(TaskEnvelope, None)
            kwargs.clear()
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if failed or result is None:
            raise ClarificationError()
        return result

    def _answer(
        self,
        session_id: str,
        *,
        actor: str,
        question_id: str,
        answer: str,
        answered_at: datetime,
        acl: tuple[str, ...],
        principals: frozenset[str],
    ) -> ClarificationSession:
        answered_at = _utc(answered_at)
        principals = _principals(principals)
        if (
            type(answer) is not str
            or not answer
            or len(answer.encode("utf-8")) > _MAX_ANSWER_BYTES
            or actor not in principals
            or tuple(sorted(principals)) != tuple(sorted(acl))
        ):
            raise ValueError("invalid clarification answer")
        session = self._store.session(session_id)
        question = next((item for item in session.questions if item.id == question_id), None)
        if question is None or session.status != "open":
            raise ValueError("clarification question unavailable")
        previous = next((item for item in session.answers if item.question_id == question_id), None)
        latest_at = (
            session.conflicts[-1].observed_at
            if session.conflicts
            else session.answers[-1].answered_at
            if session.answers
            else session.questions[-1].asked_at
        )
        if answered_at < latest_at:
            raise ValueError("clarification answer chronology changed")
        record = self._capture.record_turn(
            conversation_ref=session.conversation_ref,
            role="human",
            author=actor,
            content=answer,
            captured_at=answered_at,
            acl=acl,
        )
        snapshot = self._transactions.snapshot()
        graph, evidence, predecessors = _records(snapshot)
        if (
            graph.version != session.baseline_graph_version
            or not refs_allowed((record.id,), evidence, principals)
            or record.author != actor
        ):
            raise ValueError("unavailable clarification answer")
        answer_record = ClarificationAnswer(
            question_id=question_id,
            actor=actor,
            answered_at=answered_at,
            evidence_ref=record.id,
            answer_digest=_digest(answer),
            predecessor_evidence_ref=predecessors.get(record.id),
        )
        if previous is not None:
            if previous == answer_record:
                return session
            conflict = ClarificationConflict(
                question_id=question_id,
                actor=actor,
                observed_at=answered_at,
                original_evidence_ref=previous.evidence_ref,
                conflicting_evidence_ref=record.id,
                conflicting_answer_digest=_digest(answer),
                predecessor_evidence_ref=predecessors.get(record.id) or "",
            )
            existing_conflict = next(
                (
                    item
                    for item in session.conflicts
                    if item.conflicting_evidence_ref == conflict.conflicting_evidence_ref
                ),
                None,
            )
            if existing_conflict is not None:
                if existing_conflict == conflict:
                    return session
                raise ValueError("clarification conflict replay changed")
            expected_conflict_predecessor = (
                session.conflicts[-1].conflicting_evidence_ref
                if session.conflicts
                else session.answers[-1].evidence_ref
            )
            if conflict.predecessor_evidence_ref != expected_conflict_predecessor:
                raise ValueError("clarification conflict chronology changed")
            updated = session.model_copy(update={"conflicts": (*session.conflicts, conflict)})
            event = _event(updated, "conflicted", actor, answered_at, session.latest_event_id)
            self._store.append_clarification(event)
            return self._store.session(session_id)
        expected_predecessor = (
            session.conflicts[-1].conflicting_evidence_ref
            if session.conflicts
            else session.answers[-1].evidence_ref
            if session.answers
            else session.questions[-1].evidence_ref
        )
        if answer_record.predecessor_evidence_ref != expected_predecessor:
            raise ValueError("clarification answer chronology changed")
        updated = session.model_copy(update={"answers": (*session.answers, answer_record)})
        event = _event(updated, "answered", actor, answered_at, session.latest_event_id)
        self._store.append_clarification(event)
        return self._store.session(session_id)

    def answer(self, session_id: str, **kwargs: object) -> ClarificationSession:
        result: ClarificationSession | None = None
        signal: BaseException | None = None
        failed = False
        try:
            result = self._answer(session_id, **kwargs)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - fixed opaque boundary; no logging
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve exact cancellation identity
            caught.__traceback__ = None
            signal = caught
        finally:
            session_id = ""
            kwargs.clear()
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if failed or result is None:
            raise ClarificationError()
        return result

    def _propose(
        self,
        submission: ClarificationProposalSubmission,
        *,
        principals: frozenset[str],
    ) -> ClarificationIntentProposal:
        if type(submission) is not ClarificationProposalSubmission:
            raise ValueError("invalid clarification proposal")
        submission = ClarificationProposalSubmission.model_validate_json(
            submission.model_dump_json()
        )
        principals = _principals(principals)
        session = self._store.session(submission.session_id)
        required = {item.id for item in session.questions if item.required}
        answered = {item.question_id for item in session.answers}
        if (
            session.status not in {"open", "proposed"}
            or bool(session.conflicts)
            or not required.issubset(answered)
            or submission.task_id != session.task_id
            or submission.baseline_graph_version != session.baseline_graph_version
            or not session.answers
            or submission.actor != session.answers[-1].actor
            or submission.actor not in principals
            or submission.timestamp < session.answers[-1].answered_at
        ):
            raise ValueError("incomplete clarification")
        snapshot = self._transactions.snapshot()
        graph, evidence, _ = _records(snapshot)
        expected_evidence = tuple(
            dict.fromkeys(
                (
                    session.request_evidence_ref,
                    session.classification_evidence_ref,
                    *(item.evidence_ref for item in session.questions),
                    *(item.evidence_ref for item in session.answers),
                )
            )
        )
        changeset = submission.changeset
        if (
            graph.version != session.baseline_graph_version
            or submission.evidence_refs != expected_evidence
            or not refs_allowed(expected_evidence, evidence, principals)
            or changeset.baseline_graph_version != graph.version
            or changeset.actor != submission.actor
            or changeset.timestamp != submission.timestamp
            or changeset.evidence_refs != expected_evidence
            or changeset.validation_status != "validated"
            or changeset.reconciliation_cases_created
            or changeset.reconciliation_cases_resolved
            or set(submission.core_node_ids) | set(submission.provisional_node_ids)
            != {node.id for node in changeset.nodes_added}
            or set(submission.core_node_ids) & set(submission.provisional_node_ids)
        ):
            raise ValueError("invalid clarification proposal binding")
        self._validate_source_roles(submission, evidence, snapshot)
        self._validate_candidate_provenance(submission, graph)
        normalized = changeset.model_copy(update={"id": ""})
        normalized = normalized.model_copy(
            update={"id": _changeset_id("clarification-proposal", normalized)}
        )
        apply_changeset(graph, normalized)
        material: dict[str, object] = {
            "schema_version": 2,
            "kind": ProposalKind.REQUIREMENT.value,
            "proposed_by": submission.actor,
            "proposed_at": submission.timestamp.isoformat().replace("+00:00", "Z"),
            "baseline_graph_version": graph.version,
            "evidence_refs": list(expected_evidence),
            "source_roles": [item.model_dump(mode="json") for item in submission.source_roles],
            "changeset": normalized.model_dump(mode="json"),
            "core_node_ids": list(submission.core_node_ids),
            "provisional_node_ids": list(submission.provisional_node_ids),
            "assumptions": list(submission.assumptions),
            "unanswered_questions": list(submission.unanswered_questions),
            "conflicting_authors": list(submission.conflicting_authors),
            "destructive": submission.destructive,
            "clarification_session_id": session.id,
            "task_id": session.task_id,
        }
        proposal = ClarificationIntentProposal(
            id=_proposal_id(material),
            kind=ProposalKind.REQUIREMENT,
            proposed_by=submission.actor,
            proposed_at=submission.timestamp,
            baseline_graph_version=graph.version,
            evidence_refs=expected_evidence,
            source_roles=submission.source_roles,
            changeset=normalized,
            core_node_ids=submission.core_node_ids,
            provisional_node_ids=submission.provisional_node_ids,
            assumptions=submission.assumptions,
            unanswered_questions=submission.unanswered_questions,
            conflicting_authors=submission.conflicting_authors,
            destructive=submission.destructive,
            clarification_session_id=session.id,
            task_id=session.task_id,
        )
        if session.status == "proposed":
            stored_replay = self._store.get(proposal.id)
            if stored_replay != proposal or not isinstance(
                stored_replay, ClarificationIntentProposal
            ):
                raise ValueError("conflicting clarification proposal replay")
            return stored_replay
        proposed_session = session.model_copy(update={"status": "proposed"})
        proposed_event = _event(
            proposed_session,
            "proposed",
            submission.actor,
            submission.timestamp,
            session.latest_event_id,
            proposal.id,
        )
        ledger = self._store.bytes()
        event_frame = serialize_intent_ledger_record(
            IntentLedgerRecord(sequence=len(ledger.splitlines()), clarification=proposed_event)
        )
        proposal_frame = serialize_intent_ledger_record(
            IntentLedgerRecord(
                sequence=len(ledger.splitlines()) + 1,
                proposal=proposal,
            )
        )
        try:
            with self._transactions.transaction(rollback_base_exceptions=True) as transaction:
                if (
                    transaction.read("graph") != snapshot.content.get("graph")
                    or transaction.read_optional("evidence") != snapshot.content.get("evidence")
                    or transaction.read("intent_proposals") != ledger
                ):
                    raise ValueError("clarification snapshot changed")
                transaction.append("intent_proposals", event_frame + proposal_frame)
        except Exception:
            durable = self._store.get(proposal.id)
            durable_session = self._store.session(session.id)
            if durable != proposal or durable_session.status != "proposed":
                raise
            return durable
        stored = self._store.get(proposal.id)
        if stored != proposal or not isinstance(stored, ClarificationIntentProposal):
            raise ValueError("clarification proposal unavailable")
        return stored

    def propose(
        self,
        submission: ClarificationProposalSubmission,
        *,
        principals: frozenset[str],
    ) -> ClarificationIntentProposal:
        result: ClarificationIntentProposal | None = None
        signal: BaseException | None = None
        failed = False
        try:
            result = self._propose(submission, principals=principals)
        except Exception:  # noqa: BLE001 - fixed opaque boundary; no logging
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve exact cancellation identity
            caught.__traceback__ = None
            signal = caught
        finally:
            submission = cast(ClarificationProposalSubmission, None)
            principals = frozenset()
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if failed or result is None:
            raise ClarificationError()
        return result


class ProposalConfirmationService:
    """Confirm local graph proposals using one live descriptor-held authority snapshot."""

    def __init__(
        self,
        *,
        graph_store: YamlGraphStore,
        evidence_store: JsonlEvidenceStore,
        case_store: JsonlCaseStore,
        proposal_store: IntentProposalStore,
        changeset_executor: LocalChangeSetExecutor,
        transactions: LocalTransactionCoordinator,
        config_file: SecureFile,
        policy_file: SecureFile,
        binding_files: Mapping[str, SecureFile] | None = None,
    ) -> None:
        self._graph_store = graph_store
        self._evidence_store = evidence_store
        self._case_store = case_store
        self._store = proposal_store
        self._executor = changeset_executor
        self._transactions = transactions
        self._extras: dict[str, SecureFile] = {
            "authority_config": config_file.duplicate(),
            "authority_policy": policy_file.duplicate(),
        }
        for index, (_name, file) in enumerate(sorted((binding_files or {}).items())):
            self._extras[f"authority_binding_{index}"] = file.duplicate()

    @staticmethod
    def _authority(
        snapshot: LocalTransactionSnapshot,
    ) -> tuple[ProjectConfig, MutationPolicy, dict[str, frozenset[str]]]:
        config_bytes = snapshot.content.get("authority_config")
        policy_bytes = snapshot.content.get("authority_policy")
        if config_bytes is None or policy_bytes is None:
            raise ValueError("authority unavailable")
        config = ProjectConfig.model_validate_json(
            json.dumps(load_strict_yaml_mapping_bytes(config_bytes))
        )
        policy = MutationPolicy.model_validate(load_strict_yaml_mapping_bytes(policy_bytes))
        principals: dict[str, set[str]] = {}
        for name, content in snapshot.content.items():
            if not name.startswith("authority_binding_"):
                continue
            if content is None:
                raise ValueError("authority binding unavailable")
            binding = load_connector_config_bytes(content).binding
            for actor, values in binding.actor_principals.items():
                principals.setdefault(actor, set()).update(values)
        return config, policy, {actor: frozenset(values) for actor, values in principals.items()}

    @staticmethod
    def _aliases(
        actor: str,
        policy: MutationPolicy,
        provider_principals: Mapping[str, frozenset[str]],
    ) -> tuple[str, ...]:
        principals = provider_principals.get(actor, frozenset())
        return identity_aliases_for(policy.identities, actor, principals)

    @staticmethod
    def _configured_role(
        config: ProjectConfig,
        connector_id: str,
        locator: str,
    ) -> SourceRoleAssignment | None:
        assignments = tuple(
            item for item in config.source_roles if item.connector_id == connector_id
        )
        exact = tuple(item for item in assignments if item.scope == locator)
        if exact:
            return exact[0]
        inherited = tuple(
            item
            for item in assignments
            if item.inherited and locator.startswith(f"{item.scope.rstrip('/')}/")
        )
        return max(inherited, key=lambda item: len(item.scope), default=None)

    @classmethod
    def _validate_source_roles(
        cls,
        proposal: ClarificationIntentProposal,
        config: ProjectConfig,
        evidence: tuple[EvidenceRecord, ...],
        snapshot: LocalTransactionSnapshot,
    ) -> None:
        configured = set(config.source_roles)
        submitted = set(proposal.source_roles)
        if not submitted.issubset(configured):
            raise ValueError("clarification source role changed")
        _, ingestions, _ = parse_evidence_lines(snapshot.content.get("evidence"))
        connector_ids: dict[str, set[str]] = {}
        for ingestion in ingestions:
            connector_ids.setdefault(ingestion.evidence.id, set()).add(ingestion.connector_id)
        used: set[SourceRoleAssignment] = set()
        for record in evidence:
            if record.id not in proposal.evidence_refs:
                continue
            for connector_id in connector_ids.get(record.id, set()):
                role = cls._configured_role(config, connector_id, record.source_locator)
                if role is not None and role in submitted:
                    used.add(role)
        if used != submitted:
            raise ValueError("clarification source association changed")

    @staticmethod
    def _risk(proposal: IntentProposal, graph: Graph) -> tuple[bool, tuple[str, ...]]:
        changeset = proposal.changeset
        current_nodes = {node.id: node for node in graph.nodes}
        reasons: list[str] = []
        for name, values in (
            ("node_update", changeset.nodes_updated),
            ("node_supersession", changeset.nodes_superseded),
            ("edge_update", changeset.edges_updated),
            ("edge_supersession", changeset.edges_superseded),
            ("implementation_change", changeset.implementation_status_changes),
        ):
            if values:
                reasons.append(name)
        if any(
            item.new_confidence < item.prior_confidence
            or (
                current_nodes.get(item.subject_ref) is not None
                and current_nodes[item.subject_ref].intent_fidelity_confidence is not None
                and item.new_confidence
                < cast(float, current_nodes[item.subject_ref].intent_fidelity_confidence)
            )
            for item in changeset.confidence_changes
        ):
            reasons.append("confidence_weakening")
        if proposal.conflicting_authors:
            reasons.append("conflicting_authors")
        if proposal.destructive:
            reasons.append("destructive")
        if any(
            edge.relation in {RelationType.CONTRADICTS, RelationType.SUPERSEDES}
            for edge in changeset.edges_added
        ):
            reasons.append("contradictory_relationship")
        return bool(reasons), tuple(sorted(set(reasons)))

    @staticmethod
    def _review_case(
        proposal: IntentProposal,
        graph: Graph,
        evidence: tuple[EvidenceRecord, ...],
        reasons: tuple[str, ...],
        principals: frozenset[str],
    ) -> ReconciliationCase:
        evidence_index = {record.id: record for record in evidence}
        proposal_observed_at, proposal_authors = _evidence_attribution(
            proposal.evidence_refs, evidence_index
        )
        fingerprint = _review_case_fingerprint(proposal, reasons)
        affected = tuple(
            sorted(
                {
                    *(node.id for node in proposal.changeset.nodes_added),
                    *(item.node_id for item in proposal.changeset.nodes_updated),
                    *proposal.changeset.nodes_superseded,
                    *(
                        reference
                        for edge in proposal.changeset.edges_added
                        for reference in (edge.from_id, edge.to_id)
                    ),
                    *(
                        reference
                        for update in proposal.changeset.edges_updated
                        for reference in (
                            update.replacement.from_id,
                            update.replacement.to_id,
                        )
                    ),
                    *(
                        reference
                        for edge_id in proposal.changeset.edges_superseded
                        for edge in graph.edges
                        if edge.id == edge_id
                        for reference in (edge.from_id, edge.to_id)
                    ),
                    *(item.subject_ref for item in proposal.changeset.confidence_changes),
                    *(item.claim_id for item in proposal.changeset.implementation_status_changes),
                }
            )
        )
        if not affected:
            raise ValueError("review case has no semantic subject")
        current_nodes = {node.id: node for node in graph.nodes}
        current_assertions: list[_EpistemicAssertion] = []
        for node_id in affected:
            node = current_nodes.get(node_id)
            if node is None:
                continue
            if not refs_allowed(node.evidence_refs, evidence, principals):
                raise ValueError("current semantic evidence unavailable")
            mode, confidence = _required_node_epistemics(node)
            observed_at, authors = _evidence_attribution(node.evidence_refs, evidence_index)
            current_assertions.append(
                (node_id, mode, confidence, node.evidence_refs, observed_at, authors)
            )

        proposed_nodes = dict(current_nodes)
        proposed_nodes.update({node.id: node for node in proposal.changeset.nodes_added})
        proposed_nodes.update(
            {item.node_id: item.replacement for item in proposal.changeset.nodes_updated}
        )
        proposed_confidences = {
            item.subject_ref: item.new_confidence for item in proposal.changeset.confidence_changes
        }
        proposed_assertions: list[_EpistemicAssertion] = []
        for node_id in affected:
            node = proposed_nodes.get(node_id)
            if node is None:
                raise ValueError("proposed semantic assertion unavailable")
            mode, current_confidence = _required_node_epistemics(node)
            proposed_assertions.append(
                (
                    node_id,
                    mode,
                    proposed_confidences.get(node_id, current_confidence),
                    proposal.evidence_refs,
                    proposal_observed_at,
                    proposal_authors,
                )
            )
        evidence_sides = _group_epistemic_sides(
            tuple(current_assertions), label="current", current=True
        ) + _group_epistemic_sides(tuple(proposed_assertions), label="proposal", current=False)
        subject = affected[0]
        case_time = proposal.proposed_at
        history = (
            ClassificationEvent(
                actor=_SERVICE_ACTOR,
                at=case_time,
                prior=ReconciliationStatus.OPEN,
                new=ReconciliationStatus.PROPOSED,
            ),
            ClassificationEvent(
                actor=_SERVICE_ACTOR,
                at=case_time,
                prior=ReconciliationStatus.PROPOSED,
                new=ReconciliationStatus.NEEDS_HUMAN,
            ),
        )
        return ReconciliationCase(
            id=f"case:sha256:{fingerprint}",
            subject_ref=subject,
            case_type=ReconciliationCaseType.CONFLICTING_SOURCES,
            affected_refs=affected,
            evidence_sides=evidence_sides,
            detector_id="intent_workflow.proposal_governance.v1",
            fingerprint=fingerprint,
            created_at=case_time,
            created_by=_SERVICE_ACTOR,
            status=ReconciliationStatus.NEEDS_HUMAN,
            requires_human=True,
            impact="Independent semantic review required",
            history=history,
        )

    def _ensure_case(
        self,
        snapshot: LocalTransactionSnapshot,
        candidate: ReconciliationCase,
    ) -> ReconciliationCase:
        versions = parse_case_versions(snapshot.content.get("cases"))
        latest = {case.id: case for case in versions}
        existing = latest.get(candidate.id)
        if existing is not None:
            if existing.model_dump(
                mode="json", exclude={"status", "resolution", "resolved_by_changeset", "history"}
            ) != candidate.model_dump(
                mode="json", exclude={"status", "resolution", "resolved_by_changeset", "history"}
            ):
                raise ValueError("review case conflict")
            return existing
        append = validate_case_appends(snapshot.content.get("cases"), (candidate,))
        with self._transactions.transaction(
            rollback_base_exceptions=True,
            extras=self._extras,
        ) as transaction:
            if any(
                transaction.read_optional(name) != content
                for name, content in snapshot.content.items()
            ):
                raise ValueError("confirmation snapshot changed")
            transaction.append("cases", append)
        return candidate

    @staticmethod
    def _activation(
        proposal: IntentProposal,
        actor: str,
        at: datetime,
        selected_node_ids: tuple[str, ...],
        review_case_id: str | None,
    ) -> ChangeSet:
        changeset = proposal.changeset
        selected = set(selected_node_ids)
        added_ids = {node.id for node in changeset.nodes_added}
        nodes_added = tuple(
            node.model_copy(
                update={"status": "active", "last_modified_by": actor, "last_modified_at": at}
            )
            for node in changeset.nodes_added
            if node.id in selected
        )
        nodes_updated = tuple(
            update.model_copy(
                update={
                    "replacement": update.replacement.model_copy(
                        update={
                            "status": "active",
                            "last_modified_by": actor,
                            "last_modified_at": at,
                        }
                    )
                }
            )
            for update in changeset.nodes_updated
        )
        edges_added = tuple(
            edge.model_copy(
                update={"status": "active", "last_modified_by": actor, "last_modified_at": at}
            )
            for edge in changeset.edges_added
            if not ({edge.from_id, edge.to_id} & added_ids - selected)
        )
        evidence_refs = tuple(
            dict.fromkeys(
                (
                    *proposal.evidence_refs,
                    *(ref for node in nodes_added for ref in node.evidence_refs),
                    *(ref for update in nodes_updated for ref in update.replacement.evidence_refs),
                    *(
                        ref
                        for change in changeset.confidence_changes
                        for ref in change.evidence_refs
                    ),
                    *(
                        ref
                        for change in changeset.implementation_status_changes
                        for ref in change.evidence_refs
                    ),
                )
            )
        )
        candidate = ChangeSet(
            id="",
            actor=actor,
            timestamp=at,
            baseline_graph_version=proposal.baseline_graph_version,
            evidence_refs=evidence_refs,
            nodes_added=nodes_added,
            nodes_updated=nodes_updated,
            nodes_superseded=changeset.nodes_superseded,
            edges_added=edges_added,
            edges_updated=changeset.edges_updated,
            edges_superseded=changeset.edges_superseded,
            confidence_changes=changeset.confidence_changes,
            implementation_status_changes=changeset.implementation_status_changes,
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(review_case_id,) if review_case_id else (),
            validation_status="validated",
        )
        return candidate.model_copy(
            update={"id": _changeset_id("clarification-activation", candidate)}
        )

    @staticmethod
    def _decision(
        proposal: IntentProposal,
        changeset: ChangeSet,
        actor: str,
        aliases: tuple[str, ...],
        proposer_aliases: tuple[str, ...],
        conflict_aliases: tuple[str, ...],
        at: datetime,
        selected: tuple[str, ...],
        review_case_id: str | None,
        activation_graph_effect_digest: str,
        review_case_preimage_digest: str | None,
    ) -> ProposalDecisionV3:
        material: dict[str, JsonValue] = {
            "schema_version": 3,
            "proposal_id": proposal.id,
            "proposal_digest": proposal.digest,
            "actor": actor,
            "actor_aliases": list(aliases),
            "decided_at": at.isoformat().replace("+00:00", "Z"),
            "action": "confirm",
            "baseline_graph_version": proposal.baseline_graph_version,
            "selected_node_ids": list(selected),
            "activation_changeset_id": changeset.id,
            "activation_graph_effect_digest": activation_graph_effect_digest,
            "review_case_id": review_case_id,
            "review_case_preimage_digest": review_case_preimage_digest,
            "proposal_author_aliases": list(proposer_aliases),
            "conflicting_author_aliases": list(conflict_aliases),
        }
        return ProposalDecisionV3(
            id=_decision_id(material),
            proposal_id=proposal.id,
            proposal_digest=proposal.digest,
            actor=actor,
            actor_aliases=aliases,
            decided_at=at,
            baseline_graph_version=proposal.baseline_graph_version,
            selected_node_ids=selected,
            activation_changeset_id=changeset.id,
            activation_graph_effect_digest=activation_graph_effect_digest,
            review_case_id=review_case_id,
            review_case_preimage_digest=review_case_preimage_digest,
            proposal_author_aliases=proposer_aliases,
            conflicting_author_aliases=conflict_aliases,
        )

    @staticmethod
    def _history_changesets(content: bytes | None) -> tuple[ChangeSet, ...]:
        if content is None or (content and not content.endswith(b"\n")):
            raise ValueError("activation history unavailable")
        changesets: list[ChangeSet] = []
        for line in content.splitlines(keepends=True):
            if not line.strip():
                continue
            changeset = ChangeSet.model_validate_json(line)
            if serialize_changeset(changeset) != line:
                raise ValueError("activation history is not canonical")
            changesets.append(changeset)
        return tuple(changesets)

    @staticmethod
    def _activation_graph_effect_digest(graph: Graph, changeset: ChangeSet) -> str:
        nodes = {node.id: node for node in graph.nodes}
        edges = {edge.id: edge for edge in graph.edges}
        node_ids = tuple(
            sorted(
                {
                    *(node.id for node in changeset.nodes_added),
                    *(update.node_id for update in changeset.nodes_updated),
                    *changeset.nodes_superseded,
                    *(change.subject_ref for change in changeset.confidence_changes),
                    *(change.claim_id for change in changeset.implementation_status_changes),
                }
            )
        )
        edge_ids = tuple(
            sorted(
                {
                    *(edge.id for edge in changeset.edges_added),
                    *(update.edge_id for update in changeset.edges_updated),
                    *changeset.edges_superseded,
                }
            )
        )
        return _digest(
            {
                "graph_id": graph.id,
                "graph_version": graph.version,
                "expected_graph_version": changeset.baseline_graph_version + 1,
                "nodes": [
                    nodes[node_id].model_dump(mode="json") if node_id in nodes else None
                    for node_id in node_ids
                ],
                "edges": [
                    edges[edge_id].model_dump(mode="json", by_alias=True)
                    if edge_id in edges
                    else None
                    for edge_id in edge_ids
                ],
            }
        )

    def _authenticate_confirmation_snapshot(
        self,
        snapshot: LocalTransactionSnapshot,
        *,
        proposal: ClarificationIntentProposal,
        actor: str,
        high_risk: bool,
        reasons: tuple[str, ...],
        review_case: ReconciliationCase | None,
    ) -> _AuthenticatedConfirmation:
        config, policy, provider_principals = self._authority(snapshot)
        graph, evidence, _ = _records(snapshot)
        ledger = snapshot.content.get("intent_proposals")
        actor_aliases = self._aliases(actor, policy, provider_principals)
        proposer_aliases = self._aliases(proposal.proposed_by, policy, provider_principals)
        conflict_aliases = tuple(
            sorted(
                {
                    alias
                    for author in proposal.conflicting_authors
                    for alias in self._aliases(author, policy, provider_principals)
                }
            )
        )
        if (
            ledger is None
            or self._store.bytes() != ledger
            or self._store.get(proposal.id) != proposal
            or proposal.proposed_by not in policy.contributors
            or proposal.baseline_graph_version != graph.version
            or proposal.changeset.baseline_graph_version != graph.version
            or proposal.changeset.actor != proposal.proposed_by
            or proposal.changeset.timestamp != proposal.proposed_at
            or proposal.changeset.evidence_refs != proposal.evidence_refs
            or config.project_id not in {graph.id.removeprefix("graph:"), graph.name}
            or not refs_allowed(proposal.evidence_refs, evidence, frozenset(actor_aliases))
        ):
            raise ValueError("confirmation authority changed")
        self._validate_source_roles(proposal, config, evidence, snapshot)
        if high_risk:
            if (
                review_case is None
                or actor not in policy.approvers
                or not set(actor_aliases).isdisjoint(set(proposer_aliases) | set(conflict_aliases))
            ):
                raise ValueError("review authority changed")
            canonical_case = self._review_case(
                proposal,
                graph,
                evidence,
                reasons,
                frozenset(actor_aliases),
            )
            if canonical_case != review_case or not refs_allowed(
                canonical_case.all_evidence_refs,
                evidence,
                frozenset(actor_aliases),
            ):
                raise ValueError("review case authority changed")
            review_case = canonical_case
        elif review_case is not None or actor not in policy.contributors:
            raise ValueError("contributor authority unavailable")
        return _AuthenticatedConfirmation(
            snapshot=snapshot,
            config=config,
            policy=policy,
            graph=graph,
            evidence=evidence,
            actor_aliases=actor_aliases,
            proposer_aliases=proposer_aliases,
            conflict_aliases=conflict_aliases,
            review_case=review_case,
        )

    def _authenticate_applied(
        self,
        *,
        proposal: ClarificationIntentProposal,
        decision: ProposalDecisionRecord,
        actor: str,
        at: datetime,
        selected: tuple[str, ...],
    ) -> ProposalConfirmationResult:
        snapshot = self._transactions.snapshot(self._extras)
        config, policy, provider_principals = self._authority(snapshot)
        graph, evidence, _ = _records(snapshot)
        ledger = snapshot.content.get("intent_proposals")
        actor_aliases = self._aliases(actor, policy, provider_principals)
        proposer_aliases = self._aliases(proposal.proposed_by, policy, provider_principals)
        conflict_aliases = tuple(
            sorted(
                {
                    alias
                    for author in proposal.conflicting_authors
                    for alias in self._aliases(author, policy, provider_principals)
                }
            )
        )
        added = tuple(sorted(node.id for node in proposal.changeset.nodes_added))
        if (
            ledger is None
            or self._store.bytes() != ledger
            or self._store.get(proposal.id) != proposal
            or config.project_id not in {graph.id.removeprefix("graph:"), graph.name}
            or not set(selected).issubset(added)
            or (added and not selected)
            or not refs_allowed(proposal.evidence_refs, evidence, frozenset(actor_aliases))
            or at < proposal.proposed_at
        ):
            raise ValueError("applied confirmation binding changed")
        self._validate_source_roles(proposal, config, evidence, snapshot)
        high_risk, reasons = self._risk(proposal, graph)
        if not isinstance(decision, ProposalDecisionV3):
            raise TypeError("applied clarification decision unavailable")
        aliases_overlap = not set(actor_aliases).isdisjoint(
            set(proposer_aliases) | set(conflict_aliases)
        )
        if high_risk:
            if (
                decision.review_case_id
                != f"case:sha256:{_review_case_fingerprint(proposal, reasons)}"
                or actor not in policy.approvers
                or aliases_overlap
            ):
                raise ValueError("replay review authority unavailable")
        elif decision.review_case_id is not None or actor not in policy.contributors:
            raise ValueError("replay contributor authority unavailable")
        activation = self._activation(
            proposal,
            actor,
            at,
            selected,
            decision.review_case_id,
        )
        expected_decision = self._decision(
            proposal,
            activation,
            actor,
            actor_aliases,
            proposer_aliases,
            conflict_aliases,
            at,
            selected,
            decision.review_case_id,
            decision.activation_graph_effect_digest,
            decision.review_case_preimage_digest,
        )
        if decision != expected_decision:
            raise ValueError("conflicting applied clarification decision")

        events = self._store.clarification_events(proposal.clarification_session_id)
        closed = tuple(event for event in events if event.event_type == "closed")
        if (
            len(closed) != 1
            or events[-1] != closed[0]
            or closed[0].proposal_id != proposal.id
            or closed[0].decision_id != decision.id
            or closed[0].activation_changeset_id != activation.id
            or closed[0].actor != actor
            or closed[0].at != at
            or closed[0].session.status != "closed"
        ):
            raise ValueError("clarification closure replay mismatch")

        history = self._history_changesets(snapshot.content.get("history"))
        matching = tuple(item for item in history if item.id == activation.id)
        if len(matching) != 1 or matching[0] != activation or history[-1] != activation:
            raise ValueError("activation history replay mismatch")
        if (
            self._activation_graph_effect_digest(graph, activation)
            != decision.activation_graph_effect_digest
        ):
            raise ValueError("activation graph replay mismatch")

        if decision.review_case_id is not None:
            versions = tuple(
                case
                for case in parse_case_versions(snapshot.content.get("cases"))
                if case.id == decision.review_case_id
            )
            if len(versions) < 2:
                raise ValueError("resolved review case unavailable")
            if (
                _digest(versions[-2].model_dump(mode="json"))
                != decision.review_case_preimage_digest
            ):
                raise ValueError("review case preimage replay mismatch")
            expected_case = transition_case(
                versions[-2],
                ReconciliationStatus.RESOLVED,
                actor,
                at,
                ResolutionAction.UPDATE_REQUIREMENT,
                activation.id,
            )
            if versions[-1] != expected_case or not refs_allowed(
                expected_case.all_evidence_refs, evidence, frozenset(actor_aliases)
            ):
                raise ValueError("resolved review case replay mismatch")
        return ProposalConfirmationResult(
            status=ProposalConfirmationStatus.APPLIED,
            proposal_id=proposal.id,
            graph_version=graph.version,
            decision_id=decision.id,
            case_id=decision.review_case_id,
        )

    def _confirm(
        self,
        proposal_id: str,
        *,
        actor: str,
        at: datetime,
        selected_node_ids: tuple[str, ...],
    ) -> ProposalConfirmationResult:
        at = _utc(at)
        snapshot = self._transactions.snapshot(self._extras)
        config, policy, provider_principals = self._authority(snapshot)
        graph, evidence, _ = _records(snapshot)
        ledger = snapshot.content.get("intent_proposals")
        if ledger is None or self._store.bytes() != ledger:
            raise ValueError("proposal ledger changed")
        proposal = self._store.get(proposal_id)
        if not isinstance(proposal, ClarificationIntentProposal):
            raise TypeError("proposal is not clarification-bound")
        actor_aliases = self._aliases(actor, policy, provider_principals)
        proposer_aliases = self._aliases(proposal.proposed_by, policy, provider_principals)
        conflict_aliases = tuple(
            sorted(
                {
                    alias
                    for author in proposal.conflicting_authors
                    for alias in self._aliases(author, policy, provider_principals)
                }
            )
        )
        existing_decision = self._store.decision_for(proposal.id)
        added = tuple(sorted(node.id for node in proposal.changeset.nodes_added))
        selected = tuple(sorted(selected_node_ids or added))
        if config.project_id != graph.id.removeprefix("graph:") and config.project_id != graph.name:
            raise ValueError("project binding changed")
        if (
            not set(selected).issubset(added)
            or (added and not selected)
            or not refs_allowed(proposal.evidence_refs, evidence, actor_aliases)
            or at < proposal.proposed_at
        ):
            raise ValueError("invalid confirmation binding")
        if existing_decision is None and graph.version != proposal.baseline_graph_version:
            raise ValueError("stale proposal")
        high_risk, reasons = self._risk(proposal, graph)
        if existing_decision is not None:
            return self._authenticate_applied(
                proposal=proposal,
                decision=existing_decision,
                actor=actor,
                at=at,
                selected=selected,
            )
        case: ReconciliationCase | None = None
        if high_risk:
            case = self._review_case(proposal, graph, evidence, reasons, frozenset(actor_aliases))
            case = self._ensure_case(snapshot, case)
            if actor not in policy.approvers:
                return ProposalConfirmationResult(
                    status=ProposalConfirmationStatus.REVIEW_REQUIRED,
                    proposal_id=proposal.id,
                    graph_version=graph.version,
                    case_id=case.id,
                )
            if not set(actor_aliases).isdisjoint(set(proposer_aliases) | set(conflict_aliases)):
                raise ValueError("reviewer is not independent")
            snapshot = self._transactions.snapshot(self._extras)
        authenticated = self._authenticate_confirmation_snapshot(
            snapshot,
            proposal=proposal,
            actor=actor,
            high_risk=high_risk,
            reasons=reasons,
            review_case=case,
        )
        snapshot = authenticated.snapshot
        config = authenticated.config
        policy = authenticated.policy
        graph = authenticated.graph
        evidence = authenticated.evidence
        actor_aliases = authenticated.actor_aliases
        proposer_aliases = authenticated.proposer_aliases
        conflict_aliases = authenticated.conflict_aliases
        case = authenticated.review_case
        changeset = self._activation(
            proposal,
            actor,
            at,
            selected,
            case.id if case is not None else None,
        )
        expected_graph = apply_changeset_with_case_effects(graph, changeset)
        activation_graph_effect_digest = self._activation_graph_effect_digest(
            expected_graph, changeset
        )
        review_case_preimage_digest = (
            _digest(case.model_dump(mode="json")) if case is not None else None
        )
        decision = self._decision(
            proposal,
            changeset,
            actor,
            actor_aliases,
            proposer_aliases,
            conflict_aliases,
            at,
            selected,
            case.id if case is not None else None,
            activation_graph_effect_digest,
            review_case_preimage_digest,
        )
        resolved_cases: tuple[ReconciliationCase, ...] = ()
        if case is not None:
            if case.status is not ReconciliationStatus.NEEDS_HUMAN:
                raise ValueError("review case unavailable")
            resolved_cases = (
                transition_case(
                    case,
                    ReconciliationStatus.RESOLVED,
                    actor,
                    at,
                    ResolutionAction.UPDATE_REQUIREMENT,
                    changeset.id,
                ),
            )
        ledger = self._store.bytes()
        decision_frame = serialize_intent_ledger_record(
            IntentLedgerRecord(sequence=len(ledger.splitlines()), decision=decision)
        )
        proposed_session = self._store.session(proposal.clarification_session_id)
        if proposed_session.status == "closed":
            durable = self._store.decision_for(proposal.id)
            if durable != decision:
                raise ValueError("conflicting concurrent clarification closure")
            return self._authenticate_applied(
                proposal=proposal,
                decision=durable,
                actor=actor,
                at=at,
                selected=selected,
            )
        if proposed_session.status != "proposed":
            raise ValueError("clarification session is not proposed")
        closed_event = _event(
            proposed_session.model_copy(update={"status": "closed"}),
            "closed",
            actor,
            at,
            proposed_session.latest_event_id,
            proposal.id,
            decision.id,
            changeset.id,
        )
        closed_frame = serialize_intent_ledger_record(
            IntentLedgerRecord(
                sequence=len(ledger.splitlines()) + 1,
                clarification=closed_event,
            )
        )
        extras_preimages = {name: snapshot.content.get(name) for name in self._extras}
        try:
            self._executor.apply(
                changeset,
                resolved_cases=resolved_cases,
                intent_proposal_preimage=ledger,
                intent_proposal_append=decision_frame + closed_frame,
                evidence_preimage=snapshot.content.get("evidence"),
                rollback_base_exceptions=True,
                read_only_extras=self._extras,
                extra_preimages=extras_preimages,
            )
        except Exception:
            durable = self._store.decision_for(proposal.id)
            if durable != decision:
                raise
            return self._authenticate_applied(
                proposal=proposal,
                decision=durable,
                actor=actor,
                at=at,
                selected=selected,
            )
        return self._authenticate_applied(
            proposal=proposal,
            decision=decision,
            actor=actor,
            at=at,
            selected=selected,
        )

    def confirm(
        self,
        proposal_id: str,
        *,
        actor: str,
        at: datetime,
        selected_node_ids: tuple[str, ...] = (),
    ) -> ProposalConfirmationResult:
        signal: BaseException | None = None
        try:
            return self._confirm(
                proposal_id,
                actor=actor,
                at=at,
                selected_node_ids=selected_node_ids,
            )
        except Exception:  # noqa: BLE001, S110 - fixed opaque boundary; no logging
            pass
        except BaseException as caught:  # noqa: BLE001 - preserve exact cancellation identity
            caught.__traceback__ = None
            signal = caught
        finally:
            proposal_id = ""
            actor = ""
            at = cast(datetime, None)
            selected_node_ids = ()
        if signal is None:
            raise ClarificationError()
        raise signal.with_traceback(None)

    def close(self) -> None:
        """Release descriptor duplicates owned by this service."""
        for file in self._extras.values():
            file.close()
        self._extras.clear()


__all__ = [
    "ClarificationCoordinator",
    "ClarificationError",
    "ProposalConfirmationResult",
    "ProposalConfirmationService",
    "ProposalConfirmationStatus",
]
