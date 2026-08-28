"""Integration proof for attributed, evidence-backed clarification sessions."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.core.models import (
    ChangeSet,
    EvidenceRecord,
    Graph,
    Node,
    NodeType,
    NodeUpdate,
    ProjectConfig,
    SourceMode,
    SourceRole,
    SourceRoleAssignment,
)
from intent_engineering.intent_workflow.clarification import (
    ClarificationCoordinator,
    ClarificationError,
    ProposalConfirmationService,
)
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.models import (
    ClarificationProposalSubmission,
    ClarificationQuestionInput,
    ClarificationSession,
    TaskEnvelope,
)
from intent_engineering.intent_workflow.proposal_store import (
    IntentLedgerRecord,
    IntentProposalStore,
    IntentProposalStoreError,
    serialize_intent_ledger_record,
)
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore, parse_evidence_lines
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import YamlGraphStore

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
ALIASES = frozenset(
    {
        "local:asha",
        "local:ben",
        "github:asha",
        "slack:asha",
        "github:ben",
        "jira:ben",
        "product:priya",
        "agent:codex",
    }
)


def _base_evidence(*, acl: tuple[str, ...] = tuple(sorted(ALIASES))) -> EvidenceRecord:
    return EvidenceRecord(
        id="evidence:base",
        connector_type="markdown",
        external_object_id="docs/prd.md",
        external_version="v1",
        author="product:priya",
        observed_at=NOW - timedelta(days=1),
        source_locator="docs/prd.md",
        content_hash=hashlib.sha256(b"base").hexdigest(),
        payload={"content": "Reports stay local"},
        acl=acl,
    )


def _node(
    node_id: str,
    node_type: NodeType,
    label: str,
    *,
    source_mode: SourceMode = SourceMode.EXPLICIT,
    confidence: float = 0.95,
) -> Node:
    return Node(
        id=node_id,
        type=node_type,
        label=label,
        status="active",
        created_by="product:priya",
        created_at=NOW - timedelta(days=1),
        last_modified_by="product:priya",
        last_modified_at=NOW - timedelta(days=1),
        source_mode=source_mode,
        intent_fidelity_confidence=confidence,
        confidence_basis="Approved PRD",
        last_reassessed_at=NOW - timedelta(days=1),
        evidence_refs=("evidence:base",),
    )


@dataclass
class ClarificationHarness:
    coordinator: ClarificationCoordinator
    confirmation: ProposalConfirmationService
    capture: ConversationCapture
    graph_store: YamlGraphStore
    evidence_store: JsonlEvidenceStore
    case_store: JsonlCaseStore
    proposal_store: IntentProposalStore
    transactions: LocalTransactionCoordinator
    paths: dict[str, Path]
    directory: SecureDirectory
    task: TaskEnvelope
    classification_evidence_ref: str

    @property
    def principals(self) -> frozenset[str]:
        return ALIASES

    def open(self) -> ClarificationSession:
        return self.coordinator.open(
            self.task,
            classification_evidence_ref=self.classification_evidence_ref,
            questions=(
                ClarificationQuestionInput(
                    id="audience", prompt="Who may share reports?", required=True
                ),
                ClarificationQuestionInput(
                    id="expiry", prompt="Should shared reports expire?", required=False
                ),
            ),
            opened_by="agent:codex",
            opened_at=NOW + timedelta(microseconds=2),
            principals=self.principals,
        )

    def answer_required(self, session: ClarificationSession) -> ClarificationSession:
        return self.coordinator.answer(
            session.id,
            actor="local:asha",
            question_id="audience",
            answer="Workspace admins may share read-only reports [answer-only marker]",
            answered_at=NOW + timedelta(microseconds=5),
            acl=tuple(sorted(self.principals)),
            principals=self.principals,
        )

    def submission(
        self,
        session: ClarificationSession,
        *,
        conflict: bool = False,
        conflicting_authors: tuple[str, ...] = (),
    ) -> ClarificationProposalSubmission:
        timestamp = NOW + timedelta(microseconds=6)
        evidence_refs = (
            session.request_evidence_ref,
            session.classification_evidence_ref,
            *(item.evidence_ref for item in session.questions),
            *(item.evidence_ref for item in session.answers),
        )
        if conflict:
            current = self.graph_store.load().nodes[1]
            replacement = current.model_copy(
                update={
                    "label": "Upload raw conversations for sharing",
                    "last_modified_by": "local:asha",
                    "last_modified_at": timestamp,
                    "evidence_refs": (*current.evidence_refs, *evidence_refs),
                }
            )
            nodes_added = ()
            nodes_updated = (NodeUpdate(node_id=current.id, replacement=replacement),)
            core_node_ids: tuple[str, ...] = ()
        else:
            proposed = Node(
                id="req-read-only-sharing",
                type=NodeType.REQUIREMENT,
                label="Workspace admins may share read-only reports",
                status="proposed",
                created_by="local:asha",
                created_at=timestamp,
                last_modified_by="local:asha",
                last_modified_at=timestamp,
                source_mode=SourceMode.INFERRED,
                intent_fidelity_confidence=0.8,
                confidence_basis="Clarified conversation",
                last_reassessed_at=timestamp,
                evidence_refs=evidence_refs,
            )
            nodes_added = (proposed,)
            nodes_updated = ()
            core_node_ids = (proposed.id,)
        changeset = ChangeSet(
            id="",
            actor="local:asha",
            timestamp=timestamp,
            baseline_graph_version=session.baseline_graph_version,
            evidence_refs=evidence_refs,
            nodes_added=nodes_added,
            nodes_updated=nodes_updated,
            nodes_superseded=(),
            edges_added=(),
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(),
            validation_status="validated",
        )
        return ClarificationProposalSubmission(
            session_id=session.id,
            task_id=session.task_id,
            baseline_graph_version=session.baseline_graph_version,
            actor="local:asha",
            timestamp=timestamp,
            evidence_refs=evidence_refs,
            changeset=changeset,
            core_node_ids=core_node_ids,
            conflicting_authors=conflicting_authors,
        )

    def propose(self, *, conflict: bool = False, conflicting_authors: tuple[str, ...] = ()):
        session = self.answer_required(self.open())
        return self.coordinator.propose(
            self.submission(
                session,
                conflict=conflict,
                conflicting_authors=conflicting_authors,
            ),
            principals=self.principals,
        )

    def state_bytes(self) -> dict[str, bytes | None]:
        return {
            name: path.read_bytes() if path.exists() else None
            for name, path in self.paths.items()
        }


def _policy_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "contributors": ["local:asha"],
        "approvers": ["local:ben"],
        "executors": ["local:ben"],
        "identities": {
            "local:asha": ["github:asha", "local:asha", "slack:asha"],
            "local:ben": ["github:ben", "jira:ben", "local:ben"],
            "product:priya": ["product:priya"],
        },
    }


def _harness(
    tmp_path: Path,
    *,
    requirement_source_mode: SourceMode = SourceMode.EXPLICIT,
    requirement_confidence: float = 0.95,
    current_evidence_acl: tuple[str, ...] = tuple(sorted(ALIASES)),
) -> ClarificationHarness:
    workspace = tmp_path / ".intent"
    for name in ("history", "reconciliation", "evidence", "approvals"):
        (workspace / name).mkdir(parents=True, exist_ok=True)
    paths = {
        "config": workspace / "config.yaml",
        "policy": workspace / "approvals/policy.yaml",
        "graph": workspace / "graph.yaml",
        "history": workspace / "history/changesets.jsonl",
        "cases": workspace / "reconciliation/cases.jsonl",
        "evidence": workspace / "evidence/evidence.jsonl",
        "receipts": workspace / "approvals/receipts.jsonl",
        "intent_proposals": workspace / "history/intent-proposals.jsonl",
        "journal": workspace / "history/.local-transaction.json",
    }
    config = ProjectConfig(project_id="demo", local_actor="local:asha")
    paths["config"].write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    paths["policy"].write_text(yaml.safe_dump(_policy_payload(), sort_keys=True), encoding="utf-8")
    for name, path in paths.items():
        if name not in {"config", "policy", "graph", "journal"}:
            path.write_bytes(b"")
    directory = SecureDirectory.open(workspace)
    files = {
        "graph": directory.file("graph.yaml"),
        "history": directory.file("history/changesets.jsonl"),
        "cases": directory.file("reconciliation/cases.jsonl"),
        "evidence": directory.file("evidence/evidence.jsonl"),
        "receipts": directory.file("approvals/receipts.jsonl"),
        "intent_proposals": directory.file("history/intent-proposals.jsonl"),
    }
    transactions = LocalTransactionCoordinator(directory.file("history/.local-transaction.json"), files)
    graph_store = YamlGraphStore(files["graph"], history_path=files["history"], transactions=transactions)
    graph_store.initialize(
        Graph(
            id="graph:demo",
            version=2,
            name="demo",
            nodes=(
                _node("intent-local", NodeType.PRODUCT_INTENT, "Keep source content local"),
                _node(
                    "req-local",
                    NodeType.REQUIREMENT,
                    "Raw source content stays local",
                    source_mode=requirement_source_mode,
                    confidence=requirement_confidence,
                ),
            ),
            edges=(),
        )
    )
    evidence_store = JsonlEvidenceStore(files["evidence"], transactions=transactions)
    evidence_store.associate("markdown", _base_evidence(acl=current_evidence_acl))
    capture = ConversationCapture(evidence_store)
    request = capture.record_turn(
        conversation_ref="codex:thread-6",
        role="human",
        author="local:asha",
        content="Add team sharing",
        captured_at=NOW,
        acl=tuple(sorted(ALIASES)),
    )
    classification = capture.record_turn(
        conversation_ref="codex:thread-6",
        role="agent",
        author="agent:codex",
        content={"classification": "new_or_ambiguous", "questions_required": True},
        captured_at=NOW + timedelta(microseconds=1),
        acl=tuple(sorted(ALIASES)),
    )
    task = TaskEnvelope(
        repository_id="demo",
        actor="local:asha",
        conversation_ref="codex:thread-6",
        request="Add team sharing",
        request_evidence_ref=request.id,
        graph_version=2,
        created_at=NOW,
    )
    proposal_store = IntentProposalStore(files["intent_proposals"], transactions=transactions)
    case_store = JsonlCaseStore(files["cases"])
    executor = LocalChangeSetExecutor(graph_store, case_store, transactions)
    coordinator = ClarificationCoordinator(
        graph_store=graph_store,
        evidence_store=evidence_store,
        proposal_store=proposal_store,
        transactions=transactions,
        config=config,
        capture=capture,
    )
    confirmation = ProposalConfirmationService(
        graph_store=graph_store,
        evidence_store=evidence_store,
        case_store=case_store,
        proposal_store=proposal_store,
        changeset_executor=executor,
        transactions=transactions,
        config_file=directory.file("config.yaml"),
        policy_file=directory.file("approvals/policy.yaml"),
    )
    return ClarificationHarness(
        coordinator,
        confirmation,
        capture,
        graph_store,
        evidence_store,
        case_store,
        proposal_store,
        transactions,
        paths,
        directory,
        task,
        classification.id,
    )


@pytest.fixture
def clarification_harness(tmp_path: Path) -> ClarificationHarness:
    return _harness(tmp_path)


def test_open_answer_propose_preserves_authors_evidence_and_exact_chronology(
    clarification_harness: ClarificationHarness,
) -> None:
    opened = clarification_harness.open()
    answered = clarification_harness.answer_required(opened)
    proposal = clarification_harness.coordinator.propose(
        clarification_harness.submission(answered),
        principals=clarification_harness.principals,
    )

    assert tuple(item.author for item in opened.questions) == ("agent:codex", "agent:codex")
    assert answered.answers[0].actor == "local:asha"
    assert proposal.proposed_by == "local:asha"
    assert proposal.evidence_refs == (
        opened.request_evidence_ref,
        opened.classification_evidence_ref,
        *(item.evidence_ref for item in opened.questions),
        answered.answers[0].evidence_ref,
    )
    events = clarification_harness.proposal_store.clarification_events(opened.id)
    assert tuple(item.event_type for item in events) == ("opened", "answered", "proposed")
    assert tuple(item.predecessor_event_id for item in events) == (None, events[0].id, events[1].id)
    assert events[-1].proposal_id == proposal.id
    _, ingestions, _ = parse_evidence_lines(clarification_harness.paths["evidence"].read_bytes())
    conversation = tuple(item for item in ingestions if item.connector_id == "conversation:agent")
    assert tuple(item.predecessor_id for item in conversation) == (
        None,
        conversation[0].evidence.id,
        conversation[1].evidence.id,
        conversation[2].evidence.id,
        conversation[3].evidence.id,
    )


def test_raw_questions_and_answers_exist_only_in_immutable_evidence(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose()
    ledger = clarification_harness.paths["intent_proposals"].read_bytes()
    evidence = clarification_harness.paths["evidence"].read_bytes()
    assert b"Who may share reports?" not in ledger
    assert b"[answer-only marker]" not in ledger
    assert b"Who may share reports?" in evidence
    assert b"[answer-only marker]" in evidence
    assert proposal.clarification_session_id.startswith("clarification:sha256:")


def test_required_answer_is_enforced_but_optional_answer_may_remain_open(
    clarification_harness: ClarificationHarness,
) -> None:
    session = clarification_harness.open()
    before = clarification_harness.state_bytes()
    with pytest.raises(ClarificationError) as caught:
        clarification_harness.coordinator.propose(
            clarification_harness.submission(session),
            principals=clarification_harness.principals,
        )
    assert caught.value.args == ("intent clarification unavailable",)
    assert caught.value.__context__ is None
    assert clarification_harness.state_bytes() == before
    answered = clarification_harness.answer_required(session)
    proposal = clarification_harness.coordinator.propose(
        clarification_harness.submission(answered),
        principals=clarification_harness.principals,
    )
    assert proposal.unanswered_questions == ()
    assert {item.question_id for item in answered.answers} == {"audience"}


def test_identical_answer_replay_is_a_byte_exact_noop(
    clarification_harness: ClarificationHarness,
) -> None:
    session = clarification_harness.open()
    first = clarification_harness.answer_required(session)
    before = clarification_harness.state_bytes()
    second = clarification_harness.answer_required(session)
    assert second == first
    assert clarification_harness.state_bytes() == before


@pytest.mark.parametrize("answer", ["", "x" * (16 * 1024 + 1)])
def test_answer_bounds_fail_before_ledger_semantic_use(
    clarification_harness: ClarificationHarness,
    answer: str,
) -> None:
    session = clarification_harness.open()
    before = clarification_harness.state_bytes()
    with pytest.raises(ClarificationError):
        clarification_harness.coordinator.answer(
            session.id,
            actor="local:asha",
            question_id="audience",
            answer=answer,
            answered_at=NOW + timedelta(microseconds=5),
            acl=tuple(sorted(clarification_harness.principals)),
            principals=clarification_harness.principals,
        )
    assert clarification_harness.state_bytes() == before


def test_identical_open_replay_is_a_byte_exact_noop(
    clarification_harness: ClarificationHarness,
) -> None:
    first = clarification_harness.open()
    before = clarification_harness.state_bytes()
    second = clarification_harness.open()
    assert second == first
    assert clarification_harness.state_bytes() == before


@pytest.mark.parametrize("variant", ["duplicate", "too-many", "non-utc", "stale-task"])
def test_opening_bounds_identity_time_and_baseline_fail_before_capture(
    clarification_harness: ClarificationHarness,
    variant: str,
) -> None:
    questions = (
        ClarificationQuestionInput(id="one", prompt="First question?"),
        ClarificationQuestionInput(id="two", prompt="Second question?"),
    )
    opened_at = NOW + timedelta(microseconds=2)
    task = clarification_harness.task
    if variant == "duplicate":
        questions = (questions[0], questions[0])
    elif variant == "too-many":
        questions = tuple(
            ClarificationQuestionInput(id=f"q{index}", prompt=f"Question {index}?")
            for index in range(17)
        )
    elif variant == "non-utc":
        opened_at = opened_at.replace(tzinfo=None)
    else:
        task = task.model_copy(update={"graph_version": 1})
    before = clarification_harness.state_bytes()
    with pytest.raises(ClarificationError):
        clarification_harness.coordinator.open(
            task,
            classification_evidence_ref=clarification_harness.classification_evidence_ref,
            questions=questions,
            opened_by="agent:codex",
            opened_at=opened_at,
            principals=clarification_harness.principals,
        )
    assert clarification_harness.state_bytes() == before


def test_divergent_answer_is_durably_associated_as_a_conflict_and_blocks_proposal(
    clarification_harness: ClarificationHarness,
) -> None:
    session = clarification_harness.open()
    answered = clarification_harness.answer_required(session)
    conflicted = clarification_harness.coordinator.answer(
        session.id,
        actor="local:asha",
        question_id="audience",
        answer="A divergent private answer",
        answered_at=NOW + timedelta(microseconds=6),
        acl=tuple(sorted(clarification_harness.principals)),
        principals=clarification_harness.principals,
    )
    assert conflicted.answers == answered.answers
    assert len(conflicted.conflicts) == 1
    conflict = conflicted.conflicts[0]
    assert conflict.question_id == "audience"
    assert conflict.original_evidence_ref == answered.answers[0].evidence_ref
    assert conflict.predecessor_evidence_ref == answered.answers[0].evidence_ref
    assert conflict.conflicting_evidence_ref != conflict.original_evidence_ref
    events = clarification_harness.proposal_store.clarification_events(session.id)
    assert tuple(item.event_type for item in events) == ("opened", "answered", "conflicted")
    assert events[-1].predecessor_event_id == events[-2].id
    with pytest.raises(ClarificationError):
        clarification_harness.coordinator.propose(
            clarification_harness.submission(conflicted),
            principals=clarification_harness.principals,
        )


def test_identical_conflict_replay_is_noop_but_third_answer_is_new_conflict(
    clarification_harness: ClarificationHarness,
) -> None:
    answered = clarification_harness.answer_required(clarification_harness.open())
    kwargs = {
        "actor": "local:asha",
        "question_id": "audience",
        "answer": "A divergent private answer",
        "answered_at": NOW + timedelta(microseconds=6),
        "acl": tuple(sorted(clarification_harness.principals)),
        "principals": clarification_harness.principals,
    }
    first = clarification_harness.coordinator.answer(answered.id, **kwargs)
    before = clarification_harness.state_bytes()
    replay = clarification_harness.coordinator.answer(answered.id, **kwargs)
    assert replay == first
    assert clarification_harness.state_bytes() == before

    third = clarification_harness.coordinator.answer(
        answered.id,
        actor="local:asha",
        question_id="audience",
        answer="A third distinct private answer",
        answered_at=NOW + timedelta(microseconds=7),
        acl=tuple(sorted(clarification_harness.principals)),
        principals=clarification_harness.principals,
    )
    assert len(third.conflicts) == 2
    assert third.conflicts[-1].predecessor_evidence_ref == (
        first.conflicts[-1].conflicting_evidence_ref
    )


def test_revision_of_earlier_question_follows_latest_answer_predecessor(
    clarification_harness: ClarificationHarness,
) -> None:
    first = clarification_harness.answer_required(clarification_harness.open())
    second = clarification_harness.coordinator.answer(
        first.id,
        actor="local:asha",
        question_id="expiry",
        answer="Shared reports expire after seven days",
        answered_at=NOW + timedelta(microseconds=6),
        acl=tuple(sorted(clarification_harness.principals)),
        principals=clarification_harness.principals,
    )
    revised = clarification_harness.coordinator.answer(
        second.id,
        actor="local:asha",
        question_id="audience",
        answer="Only security administrators may share reports",
        answered_at=NOW + timedelta(microseconds=7),
        acl=tuple(sorted(clarification_harness.principals)),
        principals=clarification_harness.principals,
    )
    assert revised.conflicts[0].original_evidence_ref == first.answers[0].evidence_ref
    assert revised.conflicts[0].predecessor_evidence_ref == second.answers[-1].evidence_ref


def test_answer_time_must_follow_the_exact_question_chronology(
    clarification_harness: ClarificationHarness,
) -> None:
    session = clarification_harness.open()
    before = clarification_harness.state_bytes()
    with pytest.raises(ClarificationError):
        clarification_harness.coordinator.answer(
            session.id,
            actor="local:asha",
            question_id="audience",
            answer="Chronologically impossible answer",
            answered_at=NOW + timedelta(microseconds=2),
            acl=tuple(sorted(clarification_harness.principals)),
            principals=clarification_harness.principals,
        )
    assert clarification_harness.state_bytes() == before


def test_two_identical_concurrent_proposals_converge_on_one_typed_frame(
    clarification_harness: ClarificationHarness,
) -> None:
    session = clarification_harness.answer_required(clarification_harness.open())
    submission = clarification_harness.submission(session)

    def propose() -> object:
        return clarification_harness.coordinator.propose(
            submission, principals=clarification_harness.principals
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _index: propose(), range(2)))
    assert results[0] == results[1]
    assert clarification_harness.proposal_store.bytes().count(b'"proposal":{') == 1


def test_stale_session_cannot_propose_after_an_independent_graph_activation(
    clarification_harness: ClarificationHarness,
) -> None:
    stale = clarification_harness.answer_required(clarification_harness.open())
    external = clarification_harness.submission(stale).changeset.model_copy(
        update={"id": "changeset:external-activation"}
    )
    clarification_harness.graph_store.apply(external)
    before = clarification_harness.state_bytes()
    with pytest.raises(ClarificationError):
        clarification_harness.coordinator.propose(
            clarification_harness.submission(stale),
            principals=clarification_harness.principals,
        )
    assert clarification_harness.state_bytes() == before


@pytest.mark.parametrize("forgery", ["candidate-author", "unconfigured-source-role"])
def test_typed_submission_provenance_and_source_roles_fail_closed(
    clarification_harness: ClarificationHarness,
    forgery: str,
) -> None:
    session = clarification_harness.answer_required(clarification_harness.open())
    submission = clarification_harness.submission(session)
    if forgery == "candidate-author":
        forged = submission.changeset.nodes_added[0].model_copy(
            update={"created_by": "local:ben"}
        )
        submission = submission.model_copy(
            update={
                "changeset": submission.changeset.model_copy(
                    update={"nodes_added": (forged,)}
                )
            }
        )
    else:
        submission = submission.model_copy(
            update={
                "source_roles": (
                    SourceRoleAssignment(
                        connector_id="conversation:agent",
                        scope="codex:thread-6",
                        role=SourceRole.PROPOSED_INTENT,
                        inherited=False,
                    ),
                )
            }
        )
    before = clarification_harness.state_bytes()
    with pytest.raises(ClarificationError):
        clarification_harness.coordinator.propose(
            submission, principals=clarification_harness.principals
        )
    assert clarification_harness.state_bytes() == before


def test_existing_semantic_update_cannot_erase_or_reclassify_authoritative_provenance(
    clarification_harness: ClarificationHarness,
) -> None:
    session = clarification_harness.answer_required(clarification_harness.open())
    submission = clarification_harness.submission(session, conflict=True)
    replacement = submission.changeset.nodes_updated[0].replacement.model_copy(
        update={
            "source_mode": SourceMode.INFERRED,
            "intent_fidelity_confidence": 0.4,
            "confidence_basis": "Clarification replaced authority",
            "last_reassessed_at": submission.timestamp,
            "evidence_refs": submission.evidence_refs,
        }
    )
    forged = submission.model_copy(
        update={
            "changeset": submission.changeset.model_copy(
                update={
                    "nodes_updated": (
                        NodeUpdate(node_id=replacement.id, replacement=replacement),
                    )
                }
            )
        }
    )
    before = clarification_harness.state_bytes()
    with pytest.raises(ClarificationError):
        clarification_harness.coordinator.propose(
            forged, principals=clarification_harness.principals
        )
    assert clarification_harness.state_bytes() == before


def _repository_traceback_locals(error: BaseException) -> str:
    frames: list[str] = []
    current = error.__traceback__
    while current is not None:
        if "/src/intent_engineering/" in current.tb_frame.f_code.co_filename:
            frames.append(repr(current.tb_frame.f_locals))
        current = current.tb_next
    return "\n".join(frames)


def test_fixed_proposal_failure_drops_raw_candidate_from_traceback(
    clarification_harness: ClarificationHarness,
) -> None:
    session = clarification_harness.answer_required(clarification_harness.open())
    submission = clarification_harness.submission(session)
    secret = "PRIVATE-CLARIFICATION-CANDIDATE-8197"
    forged = submission.changeset.nodes_added[0].model_copy(
        update={"label": secret, "created_by": "local:ben"}
    )
    hostile = submission.model_copy(
        update={
            "changeset": submission.changeset.model_copy(update={"nodes_added": (forged,)})
        }
    )
    with pytest.raises(ClarificationError) as caught:
        clarification_harness.coordinator.propose(
            hostile, principals=clarification_harness.principals
        )
    assert caught.value.__context__ is None
    assert secret not in _repository_traceback_locals(caught.value)


@pytest.mark.parametrize("attack", ["missing", "reordered", "mismatched", "duplicate"])
def test_proposed_event_requires_exactly_one_matching_next_proposal_frame_without_rewrite(
    clarification_harness: ClarificationHarness,
    attack: str,
) -> None:
    proposal = clarification_harness.propose()
    path = clarification_harness.paths["intent_proposals"]
    lines = path.read_bytes().splitlines(keepends=True)
    assert len(lines) == 4
    if attack == "missing":
        hostile = b"".join(lines[:-1])
    elif attack == "reordered":
        hostile = b"".join((*lines[:2], lines[3], lines[2]))
    elif attack == "duplicate":
        hostile = b"".join(lines) + serialize_intent_ledger_record(
            IntentLedgerRecord(sequence=4, proposal=proposal)
        )
    else:
        draft = proposal.model_copy(
            update={
                "clarification_session_id": "clarification:sha256:" + "0" * 64,
                "id": "",
            }
        )
        mismatch = draft.model_copy(update={"id": f"proposal:{draft.digest}"})
        hostile = b"".join(lines[:-1]) + serialize_intent_ledger_record(
            IntentLedgerRecord(sequence=3, proposal=mismatch)
        )
    path.write_bytes(hostile)
    before = path.read_bytes()
    with pytest.raises(IntentProposalStoreError):
        clarification_harness.proposal_store.list()
    assert path.read_bytes() == before
