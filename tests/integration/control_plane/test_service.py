"""Integration proof for repository-bound authenticated workflow decisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.mcp import McpConnector
from intent_engineering.cli.runtime import Runtime, load_runtime
from intent_engineering.cli.writes import WriteWorkflow, write_workflow
from intent_engineering.control_plane.models import CredentialRecord, HumanDecisionPayload
from intent_engineering.control_plane.service import ControlPlaneError, ControlPlaneService
from intent_engineering.control_plane.webauthn_service import (
    AuthenticationRequest,
    RegistrationRequest,
    VerifiedAuthentication,
    VerifiedRegistration,
    WebAuthnVerifier,
)
from intent_engineering.core.models import (
    ChangeSet,
    EvidenceSide,
    Node,
    NodeType,
    ProjectConfig,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    ResolutionAction,
    SourceMode,
    SourceRole,
    SourceRoleAssignment,
)
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.bootstrap import BootstrapService, BootstrapSubmission
from intent_engineering.intent_workflow.clarification import ClarificationCoordinator
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.models import (
    ClarificationProposalSubmission,
    ClarificationQuestionInput,
    TaskEnvelope,
)
from intent_engineering.mutations.models import WritePlan, write_plan_id
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.executor import LocalChangeSetExecutor
from tests.e2e.test_cli_write_approval import _approval_project
from tests.unit.mutations.test_planner import base_plan

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
ORIGIN = "http://localhost:43127"


@dataclass
class _Verifier(WebAuthnVerifier):
    authentication_requests: list[AuthenticationRequest] = field(default_factory=list)

    def registration_options(self, request: RegistrationRequest) -> bytes:
        del request
        raise AssertionError("registration is outside this integration boundary")

    def verify_registration(
        self, response: bytes, request: RegistrationRequest
    ) -> VerifiedRegistration:
        del response, request
        raise AssertionError("registration is outside this integration boundary")

    def authentication_options(self, request: AuthenticationRequest) -> bytes:
        self.authentication_requests.append(request)
        return b'{"publicKey":{"userVerification":"required"}}'

    def verify_authentication(
        self, response: bytes, request: AuthenticationRequest
    ) -> VerifiedAuthentication:
        assert response in {b"signed-assertion", b"private-assertion-marker"}
        assert request == self.authentication_requests[-1]
        return VerifiedAuthentication(
            credential_id=b"control-plane-credential",
            new_sign_count=0,
            user_verified=True,
        )


@dataclass
class _Harness:
    runtime: Runtime
    service: ControlPlaneService
    verifier: _Verifier
    project: Path

    def payload(self, preview: dict[str, object]) -> HumanDecisionPayload:
        return HumanDecisionPayload.model_validate_json(json.dumps(preview["payload"]))

    def sign(self, preview: dict[str, object]) -> HumanDecisionPayload:
        assert preview["preview_digest"] == _preview_digest(preview)
        payload = self.payload(preview)
        assert self.service.decision_options(payload).startswith(b'{"publicKey"')
        request = self.verifier.authentication_requests[-1]
        assert request.payload_bytes == payload.canonical_bytes()
        assert request.challenge == hashlib.sha256(payload.canonical_bytes()).digest()
        return payload

    def state(self) -> dict[str, bytes | None]:
        snapshot = self.runtime.transactions.snapshot()
        return dict(snapshot.content)


@dataclass
class _ApprovalHarness:
    project: Path
    runtime: Runtime
    service: ControlPlaneService
    verifier: _Verifier
    workflow: WriteWorkflow
    plan: WritePlan

    def sign(self, preview: dict[str, object]) -> HumanDecisionPayload:
        assert preview["preview_digest"] == _preview_digest(preview)
        payload = HumanDecisionPayload.model_validate_json(json.dumps(preview["payload"]))
        assert self.service.decision_options(payload).startswith(b'{"publicKey"')
        request = self.verifier.authentication_requests[-1]
        assert request.payload_bytes == payload.canonical_bytes()
        assert request.challenge == hashlib.sha256(payload.canonical_bytes()).digest()
        return payload


def _policy(actor: str = "local:owner") -> dict[str, object]:
    return {
        "schema_version": 1,
        "contributors": [actor],
        "approvers": [actor],
        "executors": [actor],
        "identities": {actor: [actor]},
    }


def _harness(tmp_path: Path, *, actor: str = "local:owner") -> _Harness:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    (project / "docs").mkdir()
    (project / "docs/prd.md").write_text("# Product\nKeep exports local.\n", encoding="utf-8")
    config = ProjectConfig(
        project_id=project.name,
        local_actor=actor,
        source_exclusions=(".intent/**", ".git/**"),
        source_roles=(
            SourceRoleAssignment(
                connector_id="markdown",
                scope="docs/prd.md",
                role=SourceRole.DECLARED_INTENT,
                inherited=False,
            ),
        ),
    )
    (project / ".intent/config.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    (project / ".intent/approvals/policy.yaml").write_text(
        yaml.safe_dump(_policy(actor), sort_keys=True), encoding="utf-8"
    )
    runtime = load_runtime(project)
    verifier = _Verifier()
    nonce = iter(range(1, 256))
    service = ControlPlaneService(
        runtime,
        origin=ORIGIN,
        clock=lambda: NOW,
        challenge_source=lambda: bytes([next(nonce)]) * 32,
        webauthn_verifier=verifier,
    )
    assert runtime.webauthn_credentials.put(
        CredentialRecord(
            id="credential:" + "a" * 64 + ":0",
            project_id=config.project_id,
            repository_id=service.repository_id,
            actor=actor,
            credential_id="Y29udHJvbC1wbGFuZS1jcmVkZW50aWFs",
            public_key="cHVibGljLWtleQ",
            sign_count=0,
            created_at=NOW - timedelta(days=1),
        )
    )
    return _Harness(runtime, service, verifier, project)


def _preview_digest(preview: dict[str, object]) -> str:
    encoded = json.dumps(
        preview["preview"],
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _optional_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _repository_traceback_locals(error: BaseException) -> str:
    repository_locals: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return "".join(repository_locals)


def _approval_harness(tmp_path: Path) -> _ApprovalHarness:
    project = _approval_project(tmp_path)
    runtime = load_runtime(project)
    verifier = _Verifier()
    service = ControlPlaneService(
        runtime,
        origin=ORIGIN,
        clock=lambda: datetime(2026, 8, 26, 12, 1, tzinfo=UTC),
        challenge_source=lambda: b"w" * 32,
        webauthn_verifier=verifier,
    )
    assert runtime.webauthn_credentials.put(
        CredentialRecord(
            id="credential:" + "b" * 64 + ":0",
            project_id=runtime.config.project_id,
            repository_id=service.repository_id,
            actor="local:reviewer",
            credential_id="Y29udHJvbC1wbGFuZS1jcmVkZW50aWFs",
            public_key="cHVibGljLWtleQ",
            sign_count=0,
            created_at=datetime(2026, 8, 26, 11, 0, tzinfo=UTC),
        )
    )
    workflow = write_workflow(runtime)
    source = base_plan()
    selected = workflow.catalog.configured[0]
    material = source.model_dump(mode="json", exclude={"id"})
    material["connector_id"] = McpConnector(
        workflow.catalog.mcp_runtime,
        config=selected.config,
        profile=selected.profile,
        object_name=source.object_type,
        local_actor=source.created_by,
    ).connector_id
    plan = WritePlan.model_validate_json(json.dumps({"id": write_plan_id(material), **material}))
    assert workflow.plans.put(plan)
    return _ApprovalHarness(project, runtime, service, verifier, workflow, plan)


def _bootstrap_proposal(harness: _Harness) -> tuple[str, str]:
    captured = harness.service.onboard_preview("docs/prd.md")
    evidence_ref = str(captured["evidence_ref"])
    node = Node(
        id="intent-local-export",
        type=NodeType.PRODUCT_INTENT,
        label="Keep exports local",
        status="proposed",
        created_by="agent:codex",
        created_at=NOW,
        last_modified_by="agent:codex",
        last_modified_at=NOW,
        source_mode=SourceMode.INFERRED,
        intent_fidelity_confidence=0.82,
        confidence_basis="Inferred from the captured PRD",
        last_reassessed_at=NOW,
        evidence_refs=(evidence_ref,),
    )
    submission = BootstrapSubmission(
        baseline_graph_version=0,
        actor="agent:codex",
        timestamp=NOW,
        evidence_refs=(evidence_ref,),
        source_roles=harness.runtime.config.source_roles,
        candidate_nodes=(node,),
        candidate_edges=(),
        core_node_ids=(node.id,),
        provisional_node_ids=(),
    )
    service = BootstrapService(
        graph_store=harness.runtime.graph_store,
        evidence_store=harness.runtime.evidence_store,
        proposal_store=harness.runtime.intent_proposals,
        changeset_executor=LocalChangeSetExecutor(
            harness.runtime.graph_store,
            harness.runtime.case_store,
            harness.runtime.transactions,
        ),
        transactions=harness.runtime.transactions,
        config=harness.runtime.config,
    )
    review = service.propose(submission, frozenset({harness.runtime.config.local_actor}))
    assert review.candidate_changeset.nodes_added == (node,)
    return review.proposal_id, evidence_ref


def test_authenticated_onboarding_activation_binds_preview_and_applies_once(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    assert harness.service.status()["status"] == "onboarding_required"
    proposal_id, evidence_ref = _bootstrap_proposal(harness)
    assert harness.service.status()["status"] == "human_attention_required"
    preview = harness.service.proposal_preview(proposal_id)
    assert preview["preview_digest"] == _preview_digest(preview)
    payload = harness.sign(preview)
    graph_before = harness.runtime.graph_store.load()
    history_path = harness.project / ".intent/history/changesets.jsonl"
    history_before = _optional_bytes(history_path)

    result = harness.service.apply_decision(b"signed-assertion", payload)

    graph = harness.runtime.graph_store.load()
    history = history_path.read_bytes()
    assert result["status"] == "activated"
    assert graph.version == graph_before.version + 1
    assert len(history.splitlines()) == len((history_before or b"").splitlines()) + 1
    assert graph.nodes[0].last_modified_by == "local:owner"
    assert graph.nodes[0].evidence_refs == (evidence_ref,)
    assert harness.service.status()["status"] == "local_only"
    applied = harness.state()
    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        harness.service.apply_decision(b"signed-assertion", payload)
    assert harness.state() == applied


def _open_clarification(harness: _Harness):
    capture = ConversationCapture(harness.runtime.evidence_store)
    request = capture.record_turn(
        conversation_ref="codex:control-plane-thread",
        role="human",
        author="local:owner",
        content="Add read-only sharing",
        captured_at=NOW - timedelta(microseconds=3),
        acl=("agent:codex", "local:owner"),
    )
    classification = capture.record_turn(
        conversation_ref="codex:control-plane-thread",
        role="agent",
        author="agent:codex",
        content={"classification": "new_or_ambiguous"},
        captured_at=NOW - timedelta(microseconds=2),
        acl=("agent:codex", "local:owner"),
    )
    coordinator = ClarificationCoordinator(
        graph_store=harness.runtime.graph_store,
        evidence_store=harness.runtime.evidence_store,
        proposal_store=harness.runtime.intent_proposals,
        transactions=harness.runtime.transactions,
        config=harness.runtime.config,
        capture=capture,
    )
    task = TaskEnvelope(
        repository_id=harness.runtime.config.project_id,
        actor="local:owner",
        conversation_ref="codex:control-plane-thread",
        request="Add read-only sharing",
        request_evidence_ref=request.id,
        graph_version=harness.runtime.graph_store.load().version,
        created_at=NOW - timedelta(microseconds=3),
    )
    session = coordinator.open(
        task,
        classification_evidence_ref=classification.id,
        questions=(
            ClarificationQuestionInput(
                id="audience", prompt="Who may share reports?", required=True
            ),
        ),
        opened_by="agent:codex",
        opened_at=NOW - timedelta(microseconds=1),
        principals=frozenset({"agent:codex", "local:owner"}),
    )
    return coordinator, session


def _open_conflict_case(harness: _Harness) -> ReconciliationCase:
    proposal_id, evidence_ref = _bootstrap_proposal(harness)
    harness.service.apply_decision(
        b"signed-assertion", harness.sign(harness.service.proposal_preview(proposal_id))
    )
    case = ReconciliationCase(
        id="case:control-plane-conflict",
        subject_ref="intent-local-export",
        case_type=ReconciliationCaseType.CONFLICTING_SOURCES,
        affected_refs=("intent-local-export",),
        evidence_sides=(
            EvidenceSide(
                label="current",
                claim="Exports stay local",
                evidence_refs=(evidence_ref,),
                observed_at=NOW,
                authors=("local:owner",),
                confidence=0.9,
                source_mode=SourceMode.EXPLICIT,
                current=True,
            ),
        ),
        detector_id="control-plane-test",
        fingerprint="d" * 64,
        created_at=NOW,
        created_by="control-plane-test",
    )
    harness.runtime.case_store.put(case)
    harness.runtime.case_store.put(
        transition_case(case, ReconciliationStatus.PROPOSED, "control-plane-test", NOW)
    )
    harness.runtime.case_store.put(
        transition_case(
            harness.runtime.case_store.get(case.id),
            ReconciliationStatus.NEEDS_HUMAN,
            "control-plane-test",
            NOW,
        )
    )
    return harness.runtime.case_store.get(case.id)


def test_authenticated_clarification_answer_captures_exact_human_evidence(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _coordinator, session = _open_clarification(harness)
    answer = "Workspace owners may share read-only reports."
    preview = harness.service.answer_preview(session.id, "audience", answer)
    payload = harness.sign(preview)
    graph_before = harness.runtime.graph_store.load()
    history_path = harness.project / ".intent/history/changesets.jsonl"
    history_before = _optional_bytes(history_path)

    result = harness.service.apply_decision(b"signed-assertion", payload)

    durable = harness.runtime.intent_proposals.session(session.id)
    record = harness.runtime.evidence_store.get(durable.answers[0].evidence_ref)
    assert result["status"] == "open"
    assert record.author == "local:owner"
    assert record.payload == {"role": "human", "content": answer}
    assert durable.answers[0].answer_digest == payload.subject_digest
    assert harness.runtime.graph_store.load() == graph_before
    assert _optional_bytes(history_path) == history_before


def test_private_answer_cancellation_rolls_back_and_scrubs_traceback(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _coordinator, session = _open_clarification(harness)
    secret = "private answer marker: owners only"
    preview = harness.service.answer_preview(session.id, "audience", secret)
    payload = harness.sign(preview)
    signal = asyncio.CancelledError()

    def cancel(stage: str) -> None:
        if stage == "target:evidence":
            raise signal

    harness.runtime.transactions._fault_hook = cancel
    before = harness.state()
    with pytest.raises(asyncio.CancelledError) as caught:
        harness.service.apply_decision(b"private-assertion-marker", payload)

    assert caught.value is signal
    assert harness.state() == before
    locals_text = _repository_traceback_locals(caught.value)
    assert secret not in locals_text
    assert "private-assertion-marker" not in locals_text
    harness.runtime.transactions._fault_hook = None
    assert harness.service.apply_decision(b"signed-assertion", payload)["status"] == "open"


def test_authenticated_clarified_proposal_confirmation_has_one_graph_transition(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    coordinator, session = _open_clarification(harness)
    answer_preview = harness.service.answer_preview(
        session.id, "audience", "Workspace owners may share read-only reports."
    )
    harness.service.apply_decision(b"signed-assertion", harness.sign(answer_preview))
    answered = harness.runtime.intent_proposals.session(session.id)
    evidence_refs = (
        answered.request_evidence_ref,
        answered.classification_evidence_ref,
        *(item.evidence_ref for item in answered.questions),
        *(item.evidence_ref for item in answered.answers),
    )
    proposed = Node(
        id="req-read-only-sharing",
        type=NodeType.REQUIREMENT,
        label="Workspace owners may share read-only reports",
        status="proposed",
        created_by="local:owner",
        created_at=NOW,
        last_modified_by="local:owner",
        last_modified_at=NOW,
        source_mode=SourceMode.INFERRED,
        intent_fidelity_confidence=0.8,
        confidence_basis="Clarified human answer",
        last_reassessed_at=NOW,
        evidence_refs=evidence_refs,
    )
    changeset = ChangeSet(
        id="",
        actor="local:owner",
        timestamp=NOW,
        baseline_graph_version=answered.baseline_graph_version,
        evidence_refs=evidence_refs,
        nodes_added=(proposed,),
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
    proposal = coordinator.propose(
        ClarificationProposalSubmission(
            session_id=answered.id,
            task_id=answered.task_id,
            baseline_graph_version=answered.baseline_graph_version,
            actor="local:owner",
            timestamp=NOW,
            evidence_refs=evidence_refs,
            changeset=changeset,
            core_node_ids=(proposed.id,),
        ),
        principals=frozenset({"agent:codex", "local:owner"}),
    )
    preview = harness.service.proposal_preview(proposal.id)
    payload = harness.sign(preview)
    version = harness.runtime.graph_store.load().version
    history_path = harness.project / ".intent/history/changesets.jsonl"
    history_lines = len((_optional_bytes(history_path) or b"").splitlines())

    result = harness.service.apply_decision(b"signed-assertion", payload)

    assert result["status"] == "applied"
    assert harness.runtime.graph_store.load().version == version + 1
    assert len(history_path.read_bytes().splitlines()) == history_lines + 1
    assert harness.runtime.graph_store.load().nodes[-1].last_modified_by == "local:owner"


def test_authenticated_conflict_resolution_binds_case_and_changeset(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    case = _open_conflict_case(harness)
    preview = harness.service.proposal_preview(case.id)
    payload = harness.sign(preview)
    version = harness.runtime.graph_store.load().version

    result = harness.service.apply_decision(b"signed-assertion", payload)

    assert result["status"] == "resolved"
    assert result["action"] == ResolutionAction.UPDATE_REQUIREMENT.value
    assert harness.runtime.graph_store.load().version == version + 1
    resolved = harness.runtime.case_store.get(case.id)
    assert resolved.status is ReconciliationStatus.RESOLVED
    assert resolved.history[-1].actor == "local:owner"
    assert resolved.resolved_by_changeset == result["changeset_id"]


def test_case_transition_interrupt_restores_graph_history_and_case(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    case = _open_conflict_case(harness)
    payload = harness.sign(harness.service.proposal_preview(case.id))
    signal = KeyboardInterrupt()

    def interrupt(stage: str) -> None:
        if stage == "target:cases":
            raise signal

    harness.runtime.transactions._fault_hook = interrupt
    before = harness.state()
    with pytest.raises(KeyboardInterrupt) as caught:
        harness.service.apply_decision(b"private-assertion-marker", payload)

    assert caught.value is signal
    assert harness.state() == before
    assert "private-assertion-marker" not in _repository_traceback_locals(caught.value)


def test_authenticated_guarded_write_approval_appends_only_the_exact_approval(
    tmp_path: Path,
) -> None:
    harness = _approval_harness(tmp_path)
    preview = harness.service.proposal_preview(harness.plan.id)
    payload = harness.sign(preview)
    graph_before = harness.runtime.graph_store.load()
    receipts_path = harness.project / ".intent/approvals/receipts.jsonl"
    receipts_before = _optional_bytes(receipts_path)

    result = harness.service.apply_decision(b"signed-assertion", payload)

    approval = harness.workflow.approvals.get(str(result["approval_id"]))
    assert result["status"] == "approved"
    assert approval.actor == "local:reviewer"
    assert approval.plan_id == harness.plan.id
    assert approval.plan_hash == payload.subject_digest
    assert harness.runtime.graph_store.load() == graph_before
    assert _optional_bytes(receipts_path) == receipts_before


def test_approval_append_cancellation_restores_exact_state_and_identity(
    tmp_path: Path,
) -> None:
    harness = _approval_harness(tmp_path)
    preview = harness.service.proposal_preview(harness.plan.id)
    payload = harness.sign(preview)
    signal = asyncio.CancelledError()

    def cancel(stage: str) -> None:
        if stage == "target:approvals":
            raise signal

    harness.runtime.transactions._fault_hook = cancel
    before = {
        path: path.read_bytes()
        for path in harness.project.joinpath(".intent").rglob("*")
        if path.is_file()
    }
    with pytest.raises(asyncio.CancelledError) as caught:
        harness.service.apply_decision(b"private-assertion-marker", payload)

    assert caught.value is signal
    assert {
        path: path.read_bytes()
        for path in harness.project.joinpath(".intent").rglob("*")
        if path.is_file()
    } == before
    assert "private-assertion-marker" not in _repository_traceback_locals(caught.value)


@pytest.mark.parametrize(
    "changed",
    ("config", "policy", "graph", "evidence", "case", "proposal", "credential"),
)
def test_live_authority_drift_rejects_without_additional_mutation(
    tmp_path: Path, changed: str
) -> None:
    harness = _harness(tmp_path)
    proposal_id, _evidence_ref = _bootstrap_proposal(harness)
    payload = harness.sign(harness.service.proposal_preview(proposal_id))
    paths = {
        "config": harness.project / ".intent/config.yaml",
        "policy": harness.project / ".intent/approvals/policy.yaml",
        "graph": harness.project / ".intent/graph.yaml",
        "evidence": harness.project / ".intent/evidence/evidence.jsonl",
        "case": harness.project / ".intent/reconciliation/cases.jsonl",
        "proposal": harness.project / ".intent/history/intent-proposals.jsonl",
    }
    if changed == "credential":
        harness.runtime.webauthn_credentials.put(
            CredentialRecord(
                id="credential:" + "c" * 64 + ":0",
                project_id=harness.runtime.config.project_id,
                repository_id=harness.service.repository_id,
                actor="local:owner",
                credential_id="ZHJpZnRlZC1jcmVkZW50aWFs",
                public_key="ZHJpZnRlZC1wdWJsaWMta2V5",
                sign_count=0,
                created_at=NOW,
            )
        )
    else:
        paths[changed].write_bytes((_optional_bytes(paths[changed]) or b"") + b"\n")
    before = {
        path: path.read_bytes()
        for path in harness.project.joinpath(".intent").rglob("*")
        if path.is_file()
    }

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        harness.service.apply_decision(b"private-assertion", payload)

    after = {path: path.read_bytes() for path in before}
    assert after == before


@pytest.mark.parametrize("stage", ("target:intent_proposals", "target:graph", "target:history"))
def test_activation_interrupt_rolls_back_exact_state_and_scrubs_assertion(
    tmp_path: Path, stage: str
) -> None:
    harness = _harness(tmp_path)
    proposal_id, _evidence_ref = _bootstrap_proposal(harness)
    payload = harness.sign(harness.service.proposal_preview(proposal_id))
    signal = KeyboardInterrupt()

    def interrupt(current: str) -> None:
        if current == stage:
            raise signal

    harness.runtime.transactions._fault_hook = interrupt
    before = harness.state()
    with pytest.raises(KeyboardInterrupt) as caught:
        harness.service.apply_decision(b"private-assertion-marker", payload)
    assert caught.value is signal
    assert harness.state() == before
    assert "private-assertion-marker" not in _repository_traceback_locals(caught.value)
