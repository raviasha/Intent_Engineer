"""Integration proof for repository-bound authenticated workflow decisions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import multiprocessing
import os
from collections.abc import Callable
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
from intent_engineering.intent_workflow.clarification import (
    ClarificationCoordinator,
    ProposalConfirmationPreview,
)
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.models import (
    ClarificationIntentProposal,
    ClarificationProposalSubmission,
    ClarificationQuestionInput,
    TaskEnvelope,
)
from intent_engineering.mutations.models import WritePlan, write_plan_id
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.approval_store import JsonlApprovalStore
from tests.e2e.test_cli_write_approval import _approval_project
from tests.unit.mutations.test_planner import base_plan

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
ORIGIN = "http://localhost:43127"


@dataclass
class _Verifier(WebAuthnVerifier):
    registration_requests: list[RegistrationRequest] = field(default_factory=list)
    authentication_requests: list[AuthenticationRequest] = field(default_factory=list)
    registration_hook: Callable[[], None] | None = None
    registration_options_result: bytes = b'{"publicKey":{"userVerification":"required"}}'

    def registration_options(self, request: RegistrationRequest) -> bytes:
        self.registration_requests.append(request)
        return self.registration_options_result

    def verify_registration(
        self, response: bytes, request: RegistrationRequest
    ) -> VerifiedRegistration:
        assert response == _registration_response(request)
        if self.registration_hook is not None:
            self.registration_hook()
        return VerifiedRegistration(
            credential_id=b"new-control-plane-credential",
            public_key=b"new-public-key",
            sign_count=0,
            user_verified=True,
        )

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


def _policy(actor: str = "local:owner", *, aliases: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "schema_version": 1,
        "contributors": [actor],
        "approvers": [actor],
        "executors": [actor],
        "identities": {actor: [actor, *aliases]},
    }


def _harness(
    tmp_path: Path,
    *,
    actor: str = "local:owner",
    aliases: tuple[str, ...] = (),
) -> _Harness:
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
        yaml.safe_dump(_policy(actor, aliases=aliases), sort_keys=True), encoding="utf-8"
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


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _registration_response(request: RegistrationRequest) -> bytes:
    client_data = json.dumps(
        {
            "challenge": _b64url(request.challenge),
            "origin": request.expected_origin,
            "type": "webauthn.create",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return json.dumps(
        {
            "id": _b64url(b"new-control-plane-credential"),
            "rawId": _b64url(b"new-control-plane-credential"),
            "response": {
                "attestationObject": _b64url(b"fake-attestation"),
                "clientDataJSON": _b64url(client_data),
            },
            "type": "public-key",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


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


def _crash_after_real_approval_put(
    service: ControlPlaneService,
    payload: HumanDecisionPayload,
) -> None:
    original = JsonlApprovalStore.put

    def crash(store: JsonlApprovalStore, record: object) -> bool:
        added = original(store, record)  # type: ignore[arg-type]
        os._exit(73 if added else 74)

    JsonlApprovalStore.put = crash  # type: ignore[method-assign,assignment]
    service.apply_decision(b"signed-assertion", payload)
    os._exit(75)


def _intent_files(project: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(project): path.read_bytes()
        for path in project.joinpath(".intent").rglob("*")
        if path.is_file()
    }


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


def test_registration_wrappers_own_actor_origin_time_and_return_detached_records(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)

    options = harness.service.registration_options()
    request = harness.verifier.registration_requests[-1]
    credential = harness.service.register(_registration_response(request))

    assert options == b'{"publicKey":{"userVerification":"required"}}'
    assert request.actor == harness.runtime.config.local_actor
    assert request.expected_origin == ORIGIN
    assert request.project_id == harness.runtime.config.project_id
    assert request.repository_id == harness.service.repository_id
    assert credential.actor == harness.runtime.config.local_actor
    assert credential.created_at == NOW
    assert credential is not harness.runtime.webauthn_credentials.list()[-1]
    assert credential == harness.runtime.webauthn_credentials.list()[-1]


def test_registration_options_reject_live_configuration_drift_without_issuing_challenge(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    changed = harness.runtime.config.model_copy(update={"local_actor": "local:other"})
    (harness.project / ".intent/config.yaml").write_text(
        yaml.safe_dump(changed.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    before = harness.state()

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$") as caught:
        harness.service.registration_options()

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert harness.state() == before


def test_registration_rolls_back_when_connector_membership_changes_during_verification(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.service.registration_options()
    request = harness.verifier.registration_requests[-1]
    response = _registration_response(request)
    before = harness.state()

    def change_membership() -> None:
        (harness.project / ".intent/connectors/drift.yaml").write_text(
            "schema_version: 1\n", encoding="utf-8"
        )

    harness.verifier.registration_hook = change_membership

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$") as caught:
        harness.service.register(response)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert harness.state() == before


def test_registration_options_cancellation_rolls_back_and_scrubs_authority_locals(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    secret = b"PRIVATE-CANCELLED-REGISTRATION-OPTIONS"
    harness.verifier.registration_options_result = b'{"private":"' + secret + b'"}'
    signal = asyncio.CancelledError("registration options cancelled")

    def cancel_after_commit(stage: str) -> None:
        if stage == "journal_committed":
            raise signal

    harness.runtime.transactions._fault_hook = cancel_after_commit
    before = harness.state()

    with pytest.raises(asyncio.CancelledError) as caught:
        harness.service.registration_options()

    assert caught.value is signal
    assert harness.state() == before
    assert secret.decode("ascii") not in _repository_traceback_locals(caught.value)


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


def _create_clarified_proposal(harness: _Harness) -> ClarificationIntentProposal:
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
    return coordinator.propose(
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


def _rewrite_evidence_acl(project: Path, evidence_ref: str, acl: tuple[str, ...]) -> None:
    path = project / ".intent/evidence/evidence.jsonl"
    frames = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    changed = False
    for frame in frames:
        evidence = frame.get("evidence")
        if isinstance(evidence, dict) and evidence.get("id") == evidence_ref:
            evidence["acl"] = list(acl)
            changed = True
    assert changed
    path.write_bytes(
        b"".join(
            json.dumps(frame, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
            for frame in frames
        )
    )


def _open_conflict_case(
    harness: _Harness, *, evidence_acl: tuple[str, ...] | None = None
) -> ReconciliationCase:
    proposal_id, evidence_ref = _bootstrap_proposal(harness)
    harness.service.apply_decision(
        b"signed-assertion", harness.sign(harness.service.proposal_preview(proposal_id))
    )
    if evidence_acl is not None:
        evidence_ref = (
            ConversationCapture(harness.runtime.evidence_store)
            .record_turn(
                conversation_ref="codex:alias-authority",
                role="human",
                author="local:owner",
                content="Alias-authorized conflict evidence",
                captured_at=NOW,
                acl=evidence_acl,
            )
            .id
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


def test_inbox_surfaces_only_acl_visible_unanswered_clarification_questions(
    tmp_path: Path,
) -> None:
    """Catches the browser Inbox omitting the authoritative clarification workflow."""
    harness = _harness(tmp_path)
    _coordinator, session = _open_clarification(harness)

    inbox = harness.service.inbox()

    assert inbox == {
        "schema_version": 1,
        "pending_proposal_ids": [],
        "open_case_ids": [],
        "clarification_sessions": [
            {
                "id": session.id,
                "task_id": session.task_id,
                "questions": [
                    {
                        "id": "audience",
                        "prompt": "Who may share reports?",
                        "required": True,
                    }
                ],
            }
        ],
    }
    _rewrite_evidence_acl(
        harness.project,
        session.questions[0].evidence_ref,
        ("private:unavailable",),
    )

    assert harness.service.inbox()["clarification_sessions"] == []


def test_pending_answer_expires_at_the_decision_lifetime_and_is_purged(
    tmp_path: Path,
) -> None:
    """Catches abandoned private plaintext remaining after its authority window closes."""
    harness = _harness(tmp_path)
    _coordinator, session = _open_clarification(harness)
    preview = harness.service.answer_preview(session.id, "audience", "Private answer")
    payload = harness.payload(preview)
    assert len(harness.service._pending_answers) == 1

    harness.service._clock = lambda: NOW + timedelta(minutes=5)
    harness.service.status()

    assert harness.service._pending_answers == {}
    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        harness.service.decision_options(payload)


def test_pending_answer_cap_fails_without_evicting_an_active_exact_preview(
    tmp_path: Path,
) -> None:
    """Catches unbounded plaintext growth or capacity handling that drops live authority."""
    harness = _harness(tmp_path)
    _coordinator, session = _open_clarification(harness)
    previews = [
        harness.service.answer_preview(session.id, "audience", f"Private answer {index}")
        for index in range(64)
    ]

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        harness.service.answer_preview(session.id, "audience", "Private answer overflow")

    assert len(harness.service._pending_answers) == 64
    first = previews[0]
    result = harness.service.apply_decision(b"signed-assertion", harness.sign(first))
    assert result["status"] == "open"


def test_pending_answer_cap_failure_scrubs_private_preview_traceback(
    tmp_path: Path,
) -> None:
    """Catches cap rejection retaining the private answer through preview evidence locals."""
    harness = _harness(tmp_path)
    _coordinator, session = _open_clarification(harness)
    for index in range(64):
        harness.service.answer_preview(session.id, "audience", f"Capacity answer {index}")
    secret = "PRIVATE-CAP-ANSWER-MARKER-43127"

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$") as caught:
        harness.service.answer_preview(session.id, "audience", secret)

    assert type(caught.value) is ControlPlaneError
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(harness.service._pending_answers) == 64
    assert secret not in _repository_traceback_locals(caught.value)


def test_discard_answer_preview_cleans_up_without_requesting_human_authority(
    tmp_path: Path,
) -> None:
    """Catches browser cancel retaining plaintext or accidentally creating authority."""
    harness = _harness(tmp_path)
    _coordinator, session = _open_clarification(harness)
    preview = harness.service.answer_preview(session.id, "audience", "Discard me")
    payload = harness.payload(preview)
    answer_id = payload.subject.id

    assert harness.service.discard_answer_preview(answer_id) == {
        "schema_version": 1,
        "status": "discarded",
        "answer_id": answer_id,
    }

    assert harness.service._pending_answers == {}
    assert harness.verifier.authentication_requests == []
    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        harness.service.decision_options(payload)
    assert harness.verifier.authentication_requests == []


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
    proposal = _create_clarified_proposal(harness)
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


def test_clarified_proposal_preview_and_options_require_production_evidence_authority(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    proposal = _create_clarified_proposal(harness)
    payload = harness.payload(harness.service.proposal_preview(proposal.id))
    _rewrite_evidence_acl(harness.project, proposal.evidence_refs[0], ("agent:codex",))
    before = harness.state()

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        harness.service.decision_options(payload)
    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        harness.service.proposal_preview(proposal.id)

    assert harness.state() == before


def test_production_confirmation_preview_is_typed_and_non_mutating(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    proposal = _create_clarified_proposal(harness)
    authority = harness.service._authority()
    confirmation = harness.service._confirmation_service(authority)
    before = _intent_files(harness.project)
    try:
        preview = confirmation.preview_confirmation(
            proposal.id,
            proposal_digest=proposal.digest,
            actor="local:owner",
            at=NOW,
        )
    finally:
        confirmation.close()
        authority.close()

    assert isinstance(preview, ProposalConfirmationPreview)
    assert preview.proposal == proposal
    assert preview.selected_node_ids == ("req-read-only-sharing",)
    assert preview.high_risk is False
    assert preview.risk_reasons == ()
    assert preview.review_case is None
    assert preview.activation_changeset.actor == "local:owner"
    assert _intent_files(harness.project) == before


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


def test_conflict_resolution_uses_the_exact_authenticated_actor_aliases(
    tmp_path: Path,
) -> None:
    alias = "provider:owner-101"
    harness = _harness(tmp_path, aliases=(alias,))
    case = _open_conflict_case(harness, evidence_acl=(alias,))
    payload = harness.sign(harness.service.proposal_preview(case.id))

    result = harness.service.apply_decision(b"signed-assertion", payload)

    assert result["status"] == "resolved"
    resolved = harness.runtime.case_store.get(case.id)
    assert resolved.status is ReconciliationStatus.RESOLVED
    assert resolved.history[-1].actor == "local:owner"


def test_conflict_resolution_rejects_authenticated_alias_drift_without_mutation(
    tmp_path: Path,
) -> None:
    alias = "provider:owner-101"
    harness = _harness(tmp_path, aliases=(alias,))
    case = _open_conflict_case(harness, evidence_acl=(alias,))
    payload = harness.sign(harness.service.proposal_preview(case.id))
    policy_path = harness.project / ".intent/approvals/policy.yaml"
    policy_path.write_text(
        yaml.safe_dump(_policy(), sort_keys=True),
        encoding="utf-8",
    )
    before = harness.state()

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        harness.service.apply_decision(b"signed-assertion", payload)

    assert harness.state() == before


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


def test_cancellation_immediately_after_real_approval_put_restores_exact_state_and_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _approval_harness(tmp_path)
    preview = harness.service.proposal_preview(harness.plan.id)
    payload = harness.sign(preview)
    signal = asyncio.CancelledError()

    original = JsonlApprovalStore.put

    def cancel_after_put(store: JsonlApprovalStore, record: object) -> bool:
        added = original(store, record)  # type: ignore[arg-type]
        assert added
        raise signal

    monkeypatch.setattr(JsonlApprovalStore, "put", cancel_after_put)
    before = _intent_files(harness.project)
    with pytest.raises(asyncio.CancelledError) as caught:
        harness.service.apply_decision(b"private-assertion-marker", payload)

    assert caught.value is signal
    assert _intent_files(harness.project) == before
    assert "private-assertion-marker" not in _repository_traceback_locals(caught.value)


def test_process_crash_immediately_after_real_approval_put_recovers_exact_bytes(
    tmp_path: Path,
) -> None:
    harness = _approval_harness(tmp_path)
    payload = harness.sign(harness.service.proposal_preview(harness.plan.id))
    before = _intent_files(harness.project)
    process = multiprocessing.get_context("fork").Process(
        target=_crash_after_real_approval_put,
        args=(harness.service, payload),
    )

    process.start()
    process.join(10)
    assert process.exitcode == 73
    recovered = load_runtime(harness.project)
    assert recovered.graph_store.load() == harness.runtime.graph_store.load()
    assert _intent_files(harness.project) == before


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


def test_connector_membership_drift_during_commit_rolls_back_all_runtime_targets(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    proposal_id, _evidence_ref = _bootstrap_proposal(harness)
    payload = harness.sign(harness.service.proposal_preview(proposal_id))
    connector_path = harness.project / ".intent/connectors/concurrent.yaml"

    def add_connector(stage: str) -> None:
        if stage == "target:history":
            connector_path.write_text("schema_version: 1\n", encoding="utf-8")

    harness.runtime.transactions._fault_hook = add_connector
    before = harness.state()

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        harness.service.apply_decision(b"signed-assertion", payload)

    assert connector_path.read_text(encoding="utf-8") == "schema_version: 1\n"
    assert harness.state() == before
