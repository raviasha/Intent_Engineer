"""Integration coverage for deterministic evidence-backed graph enrichment."""

from __future__ import annotations

import hashlib
import json
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Literal, cast

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.cli.runtime import Runtime, load_runtime
from intent_engineering.core.models import (
    ChangeSet,
    Edge,
    EvidenceIngestion,
    EvidenceRecord,
    Graph,
    Node,
    NodeType,
    ProjectConfig,
    RelationType,
    SourceMode,
)
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow import enrichment as enrichment_module
from intent_engineering.intent_workflow.clarification import ClarificationCoordinator
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.enrichment import (
    EnrichmentQuestion,
    GraphEnrichmentError,
    GraphEnrichmentService,
)
from intent_engineering.intent_workflow.enrichment_models import (
    EnrichmentProposalBinding,
    EnrichmentSession,
)
from intent_engineering.intent_workflow.enrichment_store import EnrichmentSessionStore
from intent_engineering.intent_workflow.models import (
    ClarificationProposalSubmission,
    ClarificationQuestionInput,
    TaskEnvelope,
)
from intent_engineering.storage.jsonl.history_store import serialize_changeset
from intent_engineering.storage.secure import SecureFile
from intent_engineering.storage.transaction import LocalTransactionExtraReadPolicy
from intent_engineering.storage.yaml.graph_store import serialize_graph

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _evidence() -> EvidenceRecord:
    marker = "Visible product intent"
    return EvidenceRecord(
        id="evidence:intent",
        connector_type="markdown",
        external_object_id="docs/intent.md",
        external_version="1",
        author="local:asha",
        observed_at=NOW,
        source_locator="docs/intent.md",
        content_hash=hashlib.sha256(marker.encode()).hexdigest(),
        payload={"content": marker},
        acl=("local:asha",),
    )


def _graph(evidence_ref: str) -> Graph:
    common = {
        "status": "active",
        "created_by": "local:asha",
        "created_at": NOW,
        "last_modified_by": "local:asha",
        "last_modified_at": NOW,
        "source_mode": SourceMode.EXPLICIT,
        "intent_fidelity_confidence": 0.95,
        "confidence_basis": "human evidence",
        "last_reassessed_at": NOW,
        "evidence_refs": (evidence_ref,),
    }
    return Graph(
        id="graph:enrichment",
        version=1,
        nodes=(
            Node(id="intent:product", type=NodeType.PRODUCT_INTENT, label="Product", **common),
            Node(id="req:owners", type=NodeType.REQUIREMENT, label="Owners", **common),
        ),
        edges=(
            Edge(
                id="edge:refines",
                from_id="intent:product",
                relation=RelationType.REFINES,
                to_id="req:owners",
                status="active",
                created_by="local:asha",
                created_at=NOW,
                last_modified_by="local:asha",
                last_modified_at=NOW,
            ),
        ),
    )


def _evidence_bytes(record: EvidenceRecord) -> bytes:
    ingestion = EvidenceIngestion(
        connector_id="markdown", sequence=1, predecessor_id=None, evidence=record
    )
    return (
        json.dumps(
            ingestion.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
    )


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    project = tmp_path / "project"
    project.mkdir()
    initialized = initialize_project(project)
    config = ProjectConfig(project_id="project:enrichment", local_actor="local:asha")
    initialized.config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    evidence = _evidence()
    initialized.graph_path.write_bytes(serialize_graph(_graph(evidence.id)))
    (initialized.workspace / "evidence/evidence.jsonl").write_bytes(_evidence_bytes(evidence))
    (initialized.workspace / "approvals/policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": ["local:asha"],
                "approvers": ["local:asha"],
                "executors": ["local:asha"],
                "identities": {"local:asha": ["local:asha"]},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    loaded = load_runtime(project)
    try:
        yield loaded
    finally:
        loaded.close()


def _service(runtime: Runtime, clock: Clock) -> GraphEnrichmentService:
    return GraphEnrichmentService(runtime, actor="local:asha", clock=clock)


def _target_bytes(runtime: Runtime, name: str) -> bytes:
    target = runtime.transactions.target_file(name)
    try:
        return target.read_optional() or b""
    finally:
        target.close()


def _clarification_proposal(
    runtime: Runtime,
    enrichment_session: EnrichmentSession,
) -> tuple[ClarificationCoordinator, ClarificationProposalSubmission]:
    session = enrichment_session
    capture = ConversationCapture(runtime.evidence_store)
    conversation_ref = "conversation:enrichment-proposal"
    request = capture.record_turn(
        conversation_ref=conversation_ref,
        role="human",
        author="local:asha",
        content="Turn the enrichment answer into a requirement",
        captured_at=NOW + timedelta(seconds=1),
        acl=("local:asha",),
    )
    classification = capture.record_turn(
        conversation_ref=conversation_ref,
        role="agent",
        author="local:asha",
        content={"classification": "new_or_ambiguous"},
        captured_at=NOW + timedelta(seconds=2),
        acl=("local:asha",),
    )
    task = TaskEnvelope(
        repository_id=runtime.config.project_id,
        actor="local:asha",
        conversation_ref=conversation_ref,
        request="Turn the enrichment answer into a requirement",
        request_evidence_ref=request.id,
        graph_version=runtime.graph_store.load().version,
        created_at=request.observed_at,
    )
    coordinator = ClarificationCoordinator(
        graph_store=runtime.graph_store,
        evidence_store=runtime.evidence_store,
        proposal_store=runtime.intent_proposals,
        transactions=runtime.transactions,
        config=runtime.config,
        capture=capture,
    )
    clarification = coordinator.open(
        task,
        classification_evidence_ref=classification.id,
        questions=(
            ClarificationQuestionInput(
                id="requirement", prompt="What requirement should be proposed?"
            ),
        ),
        opened_by="local:asha",
        opened_at=NOW + timedelta(seconds=3),
        principals=frozenset({"local:asha"}),
    )
    clarification = coordinator.answer(
        clarification.id,
        actor="local:asha",
        question_id="requirement",
        answer="Add an explicit workspace owner requirement",
        answered_at=NOW + timedelta(seconds=4),
        acl=("local:asha",),
        principals=frozenset({"local:asha"}),
    )
    evidence_refs = tuple(
        dict.fromkeys(
            (
                clarification.request_evidence_ref,
                clarification.classification_evidence_ref,
                *(item.evidence_ref for item in clarification.questions),
                *(item.evidence_ref for item in clarification.answers),
                *session.answer_evidence_refs,
            )
        )
    )
    timestamp = NOW + timedelta(seconds=5)
    node = Node(
        id="req:explicit-workspace-owner",
        type=NodeType.REQUIREMENT,
        label="Workspace changes have an explicit owner",
        status="proposed",
        created_by="local:asha",
        created_at=timestamp,
        last_modified_by="local:asha",
        last_modified_at=timestamp,
        source_mode=SourceMode.INFERRED,
        intent_fidelity_confidence=0.8,
        confidence_basis="Clarification and enrichment evidence",
        last_reassessed_at=timestamp,
        evidence_refs=evidence_refs,
    )
    changeset = ChangeSet(
        id="",
        actor="local:asha",
        timestamp=timestamp,
        baseline_graph_version=clarification.baseline_graph_version,
        evidence_refs=evidence_refs,
        nodes_added=(node,),
        nodes_updated=(),
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
    return coordinator, ClarificationProposalSubmission(
        session_id=clarification.id,
        task_id=clarification.task_id,
        baseline_graph_version=clarification.baseline_graph_version,
        actor="local:asha",
        timestamp=timestamp,
        evidence_refs=evidence_refs,
        changeset=changeset,
        core_node_ids=(node.id,),
    )


def test_next_question_is_stable_and_answer_is_immediate_evidence(runtime: Runtime) -> None:
    clock = Clock()
    service = _service(runtime, clock)
    graph_before = runtime.transactions.target_file("graph")
    history_before = runtime.transactions.target_file("history")
    try:
        graph_bytes = graph_before.read_bytes()
        history_bytes = history_before.read_optional() or b""
    finally:
        graph_before.close()
        history_before.close()

    opened = service.start(minutes=5)
    question = service.next_question(opened.id)
    assert opened.current_gap_id == question.gap_id
    assert question == _service(runtime, clock).next_question(opened.id)
    assert question.gap_id == (
        "gap:v1:de05119ca5bf3d19137d7fd5a2399eec0c9eda5dced644e106843dc3f25630c1"
    )
    assert question.node_id == "intent:product"
    assert question.rule_id == "rubric:v1:test_verification:no_current_test_evidence"
    assert question.requested_fields
    answered = service.answer(opened.id, question.gap_id, "Workspace owners")

    assert answered.answered_gap_ids == (question.gap_id,)
    assert len(answered.answer_evidence_refs) == 1
    record = {item.id: item for item in runtime.evidence_store.list()}[
        answered.answer_evidence_refs[0]
    ]
    assert record.author == "local:asha"
    assert record.payload == {"role": "human", "content": "Workspace owners"}
    graph_after = runtime.transactions.target_file("graph")
    history_after = runtime.transactions.target_file("history")
    try:
        assert graph_after.read_bytes() == graph_bytes
        assert (history_after.read_optional() or b"") == history_bytes
    finally:
        graph_after.close()
        history_after.close()


def test_exact_answer_replay_converges_and_divergence_fails_fixed(runtime: Runtime) -> None:
    clock = Clock()
    service = _service(runtime, clock)
    opened = service.start(minutes=5)
    assert opened.current_gap_id is not None
    answered_gap = opened.current_gap_id
    first = service.answer(opened.id, answered_gap, "Workspace owners")
    evidence_after = _target_bytes(runtime, "evidence")
    sessions_after = _target_bytes(runtime, "enrichment_sessions")

    assert first.current_gap_id is not None
    evolved = service.skip(first.id, first.current_gap_id)
    assert evolved != first
    evolved_sessions = _target_bytes(runtime, "enrichment_sessions")
    service = _service(runtime, clock)

    assert service.answer(opened.id, answered_gap, "Workspace owners") == first
    assert _target_bytes(runtime, "evidence") == evidence_after
    assert _target_bytes(runtime, "enrichment_sessions") == evolved_sessions
    assert evolved_sessions != sessions_after
    assert b"Workspace owners" not in sessions_after
    with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$"):
        service.answer(opened.id, answered_gap, "Different owners")


def test_enrichment_delegates_to_real_governed_proposal_without_applying(
    runtime: Runtime,
) -> None:
    clock = Clock()
    service = _service(runtime, clock)
    opened = service.start(minutes=5)
    assert opened.current_gap_id is not None
    answered = service.answer(opened.id, opened.current_gap_id, "Workspace owners")
    coordinator, submission = _clarification_proposal(runtime, answered)
    graph_before = _target_bytes(runtime, "graph")
    service = GraphEnrichmentService(
        runtime,
        actor="local:asha",
        clock=clock,
        proposal_service=coordinator,
    )

    proposal = service.propose(answered.id, submission)

    assert proposal.changeset.nodes_added[0].id == "req:explicit-workspace-owner"
    assert answered.answer_evidence_refs[0] in proposal.evidence_refs
    assert runtime.intent_proposals.get(proposal.id) == proposal
    assert _target_bytes(runtime, "graph") == graph_before


def test_enrichment_works_with_governed_coordinator_authority(runtime: Runtime) -> None:
    clock = Clock()
    base = _service(runtime, clock)
    opened = base.start(minutes=5)
    assert opened.current_gap_id is not None
    answered = base.answer(opened.id, opened.current_gap_id, "Workspace owners")
    _coordinator, submission = _clarification_proposal(runtime, answered)
    files = {
        "authority_config": runtime.workspace_directory.file("config.yaml"),
        "authority_policy": runtime.workspace_directory.file("approvals/policy.yaml"),
    }
    policy = LocalTransactionExtraReadPolicy(
        max_bytes=1024 * 1024,
        nonblocking_regular=True,
    )
    policies = {name: policy for name in files}
    snapshot = runtime.transactions.snapshot(files, extra_read_policies=policies)
    governed = ClarificationCoordinator(
        graph_store=runtime.graph_store,
        evidence_store=runtime.evidence_store,
        proposal_store=runtime.intent_proposals,
        transactions=runtime.transactions,
        config=runtime.config,
        capture=ConversationCapture(runtime.evidence_store),
        authority_files=files,
        authority_preimages={name: snapshot.content[name] for name in files},
        authority_read_policies=policies,
    )
    graph_before = _target_bytes(runtime, "graph")
    try:
        proposal = GraphEnrichmentService(
            runtime,
            actor="local:asha",
            clock=clock,
            proposal_service=governed,
        ).propose(answered.id, submission)
    finally:
        for file in files.values():
            file.close()

    assert proposal.id.startswith("proposal:")
    assert runtime.intent_proposals.get(proposal.id) == proposal
    assert _target_bytes(runtime, "graph") == graph_before


def test_governed_enrichment_rejects_same_content_authority_substitution(
    runtime: Runtime,
) -> None:
    clock = Clock()
    base = _service(runtime, clock)
    opened = base.start(minutes=5)
    assert opened.current_gap_id is not None
    answered = base.answer(opened.id, opened.current_gap_id, "Workspace owners")
    _coordinator, submission = _clarification_proposal(runtime, answered)
    files = {
        "authority_config": runtime.workspace_directory.file("config.yaml"),
        "authority_policy": runtime.workspace_directory.file("approvals/policy.yaml"),
    }
    policy = LocalTransactionExtraReadPolicy(
        max_bytes=1024 * 1024,
        nonblocking_regular=True,
    )
    policies = {name: policy for name in files}
    snapshot = runtime.transactions.snapshot(files, extra_read_policies=policies)
    governed = ClarificationCoordinator(
        graph_store=runtime.graph_store,
        evidence_store=runtime.evidence_store,
        proposal_store=runtime.intent_proposals,
        transactions=runtime.transactions,
        config=runtime.config,
        capture=ConversationCapture(runtime.evidence_store),
        authority_files=files,
        authority_preimages={name: snapshot.content[name] for name in files},
        authority_read_policies=policies,
    )

    class SubstitutingProposal:
        def propose_enrichment(
            self,
            candidate: ClarificationProposalSubmission,
            *,
            principals: frozenset[str],
            binding: EnrichmentProposalBinding,
            enrichment_store: EnrichmentSessionStore,
            authority_files: dict[str, SecureFile],
            authority_read_policies: dict[str, LocalTransactionExtraReadPolicy],
        ):
            substitute = runtime.workspace_directory.file("history/substituted-config.yaml")
            substitute.atomic_write(authority_files["config"].read_bytes())
            replaced = dict(authority_files)
            replaced["config"] = substitute
            try:
                return governed.propose_enrichment(
                    candidate,
                    principals=principals,
                    binding=binding,
                    enrichment_store=enrichment_store,
                    authority_files=replaced,
                    authority_read_policies=authority_read_policies,
                )
            finally:
                substitute.close()

    try:
        service = GraphEnrichmentService(
            runtime,
            actor="local:asha",
            clock=clock,
            proposal_service=SubstitutingProposal(),
        )
        with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$"):
            service.propose(answered.id, submission)
    finally:
        for file in files.values():
            file.close()


def test_governed_proposal_rejects_stale_enrichment_binding_atomically(
    runtime: Runtime,
) -> None:
    clock = Clock()
    base_service = _service(runtime, clock)
    opened = base_service.start(minutes=5)
    assert opened.current_gap_id is not None
    answered = base_service.answer(opened.id, opened.current_gap_id, "Workspace owners")
    assert answered.current_gap_id is not None
    coordinator, submission = _clarification_proposal(runtime, answered)

    class RacingProposal:
        def propose_enrichment(
            self,
            candidate: ClarificationProposalSubmission,
            *,
            principals: frozenset[str],
            binding: EnrichmentProposalBinding,
            enrichment_store: EnrichmentSessionStore,
            authority_files: dict[str, SecureFile],
            authority_read_policies: dict[str, object],
        ):
            base_service.skip(answered.id, answered.current_gap_id or "")
            return coordinator.propose_enrichment(
                candidate,
                principals=principals,
                binding=binding,
                enrichment_store=enrichment_store,
                authority_files=authority_files,
                authority_read_policies=authority_read_policies,  # type: ignore[arg-type]
            )

    service = GraphEnrichmentService(
        runtime,
        actor="local:asha",
        clock=clock,
        proposal_service=RacingProposal(),
    )
    proposal_before = _target_bytes(runtime, "intent_proposals")
    graph_before = _target_bytes(runtime, "graph")

    with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$"):
        service.propose(answered.id, submission)

    assert _target_bytes(runtime, "intent_proposals") == proposal_before
    assert _target_bytes(runtime, "graph") == graph_before


def test_governed_proposal_rejects_complete_assessment_preimage_race(
    runtime: Runtime,
) -> None:
    clock = Clock()
    base_service = _service(runtime, clock)
    opened = base_service.start(minutes=5)
    assert opened.current_gap_id is not None
    answered = base_service.answer(opened.id, opened.current_gap_id, "Workspace owners")
    coordinator, submission = _clarification_proposal(runtime, answered)

    class RacingProposal:
        def propose_enrichment(
            self,
            candidate: ClarificationProposalSubmission,
            *,
            principals: frozenset[str],
            binding: EnrichmentProposalBinding,
            enrichment_store: EnrichmentSessionStore,
            authority_files: dict[str, SecureFile],
            authority_read_policies: dict[str, object],
        ):
            raced = candidate.changeset.model_copy(update={"id": "changeset:history-race"})
            with runtime.transactions.transaction() as transaction:
                transaction.append("history", serialize_changeset(raced))
            return coordinator.propose_enrichment(
                candidate,
                principals=principals,
                binding=binding,
                enrichment_store=enrichment_store,
                authority_files=authority_files,
                authority_read_policies=authority_read_policies,  # type: ignore[arg-type]
            )

    service = GraphEnrichmentService(
        runtime,
        actor="local:asha",
        clock=clock,
        proposal_service=RacingProposal(),
    )

    with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$"):
        service.propose(answered.id, submission)


def test_governed_proposal_cancellation_scrubs_retained_dependency_frames(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    base_service = _service(runtime, clock)
    opened = base_service.start(minutes=5)
    assert opened.current_gap_id is not None
    answered = base_service.answer(opened.id, opened.current_gap_id, "Workspace owners")
    coordinator, submission = _clarification_proposal(runtime, answered)
    signal = CancellationSignal("PRIVATE-PROPOSAL-CANCELLATION")
    retained: list[TracebackType] = []

    def cancel(*_args: object, **_kwargs: object):
        marker = signal.args[0]
        assert marker
        try:
            raise signal
        except BaseException as caught:
            if caught.__traceback__ is not None:
                retained.append(caught.__traceback__)
            raise

    monkeypatch.setattr(coordinator, "_propose", cancel)
    service = GraphEnrichmentService(
        runtime,
        actor="local:asha",
        clock=clock,
        proposal_service=coordinator,
    )

    with pytest.raises(CancellationSignal) as caught:
        service.propose(answered.id, submission)

    assert caught.value is signal
    assert signal.args == ()
    assert signal.__dict__ == {}
    assert signal.__cause__ is None
    assert signal.__context__ is None
    assert retained
    assert "PRIVATE-PROPOSAL-CANCELLATION" not in "\n".join(
        repr(frame.f_locals)
        for old_traceback in retained
        for frame, _line in traceback.walk_tb(old_traceback)
    )


def test_pause_restart_resume_reassesses_and_preserves_answer_evidence(runtime: Runtime) -> None:
    clock = Clock()
    service = _service(runtime, clock)
    opened = service.start(minutes=15)
    assert opened.current_gap_id is not None
    answered = service.answer(opened.id, opened.current_gap_id, "Workspace owners")
    paused = service.pause(answered.id)
    before = paused.snapshot_digest
    added = _evidence().model_copy(
        update={
            "id": "evidence:new-visible",
            "external_object_id": "docs/new.md",
            "source_locator": "docs/new.md",
            "content_hash": hashlib.sha256(b"new visible evidence").hexdigest(),
            "payload": {"content": "new visible evidence"},
        }
    )
    runtime.evidence_store.associate("markdown", added)
    clock.advance(seconds=600)

    service = _service(runtime, clock)
    resumed = service.resume(paused.id)

    assert resumed.status in {"open", "complete"}
    assert resumed.answer_evidence_refs == answered.answer_evidence_refs
    assert resumed.snapshot_digest != before
    assert resumed.remaining_budget_seconds == paused.remaining_budget_seconds


def test_active_budget_expiry_completes_without_fabricating_data(runtime: Runtime) -> None:
    clock = Clock()
    service = _service(runtime, clock)
    opened = service.start(minutes=5)
    clock.advance(seconds=300)

    completed = service.current(opened.id)

    assert completed.status == "complete"
    assert completed.answer_evidence_refs == ()
    assert completed.current_gap_id is None


@pytest.mark.parametrize("minutes", (5, 15, 30))
def test_approved_time_budgets_start_at_the_exact_limit(runtime: Runtime, minutes: int) -> None:
    service = _service(runtime, Clock())

    session = service.start(minutes=cast(Literal[5, 15, 30], minutes))

    assert session.budget_minutes == minutes
    assert session.remaining_budget_seconds == minutes * 60


def test_focus_only_session_never_selects_a_gap_outside_focus(runtime: Runtime) -> None:
    service = _service(runtime, Clock())
    session = service.start(focus="req:owners")
    selected = 0
    while session.status == "open":
        question = service.next_question(session.id)
        assert question.node_id == "req:owners"
        session = service.skip(session.id, question.gap_id)
        selected += 1
        assert selected < 16

    assert session.status == "complete"
    assert session.remaining_budget_seconds == 0


def test_service_builds_inside_one_transaction_without_nested_snapshot(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("nested recovery/snapshot")

    monkeypatch.setattr(runtime.transactions, "snapshot_without_recovery", forbidden)

    assert _service(runtime, Clock()).start(minutes=5).status == "open"


def test_dependency_reach_is_precomputed_once_per_assessment(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original = enrichment_module._dependency_reach

    def counted(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(enrichment_module, "_dependency_reach", counted)

    _service(runtime, Clock()).start(minutes=5)

    assert calls == 1


def test_dependency_reach_uses_only_active_approved_relations(runtime: Runtime) -> None:
    service = _service(runtime, Clock())
    with service._transaction() as transaction:
        snapshot, _report = service._snapshot(transaction)
    original = snapshot.graph.edges[0]
    graph = snapshot.graph.model_copy(
        update={
            "edges": (
                original,
                original.model_copy(
                    update={
                        "id": "edge:inactive-refines",
                        "from_id": "req:owners",
                        "to_id": "intent:product",
                        "status": "inactive",
                    }
                ),
                original.model_copy(
                    update={
                        "id": "edge:verified",
                        "from_id": "req:owners",
                        "to_id": "intent:product",
                        "relation": RelationType.VERIFIED_BY,
                    }
                ),
                original.model_copy(
                    update={
                        "id": "edge:contradicts",
                        "from_id": "req:owners",
                        "to_id": "intent:product",
                        "relation": RelationType.CONTRADICTS,
                    }
                ),
            )
        }
    )

    assert enrichment_module._dependency_reach(snapshot.model_copy(update={"graph": graph})) == {
        "intent:product": 1,
        "req:owners": 0,
    }


def test_dependency_reach_fails_before_work_exceeds_the_explicit_bound(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(runtime, Clock())
    with service._transaction() as transaction:
        snapshot, _report = service._snapshot(transaction)
    monkeypatch.setattr(enrichment_module, "_MAX_DEPENDENCY_NODES", 1, raising=False)

    with pytest.raises(ValueError, match="dependency graph exceeds enrichment bound"):
        enrichment_module._dependency_reach(snapshot)


def test_skip_progresses_without_inventing_answer_evidence(runtime: Runtime) -> None:
    service = _service(runtime, Clock())
    opened = service.start(minutes=5)
    assert opened.current_gap_id is not None
    evidence_before = _target_bytes(runtime, "evidence")

    skipped = service.skip(opened.id, opened.current_gap_id)

    assert skipped.skipped_gap_ids == (opened.current_gap_id,)
    assert skipped.current_gap_id != opened.current_gap_id
    assert skipped.answer_evidence_refs == ()
    assert _target_bytes(runtime, "evidence") == evidence_before


def test_optional_wording_may_change_only_prompt(runtime: Runtime) -> None:
    class HelpfulWording:
        def rephrase(self, question: EnrichmentQuestion) -> EnrichmentQuestion:
            return question.model_copy(update={"prompt": "Please provide the missing detail."})

    class HostileWording:
        def rephrase(self, question: EnrichmentQuestion) -> EnrichmentQuestion:
            return question.model_copy(update={"evidence_scope": ("evidence:hidden",)})

    class InvalidPromptWording:
        def __init__(self, prompt: str) -> None:
            self.prompt = prompt

        def rephrase(self, question: EnrichmentQuestion) -> EnrichmentQuestion:
            return question.model_copy(update={"prompt": self.prompt})

    class NoncanonicalWording:
        def rephrase(self, question: EnrichmentQuestion) -> EnrichmentQuestion:
            return question.model_copy(
                update={"requested_fields": tuple(reversed(question.requested_fields))}
            )

    class RawEnumWording:
        def rephrase(self, question: EnrichmentQuestion) -> EnrichmentQuestion:
            return question.model_copy(update={"dimension": question.dimension.value})

    clock = Clock()
    helpful = GraphEnrichmentService(
        runtime, actor="local:asha", clock=clock, wording=HelpfulWording()
    )
    opened = helpful.start(minutes=5)
    question = helpful.next_question(opened.id)
    assert question.prompt == "Please provide the missing detail."

    hostile = GraphEnrichmentService(
        runtime, actor="local:asha", clock=clock, wording=HostileWording()
    )
    with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$"):
        hostile.next_question(opened.id)
    for prompt in ("x" * 9000, "unsafe\x00prompt"):
        invalid = GraphEnrichmentService(
            runtime,
            actor="local:asha",
            clock=clock,
            wording=InvalidPromptWording(prompt),
        )
        with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$"):
            invalid.next_question(opened.id)
    noncanonical = GraphEnrichmentService(
        runtime,
        actor="local:asha",
        clock=clock,
        wording=NoncanonicalWording(),
    )
    with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$"):
        noncanonical.next_question(opened.id)
    raw_enum = GraphEnrichmentService(
        runtime,
        actor="local:asha",
        clock=clock,
        wording=RawEnumWording(),
    )
    with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$"):
        raw_enum.next_question(opened.id)


def test_answer_revalidates_preimages_and_rolls_back_all_canonical_writes(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(runtime, Clock())
    opened = service.start(minutes=5)
    assert opened.current_gap_id is not None
    before = {
        name: _target_bytes(runtime, name) for name in ("graph", "evidence", "enrichment_sessions")
    }
    original = service._capture.record_turn

    def replace_graph(**kwargs: object) -> EvidenceRecord:
        record = original(**kwargs)  # type: ignore[arg-type]
        target = runtime.transactions.target_file("graph")
        try:
            target.atomic_write(
                serialize_graph(_graph("evidence:intent").model_copy(update={"version": 2}))
            )
        finally:
            target.close()
        return record

    monkeypatch.setattr(service._capture, "record_turn", replace_graph)

    with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$"):
        service.answer(opened.id, opened.current_gap_id, "Workspace owners")

    assert {
        name: _target_bytes(runtime, name) for name in ("graph", "evidence", "enrichment_sessions")
    } == before


def test_stale_gap_answer_persists_reassessment_without_capturing_evidence(
    runtime: Runtime,
) -> None:
    service = _service(runtime, Clock())
    opened = service.start(minutes=5)
    assert opened.current_gap_id is not None
    evidence_before = _target_bytes(runtime, "evidence")
    target = runtime.transactions.target_file("graph")
    try:
        target.atomic_write(
            serialize_graph(Graph(id="graph:enrichment", version=2, nodes=(), edges=()))
        )
    finally:
        target.close()

    with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$"):
        service.answer(opened.id, opened.current_gap_id, "must not be captured")

    durable = runtime.enrichment_sessions.latest(opened.id)
    assert durable.snapshot_digest != opened.snapshot_digest
    assert durable.status == "complete"
    assert durable.answer_evidence_refs == ()
    assert _target_bytes(runtime, "evidence") == evidence_before


class CancellationSignal(BaseException):
    """Test-only cancellation whose exact identity must survive the public boundary."""


def test_answer_cancellation_is_exact_scrubbed_and_atomic(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(runtime, Clock())
    opened = service.start(minutes=5)
    assert opened.current_gap_id is not None
    before = {name: _target_bytes(runtime, name) for name in ("evidence", "enrichment_sessions")}
    secret = "PRIVATE-ENRICHMENT-ANSWER-8197"
    signal = CancellationSignal(secret)

    def cancel(**_kwargs: object) -> EvidenceRecord:
        raise signal

    monkeypatch.setattr(service._capture, "record_turn", cancel)
    with pytest.raises(CancellationSignal) as caught:
        service.answer(opened.id, opened.current_gap_id, secret)

    assert caught.value is signal
    assert caught.value.args == ()
    assert caught.value.__dict__ == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    repository_locals = "\n".join(
        repr(frame.f_locals)
        for frame, _line in traceback.walk_tb(caught.value.__traceback__)
        if "/src/intent_engineering/" in frame.f_code.co_filename
    )
    assert secret not in repository_locals
    assert {
        name: _target_bytes(runtime, name) for name in ("evidence", "enrichment_sessions")
    } == before


def test_authority_cleanup_attempts_both_closes_and_preserves_primary_cancellation(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(runtime, Clock())
    opened = service.start(minutes=5)
    assert opened.current_gap_id is not None
    primary = CancellationSignal("PRIVATE-PRIMARY-ANSWER")
    policy_cleanup = CancellationSignal("PRIVATE-POLICY-CLEANUP")
    config_cleanup = CancellationSignal("PRIVATE-CONFIG-CLEANUP")
    counts = {"policy.yaml": 0, "config.yaml": 0}
    armed = {"value": False}
    retained: list[TracebackType] = []
    authority_ids: set[int] = set()
    original_close = SecureFile.close
    original_file = runtime.workspace_directory.file

    def tracked_file(relative_path: str) -> SecureFile:
        result = original_file(relative_path)
        if relative_path in {"config.yaml", "approvals/policy.yaml"}:
            authority_ids.add(id(result))
        return result

    def cancel_answer(**_kwargs: object) -> EvidenceRecord:
        armed["value"] = True
        raise primary

    def hostile_close(file: SecureFile) -> None:
        name = file.name
        original_close(file)
        if (
            not armed["value"]
            or id(file) not in authority_ids
            or name not in counts
            or counts[name] != 0
        ):
            return
        counts[name] += 1
        signal = policy_cleanup if name == "policy.yaml" else config_cleanup
        marker = signal.args[0]
        assert marker
        try:
            raise signal
        except BaseException as caught:
            if caught.__traceback__ is not None:
                retained.append(caught.__traceback__)
            raise

    monkeypatch.setattr(service._capture, "record_turn", cancel_answer)
    monkeypatch.setattr(runtime.workspace_directory, "file", tracked_file)
    monkeypatch.setattr(SecureFile, "close", hostile_close)

    with pytest.raises(CancellationSignal) as caught:
        service.answer(opened.id, opened.current_gap_id, "PRIVATE-PRIMARY-ANSWER")

    assert caught.value is primary, (
        caught.value is policy_cleanup,
        caught.value is config_cleanup,
    )
    assert counts == {"policy.yaml": 1, "config.yaml": 1}
    for signal in (primary, policy_cleanup, config_cleanup):
        assert signal.args == ()
        assert signal.__dict__ == {}
        assert signal.__cause__ is None
        assert signal.__context__ is None
    assert retained
    assert "PRIVATE-" not in "\n".join(
        repr(frame.f_locals)
        for old_traceback in retained
        for frame, _line in traceback.walk_tb(old_traceback)
    )


def test_cleanup_cancellation_wins_over_ordinary_primary_failure(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(runtime, Clock())
    opened = service.start(minutes=5)
    assert opened.current_gap_id is not None
    cleanup = CancellationSignal("PRIVATE-CLEANUP-CANCELLATION")
    authority_ids: set[int] = set()
    counts = {"policy.yaml": 0, "config.yaml": 0}
    armed = {"value": False}
    original_close = SecureFile.close
    original_file = runtime.workspace_directory.file

    def tracked_file(relative_path: str) -> SecureFile:
        result = original_file(relative_path)
        if relative_path in {"config.yaml", "approvals/policy.yaml"}:
            authority_ids.add(id(result))
        return result

    def fail_answer(**_kwargs: object) -> EvidenceRecord:
        armed["value"] = True
        raise ValueError("PRIVATE-ORDINARY-PRIMARY")

    def hostile_close(file: SecureFile) -> None:
        name = file.name
        original_close(file)
        if armed["value"] and id(file) in authority_ids and name in counts and counts[name] == 0:
            counts[name] += 1
            if name == "policy.yaml":
                raise cleanup

    monkeypatch.setattr(runtime.workspace_directory, "file", tracked_file)
    monkeypatch.setattr(service._capture, "record_turn", fail_answer)
    monkeypatch.setattr(SecureFile, "close", hostile_close)

    with pytest.raises(CancellationSignal) as caught:
        service.answer(opened.id, opened.current_gap_id, "answer")

    assert caught.value is cleanup
    assert counts == {"policy.yaml": 1, "config.yaml": 1}
    assert cleanup.args == ()


def test_ordinary_answer_failure_clears_externally_retained_secret_frames(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(runtime, Clock())
    opened = service.start(minutes=5)
    assert opened.current_gap_id is not None
    secret = "PRIVATE-ORDINARY-ANSWER-FAILURE"
    retained: list[TracebackType] = []

    def fail(**_kwargs: object) -> EvidenceRecord:
        marker = secret
        try:
            raise ValueError(marker)
        except ValueError as caught:
            if caught.__traceback__ is not None:
                retained.append(caught.__traceback__)
            raise

    monkeypatch.setattr(service._capture, "record_turn", fail)

    with pytest.raises(GraphEnrichmentError, match="^graph enrichment unavailable$") as caught:
        service.answer(opened.id, opened.current_gap_id, secret)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert retained
    assert secret not in "\n".join(
        repr(frame.f_locals) for frame, _line in traceback.walk_tb(retained[0])
    )
