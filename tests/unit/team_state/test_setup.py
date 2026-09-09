"""Monotonic enrollment-publication handoff receipts."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

import intent_engineering.team_state.setup as setup_state_module
from intent_engineering.cli.team import GitHubEnablePreview
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.team_state.authority import authority_digest
from intent_engineering.team_state.enrollment import (
    EnrollmentPublicationPlanV2,
    enrollment_transition_plan_digest,
)
from intent_engineering.team_state.github import GitHubProtectionPolicy, GitHubProtectionPreview
from intent_engineering.team_state.models import (
    CanonicalStateFile,
    CanonicalStateSnapshot,
    CertifiedStateSignatureV2,
    TeamStateManifestV2,
)
from intent_engineering.team_state.restore import VerifiedReleaseV2
from intent_engineering.team_state.setup import (
    EnrollmentApprovalRequest,
    EnrollmentReceiptV2,
    GitHubSetupBridge,
    _complete_setup_request,
    _draft,
    _enrollment_receipt,
    _stage_enrollment_approval,
    load_setup_request,
    save_setup_request,
)
from tests.helpers.shared_state import canonical_files, ready_project
from tests.unit.team_state.test_enrollment import (
    NOW,
    PROJECT,
    REPOSITORY,
    _assertion,
    _credential,
    _join,
    build_sponsor_decision_payload,
)


def _prepared(tmp_path):
    sponsor, _member, state, invite, response = _join(tmp_path)
    preview = sponsor.preview_approval(invite=invite, response=response, current=state, now=NOW)
    credential = _credential(100, "alice", "github:100")
    source = tmp_path / "publication" / "project"
    source.mkdir(parents=True)
    ready_project(source)
    files = canonical_files(source)
    snapshot = CanonicalStateSnapshot(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        graph_version=1,
        files=tuple(CanonicalStateFile(path=path, content=files[path]) for path in sorted(files)),
    )
    manifest = TeamStateManifestV2(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        graph_version=1,
        parent_bundle_digest="sha256:" + "a" * 64,
        bundle_digest=state.bundle_digest,
        bundle_size=100,
        recipient_key_ids=state.authority.active_recipient_key_ids(),
        authority_digest=authority_digest(state.authority),
        authority_epoch=1,
        root_key_id=state.authority.root.root_key_id,
        created_at=NOW,
    )
    parent = VerifiedReleaseV2(
        manifest=manifest,
        manifest_bytes=manifest.canonical_bytes(),
        authority=state.authority,
        commit=state.state_commit,
        snapshot=snapshot,
    )
    plan = sponsor.plan_publication(snapshot=snapshot, parent=parent, preview=preview, now=NOW)
    request = _approval_request(preview, plan)
    decision = _sponsor_decision(preview, request)
    transition = sponsor.approve(
        preview=preview,
        sponsor_decision=decision,
        sponsor_pre_assertion_sign_count=6,
        sponsor_assertion=_assertion(credential),
        current=state,
        now=NOW,
        approval_request_digest=request.digest(),
    )
    publication = sponsor.prepare_publication(
        snapshot=snapshot, parent=parent, transition=transition, now=NOW, plan=plan
    )
    proof = sponsor.transition_proof(preview=preview, parent=parent, publication=publication)
    return state, preview, publication, proof, request


def _approval_request(preview, publication) -> EnrollmentApprovalRequest:
    policy = GitHubProtectionPolicy(
        snapshot_digest="sha256:" + "1" * 64,
        enforce_admins=True,
        allow_deletions=False,
        allow_force_pushes=False,
        required_linear_history=True,
        dismiss_stale_reviews=True,
        require_code_owner_reviews=True,
        required_approving_review_count=1,
        bypass_pull_request_allowances_empty=True,
        required_status_checks_strict=True,
        required_status_check_contexts=("Intent Engineering / state",),
        required_status_checks=(),
        restrictions_digest="sha256:" + "2" * 64,
    )
    protection = GitHubProtectionPreview(
        repository_id=REPOSITORY,
        branch_creation_required=False,
        requires_change=False,
        before_policy=policy,
        after_policy=policy,
        digest="sha256:" + "3" * 64,
    )
    plan = (
        publication
        if type(publication) is EnrollmentPublicationPlanV2
        else EnrollmentPublicationPlanV2(
            manifest=publication.manifest,
            authority=publication.authority,
            bundle=base64.urlsafe_b64encode(publication.bundle).rstrip(b"=").decode("ascii"),
            branch=publication.branch,
            bundle_path=publication.bundle_path,
            signature_path=publication.signature_path,
            snapshot_digest="sha256:" + "4" * 64,
            parent_manifest_digest="sha256:" + "5" * 64,
            parent_commit=preview.base_state_commit,
        )
    )
    return EnrollmentApprovalRequest(
        preview=preview,
        github_preflight=protection,
        publication_plan=plan,
        transition_plan_digest=enrollment_transition_plan_digest(preview, plan),
    )


def _sponsor_decision(preview, request: EnrollmentApprovalRequest) -> VerifiedHumanDecision:
    credential = _credential(100, "alice", "github:100")
    return VerifiedHumanDecision(
        payload=build_sponsor_decision_payload(
            preview=preview,
            credential=credential,
            challenge=b"k" * 32,
            now=NOW,
            approval_request_digest=request.digest(),
        ),
        credential=credential,
        verified_at=NOW,
    )


def test_v2_draft_round_trips_authority_without_changing_task6_receipt_contract(tmp_path) -> None:
    state, _preview, publication, proof, _request = _prepared(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(workspace_directory=directory)
    try:
        saved = _draft(  # type: ignore[arg-type]
            runtime,
            prepared=publication,
            transition_proof=proof,
            anchor=state.state_commit,
        )
        assert saved is not None and saved.authority == publication.authority
        assert saved.external_write_attempted is False
        restored = _draft(runtime)  # type: ignore[arg-type]
        assert restored is not None and restored.publication() == publication
        content = (workspace / "team-publication.json").read_text()
        assert "private_key" not in content and "content_key" not in content
    finally:
        directory.close()


def test_v2_enrollment_draft_rejects_forged_state_signature(tmp_path) -> None:
    state, _preview, publication, proof, _request = _prepared(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(workspace_directory=directory)
    forged_envelope = publication.envelope.model_copy(
        update={
            "signatures": (
                CertifiedStateSignatureV2(
                    certificate_id=publication.envelope.signatures[0].certificate_id,
                    signature_id=publication.envelope.signatures[0].signature_id,
                    signature="A" * 86,
                ),
            )
        }
    )
    forged = replace(
        publication,
        envelope=forged_envelope,
        signatures=forged_envelope.canonical_bytes(),
    )
    try:
        with pytest.raises(ValueError, match="publication authentication"):
            _draft(  # type: ignore[arg-type]
                runtime,
                prepared=forged,
                transition_proof=proof,
                anchor=state.state_commit,
            )
    finally:
        directory.close()


def test_sponsor_decision_binds_the_exact_publication_and_github_plan(tmp_path) -> None:
    _state, preview, _publication, _proof, request = _prepared(tmp_path)
    changed_plan = request.publication_plan.model_copy(
        update={"snapshot_digest": "sha256:" + "6" * 64}
    )
    changed = EnrollmentApprovalRequest(
        preview=preview,
        github_preflight=request.github_preflight,
        publication_plan=changed_plan,
        transition_plan_digest=enrollment_transition_plan_digest(preview, changed_plan),
    )
    credential = _credential(100, "alice", "github:100")

    approved = build_sponsor_decision_payload(
        preview=preview,
        credential=credential,
        challenge=b"k" * 32,
        now=NOW,
        approval_request_digest=request.digest(),
    )
    mutated = build_sponsor_decision_payload(
        preview=preview,
        credential=credential,
        challenge=b"k" * 32,
        now=NOW,
        approval_request_digest=changed.digest(),
    )

    assert approved.subject_digest != mutated.subject_digest
    assert approved.result_digest == request.digest()


def test_enrollment_receipt_is_bounded_public_and_monotonic(tmp_path) -> None:
    state, preview, publication, proof, request = _prepared(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(workspace_directory=directory)
    manifest_digest = "sha256:" + hashlib.sha256(publication.manifest_bytes).hexdigest()
    approved = EnrollmentReceiptV2(
        invite_id=preview.invite.invite_id,
        response_digest=preview.response_digest,
        authority_before_digest=preview.authority_before_digest,
        authority_after_digest=preview.authority_after_digest,
        publication_manifest_digest=manifest_digest,
        approval_request_digest=request.digest(),
        approved_github_preflight=request.github_preflight,
        sponsor_decision=_sponsor_decision(preview, request).payload,
        transition_proof=proof,
        phase="approved",
    )
    assert len(proof.canonical_bytes()) < 64 * 1024
    assert proof.parent_ci_recipient == state.authority.ci_recipient
    assert proof.sponsor_member == state.authority.members[0]
    assert proof.authority_before_digest == authority_digest(state.authority)
    with pytest.raises(ValueError, match="transition proof changed"):
        type(proof).model_validate(
            proof.model_dump(mode="python") | {"publication_manifest_digest": "sha256:" + "0" * 64}
        )
    pending = approved.model_copy(update={"phase": "publication-pending"})
    pr = pending.model_copy(
        update={
            "phase": "pr-pending",
            "publication_commit": "3" * 40,
            "pull_request_number": 7,
            "pull_request_url": "https://github.com/acme/project/pull/7",
        }
    )
    merged = pr.model_copy(update={"phase": "merged", "merged_commit": "4" * 40})
    try:
        assert _enrollment_receipt(runtime, receipt=approved) == approved  # type: ignore[arg-type]
        assert _enrollment_receipt(runtime, receipt=pending) == pending  # type: ignore[arg-type]
        assert _enrollment_receipt(runtime, receipt=pr) == pr  # type: ignore[arg-type]
        assert _enrollment_receipt(runtime, receipt=merged) == merged  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="changed"):
            _enrollment_receipt(runtime, receipt=pr)  # type: ignore[arg-type]
        content = (workspace / "team-enrollment-receipt.json").read_bytes()
        assert len(content) < 128 * 1024
        assert state.authority.ci_recipient.key_id.encode() in content
        assert b"private_key" not in content and b"content_key" not in content
    finally:
        directory.close()


def test_bridge_constructs_from_authenticated_context_after_setup_is_complete(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(
        workspace_directory=directory,
        config=SimpleNamespace(project_id=PROJECT, local_actor="alice"),
    )
    preview = GitHubEnablePreview(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        actor="alice",
        code_owner="@alice",
        codeowners_suggestion="/src/ @alice\n",
        workflow_suggestion="name: state\n",
        check_workflow_suggestion="name: check\n",
        preview_digest="pending",
    )
    preview = preview.model_copy(
        update={
            "preview_digest": setup_state_module._digest(
                preview.model_dump(mode="json", exclude={"state", "preview_digest"})
            )
        }
    )

    class Service:
        def __init__(self) -> None:
            self._runtime = runtime
            self._team_recipient = None
            self.restore_calls = 0

        def _restore_team_recipient(self) -> None:
            self.restore_calls += 1

    try:
        save_setup_request(runtime, preview)  # type: ignore[arg-type]
        request = load_setup_request(runtime)  # type: ignore[arg-type]
        assert request is not None
        _complete_setup_request(runtime, request)  # type: ignore[arg-type]
        assert load_setup_request(runtime) is None  # type: ignore[arg-type]

        service = Service()
        bridge = GitHubSetupBridge(service)  # type: ignore[arg-type]

        assert bridge.request == request
        assert service._team_repository_id == REPOSITORY
        assert service.restore_calls == 1
    finally:
        directory.close()


@pytest.mark.parametrize(
    "fault_stage",
    (
        "journal_prepared",
        "target:draft",
        "target:receipt",
        "journal_committed",
        "journal_cleaned",
    ),
)
def test_enrollment_approval_stages_draft_and_receipt_atomically(
    tmp_path, fault_stage: str
) -> None:
    state, preview, publication, proof, request = _prepared(tmp_path)
    decision = _sponsor_decision(preview, request)
    receipt = GitHubSetupBridge._enrollment_receipt_for(
        request,
        publication,
        sponsor_decision=decision,
        transition_proof=proof,
        phase="approved",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(workspace_directory=directory)
    signal = BaseException("simulated interruption")
    try:
        with pytest.raises(BaseException) as caught:
            _stage_enrollment_approval(
                runtime,  # type: ignore[arg-type]
                publication=publication,
                transition_proof=proof,
                anchor=state.state_commit,
                receipt=receipt,
                fault_hook=lambda stage: (
                    (_ for _ in ()).throw(signal) if stage == fault_stage else None
                ),
            )
        assert caught.value is signal
        assert _draft(runtime) is None  # type: ignore[arg-type]
        assert _enrollment_receipt(runtime) is None  # type: ignore[arg-type]

        _stage_enrollment_approval(
            runtime,  # type: ignore[arg-type]
            publication=publication,
            transition_proof=proof,
            anchor=state.state_commit,
            receipt=receipt,
        )
        assert _draft(runtime).publication() == publication  # type: ignore[arg-type,union-attr]
        assert _enrollment_receipt(runtime) == receipt  # type: ignore[arg-type]
    finally:
        directory.close()
