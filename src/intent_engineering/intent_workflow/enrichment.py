"""Deterministic, evidence-backed progressive graph enrichment."""

from __future__ import annotations

import hashlib
import json
import re
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from intent_engineering.assessment import (
    AssessmentDimension,
    AssessmentHealth,
    AssessmentPolicy,
    AssessmentReport,
    AssessmentSnapshot,
    GraphAssessmentService,
    build_assessment_snapshot_from_transaction,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.conversation import (
    ConversationCapture,
    validate_conversation_ingestion,
)
from intent_engineering.intent_workflow.enrichment_models import (
    EnrichmentEvent,
    EnrichmentProposalBinding,
    EnrichmentSession,
)
from intent_engineering.intent_workflow.enrichment_store import EnrichmentSessionStore
from intent_engineering.intent_workflow.models import (
    ClarificationIntentProposal,
    ClarificationProposalSubmission,
)
from intent_engineering.storage.secure import SecureFile
from intent_engineering.storage.transaction import (
    LocalTransaction,
    LocalTransactionExtraReadPolicy,
)

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ANSWER_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_ANSWER_BYTES = 16 * 1024
_MAX_QUESTION_TEXT_BYTES = 8 * 1024
_MAX_DEPENDENCY_NODES = 10_000
_MAX_DEPENDENCY_EDGES = 50_000
_MAX_DEPENDENCY_WORK = 1_000_000
_AUTHORITY_POLICY = LocalTransactionExtraReadPolicy(
    max_bytes=1024 * 1024,
    nonblocking_regular=True,
)
_AUTHORITY_POLICIES = {"acl_policy": _AUTHORITY_POLICY, "config": _AUTHORITY_POLICY}
_PROTECTED_PREIMAGES = (
    "acl_policy",
    "cases",
    "config",
    "graph",
    "history",
    "intent_proposals",
)
_SEVERITY = {
    AssessmentHealth.RED: 0,
    AssessmentHealth.ORANGE: 1,
    AssessmentHealth.GREEN: 2,
    AssessmentHealth.UNASSESSED: 3,
}
_DEPENDENCY_RELATIONS = frozenset(AssessmentPolicy.v1().critical_relations)
_TEMPLATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "no_current_test_evidence",
        "What current test evidence verifies {node_id}?",
        ("test result", "verified behavior", "evidence source"),
    ),
    (
        "no_implementation_link",
        "Which implementation satisfies {node_id}?",
        ("implementation reference", "implemented behavior"),
    ),
    (
        "no_intent_coverage",
        "Which approved intent does {node_id} support?",
        ("intent reference", "intended outcome"),
    ),
    (
        "no_explicit_typed_assertion",
        "What explicit assertion should be recorded for {node_id}?",
        ("assertion", "source"),
    ),
    (
        "no_visible_evidence_identity",
        "Which attributable evidence supports {node_id}?",
        ("evidence source", "author"),
    ),
    (
        "blocking_conflict",
        "How should the blocking conflict for {node_id} be resolved?",
        ("preferred position", "reason", "supporting evidence"),
    ),
    (
        "stale_evidence",
        "What current evidence refreshes {node_id}?",
        ("current evidence", "source revision"),
    ),
)


def _recursive_type_exact_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, BaseModel):
        right_model = cast(BaseModel, right)
        return all(
            _recursive_type_exact_equal(getattr(left, name), getattr(right_model, name))
            for name in type(left).model_fields
        )
    if isinstance(left, (tuple, list)):
        right_sequence = cast(tuple[object, ...] | list[object], right)
        return len(left) == len(right_sequence) and all(
            _recursive_type_exact_equal(left_item, right_item)
            for left_item, right_item in zip(left, right_sequence, strict=True)
        )
    if isinstance(left, dict):
        right_mapping = cast(dict[object, object], right)
        if len(left) != len(right_mapping):
            return False
        unmatched = list(right_mapping.items())
        for left_key, left_value in left.items():
            for index, (right_key, right_value) in enumerate(unmatched):
                if _recursive_type_exact_equal(left_key, right_key):
                    if not _recursive_type_exact_equal(left_value, right_value):
                        return False
                    unmatched.pop(index)
                    break
            else:
                return False
        return not unmatched
    return bool(left == right)


class GraphEnrichmentError(ValueError):
    """Fixed public failure for enrichment operations."""

    def __init__(self) -> None:
        super().__init__("graph enrichment unavailable")


class EnrichmentQuestion(StrictModel):
    """One bounded deterministic question grounded in an approved rubric gap."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    gap_id: Annotated[str, Field(min_length=1, max_length=256)]
    node_id: Annotated[str, Field(min_length=1, max_length=256)]
    dimension: AssessmentDimension
    rule_id: Annotated[str, Field(min_length=1, max_length=256)]
    prompt: Annotated[str, Field(min_length=1, max_length=_MAX_QUESTION_TEXT_BYTES)]
    reason: Annotated[str, Field(min_length=1, max_length=_MAX_QUESTION_TEXT_BYTES)]
    requested_fields: Annotated[tuple[str, ...], Field(min_length=1, max_length=16)]
    evidence_scope: Annotated[tuple[str, ...], Field(max_length=256)] = ()

    @field_validator("requested_fields", "evidence_scope")
    @classmethod
    def canonical_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(type(value) is not str or not value or _CONTROL.search(value) for value in values):
            raise ValueError("invalid enrichment question")
        if len(values) != len(set(values)):
            raise ValueError("invalid enrichment question")
        return tuple(sorted(values))

    @field_validator("prompt")
    @classmethod
    def require_safe_prompt(cls, value: str) -> str:
        if _CONTROL.search(value) or len(value.encode("utf-8")) > _MAX_QUESTION_TEXT_BYTES:
            raise ValueError("invalid enrichment prompt")
        return value


class QuestionWordingPort(Protocol):
    """Optional non-authoritative wording adapter."""

    def rephrase(self, question: EnrichmentQuestion) -> EnrichmentQuestion: ...


class ProposalPort(Protocol):
    """Existing governed proposal boundary used after enrichment."""

    def propose_enrichment(
        self,
        submission: ClarificationProposalSubmission,
        *,
        principals: frozenset[str],
        binding: EnrichmentProposalBinding,
        enrichment_store: EnrichmentSessionStore,
        authority_files: dict[str, SecureFile],
        authority_read_policies: dict[str, LocalTransactionExtraReadPolicy],
    ) -> ClarificationIntentProposal: ...


class _Candidate(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    question: EnrichmentQuestion
    priority: tuple[int, int, int, int, int, int, str]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _preimage_digest(content: bytes | None) -> str:
    framed = b"absent" if content is None else b"present\x00" + content
    return f"sha256:{hashlib.sha256(framed).hexdigest()}"


def _gap_id(node_id: str, dimension: AssessmentDimension, rule_id: str) -> str:
    return (
        f"gap:v1:{hashlib.sha256(_canonical_json([node_id, dimension.value, rule_id])).hexdigest()}"
    )


def _template(rule_id: str, node_id: str) -> tuple[str, tuple[str, ...]]:
    for suffix, prompt, fields in _TEMPLATES:
        if rule_id.endswith(suffix):
            return prompt.format(node_id=node_id), fields
    return (
        f"What evidence or clarification addresses {rule_id} for {node_id}?",
        ("clarification", "supporting evidence"),
    )


def _dependency_reach(snapshot: AssessmentSnapshot) -> dict[str, int]:
    if (
        len(snapshot.graph.nodes) > _MAX_DEPENDENCY_NODES
        or len(snapshot.graph.edges) > _MAX_DEPENDENCY_EDGES
    ):
        raise ValueError("dependency graph exceeds enrichment bound")
    outgoing: dict[str, set[str]] = {}
    for edge in snapshot.graph.edges:
        if edge.status != "active" or edge.relation not in _DEPENDENCY_RELATIONS:
            continue
        outgoing.setdefault(edge.from_id, set()).add(edge.to_id)
    reach: dict[str, int] = {}
    work = 0
    for node in snapshot.graph.nodes:
        seen: set[str] = set()
        pending = list(outgoing.get(node.id, ()))
        while pending:
            work += 1
            if work > _MAX_DEPENDENCY_WORK:
                raise ValueError("dependency graph exceeds enrichment bound")
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(outgoing.get(current, ()))
        seen.discard(node.id)
        reach[node.id] = len(seen)
    return reach


def _candidates(
    report: AssessmentReport,
    snapshot: AssessmentSnapshot,
    *,
    focus_id: str | None,
    excluded: frozenset[str],
) -> tuple[_Candidate, ...]:
    critical_ids = frozenset(node for branch in report.branches for node in branch.node_ids)
    dependency_reach = _dependency_reach(snapshot)
    if focus_id is None:
        focused = frozenset(scorecard.node_id for scorecard in report.nodes)
    else:
        branches = tuple(branch for branch in report.branches if branch.branch_id == focus_id)
        nodes = tuple(scorecard for scorecard in report.nodes if scorecard.node_id == focus_id)
        if len(branches) + len(nodes) != 1:
            raise ValueError("invalid enrichment focus")
        focused = frozenset(branches[0].node_ids) if branches else frozenset({nodes[0].node_id})
    result: list[_Candidate] = []
    for scorecard in report.nodes:
        if scorecard.node_id not in focused:
            continue
        for dimension in scorecard.dimensions:
            for check in dimension.failed:
                identifier = _gap_id(scorecard.node_id, dimension.dimension, check.rule_id)
                if identifier in excluded:
                    continue
                prompt, fields = _template(check.rule_id, scorecard.node_id)
                question = EnrichmentQuestion(
                    gap_id=identifier,
                    node_id=scorecard.node_id,
                    dimension=dimension.dimension,
                    rule_id=check.rule_id,
                    prompt=prompt,
                    reason=check.explanation,
                    requested_fields=fields,
                    evidence_scope=tuple(sorted({*dimension.evidence_refs, *check.references})),
                )
                confidence_impact = 100 - (dimension.confidence or 0)
                robustness_impact = check.points
                conflict_staleness = (
                    0
                    if check.rule_id.endswith("blocking_conflict")
                    else 1
                    if check.rule_id.endswith("stale_evidence")
                    else 2
                )
                result.append(
                    _Candidate(
                        question=question,
                        priority=(
                            _SEVERITY[check.severity],
                            0 if scorecard.node_id in critical_ids else 1,
                            -confidence_impact,
                            -robustness_impact,
                            -dependency_reach.get(scorecard.node_id, 0),
                            conflict_staleness,
                            identifier,
                        ),
                    )
                )
    return tuple(sorted(result, key=lambda item: item.priority))


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _scrub_signal(error: BaseException) -> BaseException:
    old_traceback = error.__traceback__
    error.args = ()
    error.__dict__.clear()
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    if old_traceback is not None:
        traceback.clear_frames(old_traceback)
    old_traceback = None
    return error


def _scrub_suspended_signal(error: BaseException) -> BaseException:
    old_traceback = error.__traceback__
    error.args = ()
    error.__dict__.clear()
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    if old_traceback is not None and old_traceback.tb_next is not None:
        traceback.clear_frames(old_traceback.tb_next)
    old_traceback = None
    return error


def _close_authority_files(
    policy: SecureFile | None, config: SecureFile | None
) -> tuple[BaseException, ...]:
    """Attempt every close in an unwound helper so retained frames can be cleared."""
    errors: list[BaseException] = []
    for opened in (policy, config):
        if opened is None:
            continue
        try:
            opened.close()
        except BaseException as caught:  # noqa: BLE001 - returned for boundary scrubbing
            errors.append(caught)
    opened = None
    policy = None
    config = None
    return tuple(errors)


def _raise_signal(error: BaseException) -> NoReturn:
    raise error.with_traceback(None) from None


class GraphEnrichmentService:
    """Run bounded enrichment without directly mutating the canonical graph."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        actor: str,
        clock: Callable[[], datetime] | None = None,
        assessment_service: GraphAssessmentService | None = None,
        wording: QuestionWordingPort | None = None,
        proposal_service: ProposalPort | None = None,
    ) -> None:
        if type(actor) is not str or not actor or _CONTROL.search(actor):
            raise GraphEnrichmentError() from None
        self._runtime = runtime
        self._actor = actor
        self._clock = clock or _utc_now
        self._assessment = assessment_service or GraphAssessmentService(clock=self._clock)
        self._capture = ConversationCapture(
            runtime.evidence_store, connector_id="conversation:enrichment"
        )
        self._wording = wording
        self._proposal = proposal_service

    def _now(self) -> datetime:
        value = self._clock()
        offset = value.utcoffset() if type(value) is datetime and value.tzinfo is not None else None
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or offset is None
            or offset.total_seconds() != 0
            or value.microsecond != 0
        ):
            raise ValueError("invalid enrichment clock")
        return value.astimezone(UTC)

    @contextmanager
    def _transaction(
        self, *, mutable_targets: frozenset[str] = frozenset()
    ) -> Iterator[LocalTransaction]:
        config: SecureFile | None = None
        policy: SecureFile | None = None
        protected: dict[str, bytes | None] = {}
        primary: BaseException | None = None
        cleanups: list[BaseException] = []
        try:
            try:
                config = self._runtime.workspace_directory.file("config.yaml")
                policy = self._runtime.workspace_directory.file("approvals/policy.yaml")
                with self._runtime.transactions.transaction(
                    rollback_base_exceptions=True,
                    extras={"config": config, "acl_policy": policy},
                    extra_read_policies=_AUTHORITY_POLICIES,
                ) as transaction:
                    protected = {
                        name: transaction.read_optional(name) for name in _PROTECTED_PREIMAGES
                    }
                    yield transaction
                    if any(
                        transaction.read_optional(name) != content
                        for name, content in protected.items()
                        if name not in mutable_targets
                    ):
                        raise ValueError("enrichment preimage changed")
            except BaseException as caught:  # noqa: BLE001 - clean before re-raising
                primary = caught
        finally:
            protected.clear()
            cleanups.extend(_close_authority_files(policy, config))
            config = None
            policy = None
        cleanup: BaseException | None
        for cleanup in cleanups:
            _scrub_signal(cleanup)
        cleanup = None
        cleanup_cancellation = next(
            (item for item in cleanups if not isinstance(item, Exception)), None
        )
        selected: BaseException | None
        if primary is not None and not isinstance(primary, Exception):
            selected = primary
        elif cleanup_cancellation is not None:
            if primary is not None:
                _scrub_suspended_signal(primary)
            selected = cleanup_cancellation
        else:
            selected = primary if primary is not None else cleanups[0] if cleanups else None
        primary = None
        cleanup_cancellation = None
        cleanups.clear()
        if selected is not None:
            _raise_signal(selected)

    def _snapshot(
        self, transaction: LocalTransaction
    ) -> tuple[AssessmentSnapshot, AssessmentReport]:
        snapshot = build_assessment_snapshot_from_transaction(
            self._runtime,
            self._actor,
            transaction,
        )
        report = self._assessment.assess(snapshot)
        canonical = AssessmentReport.model_validate_json(report.model_dump_json())
        if (
            type(report) is not AssessmentReport
            or canonical != report
            or report.project_id != snapshot.project_id
            or report.graph_id != snapshot.graph.id
            or report.graph_version != snapshot.graph.version
            or report.graph_digest != snapshot.graph_digest
            or report.evidence_digest != snapshot.evidence_digest
            or report.ingestion_digest != snapshot.ingestion_digest
            or report.case_digest != snapshot.case_digest
            or report.clarification_digest != snapshot.clarification_digest
            or report.history_digest != snapshot.history_digest
            or report.snapshot_digest != snapshot.aggregate_digest
            or report.principal_projection_digest != snapshot.principal_projection_digest
        ):
            raise ValueError("invalid enrichment assessment")
        return snapshot, canonical

    def _last_event(self, session_id: str) -> EnrichmentEvent:
        events = self._runtime.enrichment_sessions.events(session_id)
        if not events:
            raise ValueError("missing enrichment session")
        return events[-1]

    def _append(
        self,
        previous: EnrichmentEvent | None,
        event_type: Literal[
            "opened", "answered", "skipped", "paused", "resumed", "reassessed", "completed"
        ],
        session: EnrichmentSession,
        *,
        gap_id: str | None = None,
        answer_ref: str | None = None,
        reassessment_predecessor: str | None = None,
        operation_id: str | None = None,
    ) -> EnrichmentEvent:
        event = EnrichmentEvent(
            sequence=0 if previous is None else previous.sequence + 1,
            predecessor_event_digest=None if previous is None else previous.digest,
            event_type=event_type,
            session=session,
            gap_id=gap_id,
            answer_evidence_ref=answer_ref,
            reassessment_predecessor_digest=reassessment_predecessor,
            operation_id=operation_id,
            at=session.updated_at,
        )
        if not self._runtime.enrichment_sessions.append(event):
            durable = self._last_event(session.id)
            if durable != event:
                raise ValueError("divergent enrichment event")
        return event

    @staticmethod
    def _remaining(previous: EnrichmentEvent, now: datetime) -> int:
        elapsed = (now - previous.at).total_seconds()
        if elapsed < 0 or not elapsed.is_integer():
            raise ValueError("invalid enrichment time")
        if previous.session.status == "paused":
            return previous.session.remaining_budget_seconds
        return max(0, previous.session.remaining_budget_seconds - int(elapsed))

    def _selected(
        self,
        report: AssessmentReport,
        snapshot: AssessmentSnapshot,
        session: EnrichmentSession,
    ) -> EnrichmentQuestion | None:
        choices = _candidates(
            report,
            snapshot,
            focus_id=session.focus_id,
            excluded=frozenset({*session.answered_gap_ids, *session.skipped_gap_ids}),
        )
        return None if not choices else choices[0].question

    def _reassess(
        self,
        previous: EnrichmentEvent,
        snapshot: AssessmentSnapshot,
        report: AssessmentReport,
        now: datetime,
        operation_id: str | None = None,
    ) -> EnrichmentEvent:
        selected = self._selected(report, snapshot, previous.session)
        session = previous.session.model_copy(
            update={
                "snapshot_digest": report.snapshot_digest,
                "current_gap_id": None if selected is None else selected.gap_id,
                "remaining_budget_seconds": self._remaining(previous, now),
                "updated_at": now,
            }
        )
        if session == previous.session:
            return previous
        return self._append(
            previous,
            "reassessed",
            session,
            reassessment_predecessor=previous.session.snapshot_digest,
            operation_id=operation_id,
        )

    def _terminal_if_needed(
        self,
        previous: EnrichmentEvent,
        now: datetime,
        operation_id: str | None = None,
    ) -> EnrichmentEvent:
        session = previous.session
        expired = session.budget_minutes is not None and session.remaining_budget_seconds == 0
        if session.current_gap_id is not None and not expired:
            return previous
        completed = session.model_copy(
            update={"status": "complete", "current_gap_id": None, "updated_at": now}
        )
        return self._append(previous, "completed", completed, operation_id=operation_id)

    def _start(self, minutes: Literal[5, 15, 30] | None, focus: str | None) -> EnrichmentSession:
        if minutes not in {None, 5, 15, 30} or (minutes is None and focus is None):
            raise ValueError("invalid enrichment scope")
        if focus is not None and (type(focus) is not str or not focus or _CONTROL.search(focus)):
            raise ValueError("invalid enrichment focus")
        now = self._now()
        with self._transaction() as transaction:
            snapshot, report = self._snapshot(transaction)
            provisional = EnrichmentSession(
                id="refine:pending",
                status="open",
                focus_id=focus,
                budget_minutes=minutes,
                remaining_budget_seconds=0 if minutes is None else minutes * 60,
                snapshot_digest=report.snapshot_digest,
                current_gap_id=None,
                started_at=now,
                updated_at=now,
            )
            selected = self._selected(report, snapshot, provisional)
            identity = hashlib.sha256(
                _canonical_json(
                    [self._actor, report.snapshot_digest, focus, minutes, now.isoformat()]
                )
            ).hexdigest()
            session = provisional.model_copy(
                update={
                    "id": f"refine:{identity}",
                    "current_gap_id": None if selected is None else selected.gap_id,
                }
            )
            opened = self._append(None, "opened", session)
            return self._terminal_if_needed(opened, now).session

    def start(
        self,
        minutes: Literal[5, 15, 30] | None = None,
        focus: str | None = None,
    ) -> EnrichmentSession:
        return cast(EnrichmentSession, self._public(lambda: self._start(minutes, focus)))

    def _current(self, session_id: str) -> EnrichmentSession:
        now = self._now()
        with self._transaction() as transaction:
            previous = self._last_event(session_id)
            if previous.session.status in {"complete", "cancelled"}:
                return previous.session
            if previous.session.status == "paused":
                return previous.session
            snapshot, report = self._snapshot(transaction)
            refreshed = self._reassess(previous, snapshot, report, now)
            return self._terminal_if_needed(refreshed, now).session

    def current(self, session_id: str) -> EnrichmentSession:
        return cast(EnrichmentSession, self._public(lambda: self._current(session_id)))

    def _question(self, session_id: str) -> EnrichmentQuestion:
        now = self._now()
        with self._transaction() as transaction:
            previous = self._last_event(session_id)
            if previous.session.status != "open":
                raise ValueError("no enrichment question")
            snapshot, report = self._snapshot(transaction)
            previous = self._reassess(previous, snapshot, report, now)
            previous = self._terminal_if_needed(previous, now)
            session = previous.session
            selected = self._selected(report, snapshot, session)
            if (
                session.status != "open"
                or selected is None
                or selected.gap_id != session.current_gap_id
            ):
                raise ValueError("stale enrichment question")
            if self._wording is None:
                return selected
            rephrased = self._wording.rephrase(selected)
            if type(rephrased) is not EnrichmentQuestion:
                raise ValueError("invalid question wording")
            if type(rephrased.prompt) is not str or any(
                not _recursive_type_exact_equal(
                    getattr(selected, field_name), getattr(rephrased, field_name)
                )
                for field_name in EnrichmentQuestion.model_fields
                if field_name != "prompt"
            ):
                raise ValueError("invalid question wording")
            canonical = EnrichmentQuestion.model_validate_json(rephrased.model_dump_json())
            if not _recursive_type_exact_equal(canonical, rephrased):
                raise ValueError("invalid question wording")
            return canonical

    def next_question(self, session_id: str) -> EnrichmentQuestion:
        return cast(EnrichmentQuestion, self._public(lambda: self._question(session_id)))

    @staticmethod
    def _conversation_ref(session_id: str, gap_id: str) -> str:
        return f"enrichment:v1:{hashlib.sha256(_canonical_json([session_id, gap_id])).hexdigest()}"

    def _replayed_answer(
        self,
        previous: EnrichmentEvent,
        snapshot: AssessmentSnapshot,
        gap_id: str,
        answer: str,
    ) -> EnrichmentSession | None:
        session = previous.session
        if gap_id not in session.answered_gap_ids:
            return None
        index = session.answered_gap_ids.index(gap_id)
        evidence_ref = session.answer_evidence_refs[index]
        _record, content = validate_conversation_ingestion(
            snapshot.evidence,
            snapshot.ingestions,
            evidence_ref=evidence_ref,
            conversation_ref=self._conversation_ref(session.id, gap_id),
            author=self._actor,
            acl=(self._actor,),
            connector_id=self._capture.connector_id,
        )
        if content != answer:
            raise ValueError("divergent enrichment answer")
        events = self._runtime.enrichment_sessions.events(session.id)
        answered = tuple(
            (index, event)
            for index, event in enumerate(events)
            if event.event_type == "answered" and event.gap_id == gap_id
        )
        if len(answered) != 1 or answered[0][1].operation_id is None:
            raise ValueError("missing enrichment replay result")
        position, answer_event = answered[0]
        operation_id = answer_event.operation_id
        result = answer_event.session
        for event in events[position + 1 :]:
            if event.operation_id != operation_id:
                break
            result = event.session
        return result

    def _answer(self, session_id: str, gap_id: str, answer: str) -> EnrichmentSession:
        if (
            type(answer) is not str
            or not answer
            or len(answer.encode("utf-8")) > _MAX_ANSWER_BYTES
            or _ANSWER_CONTROL.search(answer)
        ):
            raise ValueError("invalid enrichment answer")
        now = self._now()
        operation_id = _digest(["answer", session_id, gap_id, _digest(answer)])
        stale = False
        result: EnrichmentSession | None = None
        with self._transaction() as transaction:
            previous = self._last_event(session_id)
            snapshot, report = self._snapshot(transaction)
            replayed = self._replayed_answer(previous, snapshot, gap_id, answer)
            if replayed is not None:
                return replayed
            if previous.session.status != "open":
                raise ValueError("inactive enrichment session")
            previous = self._reassess(previous, snapshot, report, now)
            if previous.session.current_gap_id != gap_id:
                previous = self._terminal_if_needed(previous, now)
                stale = True
            else:
                previous = self._terminal_if_needed(previous, now)
            if stale or previous.session.status == "complete":
                result = previous.session
            else:
                if previous.session.status != "open":
                    raise ValueError("stale enrichment answer")
                record = self._capture.record_turn(
                    conversation_ref=self._conversation_ref(session_id, gap_id),
                    role="human",
                    author=self._actor,
                    content=answer,
                    captured_at=now,
                    acl=(self._actor,),
                )
                resolved = previous.session.model_copy(
                    update={
                        "answered_gap_ids": (*previous.session.answered_gap_ids, gap_id),
                        "answer_evidence_refs": (*previous.session.answer_evidence_refs, record.id),
                        "current_gap_id": None,
                        "updated_at": now,
                    }
                )
                previous = self._append(
                    previous,
                    "answered",
                    resolved,
                    gap_id=gap_id,
                    answer_ref=record.id,
                    operation_id=operation_id,
                )
                refreshed_snapshot, refreshed_report = self._snapshot(transaction)
                previous = self._reassess(
                    previous,
                    refreshed_snapshot,
                    refreshed_report,
                    now,
                    operation_id,
                )
                result = self._terminal_if_needed(previous, now, operation_id).session
        if stale:
            raise ValueError("stale enrichment answer")
        if result is None:
            raise ValueError("missing enrichment result")
        return result

    def answer(self, session_id: str, gap_id: str, answer: str) -> EnrichmentSession:
        operation = lambda: self._answer(session_id, gap_id, answer)
        try:
            return cast(EnrichmentSession, self._public(operation))
        finally:
            operation = cast(Callable[[], EnrichmentSession], None)
            answer = ""

    def _skip(self, session_id: str, gap_id: str) -> EnrichmentSession:
        now = self._now()
        with self._transaction() as transaction:
            previous = self._last_event(session_id)
            if gap_id in previous.session.skipped_gap_ids:
                return previous.session
            if previous.session.status != "open":
                raise ValueError("inactive enrichment session")
            snapshot, report = self._snapshot(transaction)
            previous = self._reassess(previous, snapshot, report, now)
            previous = self._terminal_if_needed(previous, now)
            if previous.session.status == "complete":
                return previous.session
            if previous.session.status != "open" or previous.session.current_gap_id != gap_id:
                raise ValueError("stale enrichment skip")
            skipped = previous.session.model_copy(
                update={
                    "skipped_gap_ids": (*previous.session.skipped_gap_ids, gap_id),
                    "current_gap_id": None,
                    "updated_at": now,
                }
            )
            previous = self._append(previous, "skipped", skipped, gap_id=gap_id)
            previous = self._reassess(previous, snapshot, report, now)
            return self._terminal_if_needed(previous, now).session

    def skip(self, session_id: str, gap_id: str) -> EnrichmentSession:
        return cast(EnrichmentSession, self._public(lambda: self._skip(session_id, gap_id)))

    def _pause(self, session_id: str) -> EnrichmentSession:
        now = self._now()
        with self._transaction() as transaction:
            previous = self._last_event(session_id)
            if previous.session.status == "paused":
                return previous.session
            if previous.session.status != "open":
                raise ValueError("inactive enrichment session")
            snapshot, report = self._snapshot(transaction)
            previous = self._reassess(previous, snapshot, report, now)
            previous = self._terminal_if_needed(previous, now)
            if previous.session.status != "open":
                return previous.session
            paused = previous.session.model_copy(update={"status": "paused", "updated_at": now})
            return self._append(previous, "paused", paused).session

    def pause(self, session_id: str) -> EnrichmentSession:
        return cast(EnrichmentSession, self._public(lambda: self._pause(session_id)))

    def _resume(self, session_id: str) -> EnrichmentSession:
        now = self._now()
        with self._transaction() as transaction:
            previous = self._last_event(session_id)
            if previous.session.status == "open":
                return previous.session
            if previous.session.status != "paused":
                raise ValueError("inactive enrichment session")
            snapshot, report = self._snapshot(transaction)
            previous = self._reassess(previous, snapshot, report, now)
            resumed = previous.session.model_copy(update={"status": "open", "updated_at": now})
            previous = self._append(previous, "resumed", resumed)
            return self._terminal_if_needed(previous, now).session

    def resume(self, session_id: str) -> EnrichmentSession:
        return cast(EnrichmentSession, self._public(lambda: self._resume(session_id)))

    def _delegate_proposal(
        self,
        submission: ClarificationProposalSubmission,
        binding: EnrichmentProposalBinding,
    ) -> ClarificationIntentProposal:
        if self._proposal is None:  # pragma: no cover - checked by caller
            raise ValueError("proposal boundary unavailable")
        config: SecureFile | None = None
        policy: SecureFile | None = None
        result: ClarificationIntentProposal | None = None
        primary: BaseException | None = None
        cleanups: list[BaseException] = []
        try:
            try:
                config = self._runtime.workspace_directory.file("config.yaml")
                policy = self._runtime.workspace_directory.file("approvals/policy.yaml")
                result = self._proposal.propose_enrichment(
                    submission,
                    principals=frozenset({self._actor}),
                    binding=binding,
                    enrichment_store=self._runtime.enrichment_sessions,
                    authority_files={"config": config, "acl_policy": policy},
                    authority_read_policies=_AUTHORITY_POLICIES,
                )
            except BaseException as caught:  # noqa: BLE001 - select after all cleanup
                primary = caught
        finally:
            cleanups.extend(_close_authority_files(policy, config))
            config = None
            policy = None
        cleanup: BaseException | None
        for cleanup in cleanups:
            _scrub_signal(cleanup)
        cleanup = None
        cleanup_cancellation = next(
            (item for item in cleanups if not isinstance(item, Exception)), None
        )
        selected: BaseException | None
        if primary is not None and not isinstance(primary, Exception):
            selected = primary
        elif cleanup_cancellation is not None:
            if primary is not None:
                _scrub_suspended_signal(primary)
            selected = cleanup_cancellation
        else:
            selected = primary if primary is not None else cleanups[0] if cleanups else None
        primary = None
        cleanup_cancellation = None
        cleanups.clear()
        if selected is not None:
            _raise_signal(selected)
        if result is None:
            raise ValueError("proposal boundary unavailable")
        return result

    def _propose(
        self,
        session_id: str,
        submission: ClarificationProposalSubmission,
    ) -> ClarificationIntentProposal:
        if self._proposal is None or type(submission) is not ClarificationProposalSubmission:
            raise ValueError("proposal boundary unavailable")
        validated = ClarificationProposalSubmission.model_validate_json(
            submission.model_dump_json()
        )
        if validated != submission:
            raise ValueError("invalid enrichment proposal")
        with self._transaction() as transaction:
            previous = self._last_event(session_id)
            snapshot, report = self._snapshot(transaction)
            if previous.session.status in {"open", "paused"}:
                previous = self._reassess(previous, snapshot, report, self._now())
            elif previous.session.snapshot_digest != report.snapshot_digest:
                raise ValueError("stale enrichment proposal")
            session = previous.session
            visible = frozenset(item.id for item in snapshot.evidence)
            if (
                not session.answer_evidence_refs
                or not set(session.answer_evidence_refs).issubset(validated.evidence_refs)
                or not set(session.answer_evidence_refs).issubset(visible)
            ):
                raise ValueError("invalid enrichment proposal")
            for gap_id, evidence_ref in zip(
                session.answered_gap_ids, session.answer_evidence_refs, strict=True
            ):
                validate_conversation_ingestion(
                    snapshot.evidence,
                    snapshot.ingestions,
                    evidence_ref=evidence_ref,
                    conversation_ref=self._conversation_ref(session.id, gap_id),
                    author=self._actor,
                    acl=(self._actor,),
                    connector_id=self._capture.connector_id,
                )
            binding = EnrichmentProposalBinding(
                session_id=session.id,
                latest_event_digest=previous.digest,
                snapshot_digest=session.snapshot_digest,
                actor=self._actor,
                answered_gap_ids=session.answered_gap_ids,
                answer_evidence_refs=session.answer_evidence_refs,
                assessment_preimage_digests=tuple(
                    (name, _preimage_digest(transaction.read_optional(name)))
                    for name in (
                        "acl_policy",
                        "cases",
                        "config",
                        "evidence",
                        "graph",
                        "history",
                        "intent_proposals",
                    )
                ),
            )
        return self._delegate_proposal(validated, binding)

    def propose(
        self,
        session_id: str,
        submission: ClarificationProposalSubmission,
    ) -> ClarificationIntentProposal:
        operation = lambda: self._propose(session_id, submission)
        try:
            return cast(ClarificationIntentProposal, self._public(operation))
        finally:
            operation = cast(Callable[[], ClarificationIntentProposal], None)
            submission = cast(ClarificationProposalSubmission, None)

    @staticmethod
    def _public(operation: Callable[[], object]) -> object:
        result: object | None = None
        signal: BaseException | None = None
        failed = False
        try:
            result = operation()
        except Exception as caught:  # noqa: BLE001 - expose one fixed enrichment boundary
            _scrub_signal(caught)
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_signal(caught)
        finally:
            operation = cast(Callable[[], object], None)
        if signal is not None:
            detached = signal
            signal = None
            _raise_signal(detached)
        if failed or result is None:
            raise GraphEnrichmentError() from None
        return result


__all__ = [
    "EnrichmentQuestion",
    "GraphEnrichmentError",
    "GraphEnrichmentService",
    "ProposalPort",
    "QuestionWordingPort",
]
