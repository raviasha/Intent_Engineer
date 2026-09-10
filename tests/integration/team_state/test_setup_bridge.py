"""Enrollment approval composes with the existing guarded GitHub receipts."""

from __future__ import annotations

import base64
from types import MethodType, SimpleNamespace

import pytest

import intent_engineering.team_state.github_publication as github_publication_module
import intent_engineering.team_state.setup as setup_state_module
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.team_state.enrollment import (
    EnrollmentPublicationPlanV2,
    enrollment_transition_plan_digest,
)
from intent_engineering.team_state.github import (
    GitHubProtectionPolicy,
    GitHubProtectionPreview,
    PublicationPullRequest,
)
from intent_engineering.team_state.setup import (
    EnrollmentApprovalRequest,
    GitHubSetupBridge,
    _draft,
    _enrollment_receipt,
)
from tests.unit.team_state.test_github_publication import _status
from tests.unit.team_state.test_setup import _prepared, _sponsor_decision


def _protection() -> GitHubProtectionPreview:
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
    return GitHubProtectionPreview(
        repository_id="github.com/acme/project",
        branch_creation_required=False,
        requires_change=False,
        before_policy=policy,
        after_policy=policy,
        digest="sha256:" + "3" * 64,
    )


def _request(preview, publication, protection) -> EnrollmentApprovalRequest:
    plan = EnrollmentPublicationPlanV2(
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
    return EnrollmentApprovalRequest(
        preview=preview,
        github_preflight=protection,
        publication_plan=plan,
        transition_plan_digest=enrollment_transition_plan_digest(preview, plan),
    )


class _Api:
    def __init__(self, close_signal: BaseException | None = None) -> None:
        self.closed = 0
        self.close_signal = close_signal

    async def aclose(self) -> None:
        self.closed += 1
        if self.close_signal is not None:
            raise self.close_signal

    async def request_json_object(self, method, path, **_kwargs):
        assert method == "GET" and path == "/users/bob"
        return SimpleNamespace(payload={"id": 200, "login": "bob"})


class _Enrollment:
    def __init__(self, preview, publication, proof) -> None:
        self.preview = preview
        self.publication = publication
        self.proof = proof

    def preview_approval(self, **_kwargs):
        return self.preview

    def approve(self, **kwargs):
        transition = object()
        kwargs["persist_approval"](transition)
        return transition

    def prepare_publication(self, **_kwargs):
        return self.publication

    def transition_proof(self, **_kwargs):
        return self.proof


@pytest.mark.anyio
@pytest.mark.parametrize("resolution", ["merged", "closed"])
async def test_normal_v2_member_publication_uses_guarded_existing_draft_lifecycle(
    tmp_path, monkeypatch, resolution
):
    """B publishes normally without sponsor/root authority or another CLI command."""
    from intent_engineering.cli.runtime import load_runtime
    from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
    from intent_engineering.team_state.enrollment import VerifiedRemoteStateV2
    from intent_engineering.team_state.keys import DeviceEnrollmentBinding
    from intent_engineering.team_state.publication import PublicationAuthorityV2, PublicationService
    from intent_engineering.team_state.restore import VerifiedReleaseV2
    from tests.unit.team_state.test_enrollment import NOW, _fixture

    _, preview, enrolled, _, _ = _prepared(tmp_path)
    runtime = load_runtime(tmp_path / "publication" / "project")
    _, member, _, _, _ = _fixture(tmp_path)
    response = preview.response
    member._device_store.create(
        DeviceEnrollmentBinding(
            project_id=response.project_id,
            repository_id=response.repository_id,
            actor=response.actor,
            github_account_id=response.github_account_id,
            github_login=response.github_login,
            device_id=response.device_id,
        )
    )
    parent = VerifiedReleaseV2(
        enrolled.manifest, enrolled.manifest_bytes, enrolled.authority, "3" * 40
    )
    authority = PublicationAuthorityV2(
        enrolled.authority,
        response.proposed_member.member_id,
        preview.certificate.certificate_id,
        parent,
        parent.commit,
    )
    bridge = object.__new__(GitHubSetupBridge)
    bridge.service = SimpleNamespace(_runtime=runtime, _now=lambda: NOW)
    bridge.repository = "acme/project"
    bridge.transport = setup_state_module._PublicationTransport()
    current = VerifiedRemoteStateV2(
        authority=enrolled.authority,
        state_commit=parent.commit,
        bundle_digest=parent.manifest.bundle_digest,
        default_branch="main",
        default_branch_commit="2" * 40,
        tooling_digest="sha256:" + "d" * 64,
    )
    status = _status().model_copy(update={"branch_commit": parent.commit})
    pr_state = "open"
    preflight_count = []

    async def preflight(_self, _api, _current, _certificate_id, *, require_sponsor=True):
        assert require_sponsor is False
        preflight_count.append(1)
        return status, _Client(_api), _protection()

    bridge._member_preflight = MethodType(preflight, bridge)

    class _Publisher:
        def __init__(self, guarded, _inspector, _reviewed):
            self.guarded = guarded

        async def publish(self, publication, *, base_commit):
            assert _draft(runtime).external_write_attempted
            assert publication.manifest.authority_digest == enrolled.manifest.authority_digest
            assert base_commit == parent.commit
            await self.guarded.before_write()
            return "4" * 40

    class _Client:
        def __init__(self, guarded, **_kwargs):
            self.guarded = guarded

        async def inspect(self, _repository):
            return status

        async def open_publication_pr(self, _publication, *, expected_head_commit):
            assert expected_head_commit == "4" * 40
            await self.guarded.before_write()
            return PublicationPullRequest(
                repository_id=current.authority.repository_id,
                number=9,
                url="https://github.com/acme/project/pull/9",
                created=True,
            )

        async def publication_pull_request_state(self, *_args, **_kwargs):
            return pr_state

        async def confirm_publication_merge(
            self,
            _publication,
            *,
            expected_head_commit,
            expected_base_commit,
            pull_request_number,
        ):
            assert expected_head_commit == "4" * 40
            assert expected_base_commit == parent.commit
            assert pull_request_number == 9
            return status

    monkeypatch.setattr(setup_state_module, "github_api", _Api)
    monkeypatch.setattr(setup_state_module, "GitHubTeamStateClient", _Client)
    monkeypatch.setattr(github_publication_module, "GitHubApiPublisher", _Publisher)
    publication = PublicationService(
        runtime,
        repository_id=response.repository_id,
        decision_repository_id=response.credential.repository_id,
        authority=lambda: authority,
        publisher=bridge.transport,
        device_signer=member._device_store,
    )
    try:
        reviewed = publication.preview(now=NOW)
        assert reviewed.payload.actor == "github:200"
        result = await bridge.publish_member_state(
            publication=publication,
            decision=VerifiedHumanDecision(reviewed.payload, response.credential, NOW),
            current=current,
            certificate_id=preview.certificate.certificate_id,
            protection=_protection(),
            verify_local=lambda: None,
        )
        assert result["state"] == "publication_pending"
        assert _draft(runtime).pull_request_number == 9
        assert len(preflight_count) >= 3
        recovered = await bridge.reconcile_member_publication(
            current=current,
            certificate_id=preview.certificate.certificate_id,
        )
        assert recovered["state"] == "publication_pending"
        if resolution == "merged":
            status = status.model_copy(update={"branch_commit": "5" * 40})
            recovered = await bridge.reconcile_member_publication(
                current=current,
                certificate_id=preview.certificate.certificate_id,
            )
            assert recovered["state"] == "published"
        else:
            pr_state = "closed"
            recovered = await bridge.reconcile_member_publication(
                current=current,
                certificate_id=preview.certificate.certificate_id,
            )
            assert recovered["state"] == "publication_closed"
            recovered = await bridge.reconcile_member_publication(
                current=current,
                certificate_id=preview.certificate.certificate_id,
                restart_closed=True,
            )
            assert recovered["state"] == "member-active"
        assert _draft(runtime) is None
    finally:
        runtime.close()


@pytest.mark.anyio
async def test_member_approval_stages_recovery_before_consuming_replay_token(
    tmp_path, monkeypatch
) -> None:
    state, preview, publication, proof, request = _prepared(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(workspace_directory=directory)
    bridge = object.__new__(GitHubSetupBridge)
    bridge.service = SimpleNamespace(_runtime=runtime, _now=lambda: preview.invite.created_at)
    bridge.repository = "acme/project"
    bridge.request = SimpleNamespace(preview=SimpleNamespace())
    protection = _protection()

    async def preflight(_self, _api, _current, _certificate_id):
        return _status(), object(), protection

    bridge._member_preflight = MethodType(preflight, bridge)
    monkeypatch.setattr(setup_state_module, "github_api", _Api)
    monkeypatch.setattr(
        setup_state_module,
        "_stage_enrollment_approval",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated process interruption")),
    )

    class _ReplayAwareEnrollment(_Enrollment):
        consumed = False

        def approve(self, **kwargs):
            transition = object()
            persist = kwargs.get("persist_approval")
            if persist is not None:
                persist(transition)
            self.consumed = True
            return transition

    enrollment = _ReplayAwareEnrollment(preview, publication, proof)
    try:
        with pytest.raises(ValueError, match="^team enrollment unavailable$"):
            await bridge.approve_member(
                request=request,
                enrollment=enrollment,  # type: ignore[arg-type]
                sponsor_decision=_sponsor_decision(preview, request),
                sponsor_pre_assertion_sign_count=6,
                sponsor_assertion=b"secret-assertion",
                current=state,
                parent=None,  # type: ignore[arg-type]
                snapshot=None,  # type: ignore[arg-type]
                now=preview.invite.created_at,
            )
        assert enrollment.consumed is False
    finally:
        directory.close()


@pytest.mark.anyio
async def test_member_approval_marks_attempt_before_publish_and_commit_before_pr(
    tmp_path, monkeypatch
) -> None:
    state, preview, publication, proof, request = _prepared(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(workspace_directory=directory)
    bridge = object.__new__(GitHubSetupBridge)
    bridge.service = SimpleNamespace(_runtime=runtime, _now=lambda: preview.invite.created_at)
    bridge.repository = "acme/project"
    bridge.request = SimpleNamespace(preview=SimpleNamespace())
    api = _Api()
    protection = _protection()
    preflights = 0

    async def preflight(_self, _api, _current, _certificate_id):
        nonlocal preflights
        preflights += 1
        return _status(), _Client(_api), protection

    bridge._member_preflight = MethodType(preflight, bridge)
    monkeypatch.setattr(setup_state_module, "github_api", lambda: api)

    class _Publisher:
        def __init__(self, guarded, _inspector, _reviewed) -> None:
            self.guarded = guarded

        async def publish(self, _publication, *, base_commit, transition_proof):
            assert base_commit == state.state_commit
            assert transition_proof == proof
            assert _draft(runtime).external_write_attempted is True
            assert _enrollment_receipt(runtime).phase == "publication-pending"
            await self.guarded.before_write()
            return "3" * 40

    class _Client:
        def __init__(self, guarded, **_kwargs) -> None:
            self.guarded = guarded

        async def inspect(self, _repository):
            return _status()

        async def open_publication_pr(
            self, _publication, *, expected_head_commit, transition_proof
        ):
            assert expected_head_commit == "3" * 40
            assert transition_proof == proof
            assert _draft(runtime).publication_commit == "3" * 40
            await self.guarded.before_write()
            return PublicationPullRequest(
                repository_id="github.com/acme/project",
                number=7,
                url="https://github.com/acme/project/pull/7",
                created=True,
            )

    monkeypatch.setattr(setup_state_module, "GitHubTeamStateClient", _Client)
    monkeypatch.setattr(github_publication_module, "GitHubApiPublisher", _Publisher)
    try:
        result = await bridge.approve_member(
            request=request,
            enrollment=_Enrollment(preview, publication, proof),  # type: ignore[arg-type]
            sponsor_decision=_sponsor_decision(preview, request),
            sponsor_pre_assertion_sign_count=6,
            sponsor_assertion=b"secret-assertion",
            current=state,
            parent=None,  # type: ignore[arg-type]
            snapshot=None,  # type: ignore[arg-type]
            now=preview.invite.created_at,
        )
        assert result.pull_request_url == "https://github.com/acme/project/pull/7"
        assert _enrollment_receipt(runtime).phase == "pr-pending"
        assert _draft(runtime).pull_request_number == 7
        assert preflights == 3
        assert api.closed == 1
    finally:
        directory.close()


@pytest.mark.anyio
async def test_lost_publication_response_preserves_both_recovery_receipts(
    tmp_path, monkeypatch
) -> None:
    state, preview, publication, proof, request = _prepared(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(workspace_directory=directory)
    bridge = object.__new__(GitHubSetupBridge)
    bridge.service = SimpleNamespace(_runtime=runtime, _now=lambda: preview.invite.created_at)
    bridge.repository = "acme/project"
    bridge.request = SimpleNamespace(preview=SimpleNamespace())
    api = _Api()
    protection = _protection()

    async def preflight(_self, _api, _current, _certificate_id):
        return _status(), _Client(_api), protection

    bridge._member_preflight = MethodType(preflight, bridge)
    monkeypatch.setattr(setup_state_module, "github_api", lambda: api)

    class _Client:
        def __init__(self, guarded, **_kwargs) -> None:
            self.guarded = guarded

        async def inspect(self, _repository):
            return _status()

    class _LostPublisher:
        def __init__(self, guarded, _inspector, _reviewed) -> None:
            self.guarded = guarded

        async def publish(self, _publication, *, base_commit, transition_proof):
            assert transition_proof == proof
            await self.guarded.before_write()
            raise TimeoutError("lost provider response with token=secret")

    monkeypatch.setattr(setup_state_module, "GitHubTeamStateClient", _Client)
    monkeypatch.setattr(github_publication_module, "GitHubApiPublisher", _LostPublisher)
    try:
        with pytest.raises(ValueError, match="^team enrollment unavailable$"):
            await bridge.approve_member(
                request=request,
                enrollment=_Enrollment(preview, publication, proof),  # type: ignore[arg-type]
                sponsor_decision=_sponsor_decision(preview, request),
                sponsor_pre_assertion_sign_count=6,
                sponsor_assertion=b"secret-assertion",
                current=state,
                parent=None,  # type: ignore[arg-type]
                snapshot=None,  # type: ignore[arg-type]
                now=preview.invite.created_at,
            )
        assert _draft(runtime).external_write_attempted is True
        assert _enrollment_receipt(runtime).phase == "publication-pending"
        assert "secret" not in (workspace / "team-enrollment-receipt.json").read_text()
    finally:
        directory.close()


@pytest.mark.anyio
async def test_publication_cancellation_preserves_identity_scrubs_traceback_and_receipts(
    tmp_path, monkeypatch
) -> None:
    state, preview, publication, proof, request = _prepared(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(workspace_directory=directory)
    bridge = object.__new__(GitHubSetupBridge)
    bridge.service = SimpleNamespace(_runtime=runtime, _now=lambda: preview.invite.created_at)
    bridge.repository = "acme/project"
    bridge.request = SimpleNamespace(preview=SimpleNamespace())
    cleanup = BaseException("secret-cleanup")
    cleanup.__dict__["token"] = "secret-cleanup-token"
    api = _Api(cleanup)
    protection = _protection()
    signal = BaseException("secret-cancellation")
    signal.__dict__["token"] = "secret-token"

    async def preflight(_self, _api, _current, _certificate_id):
        return _status(), _Client(_api), protection

    bridge._member_preflight = MethodType(preflight, bridge)
    monkeypatch.setattr(setup_state_module, "github_api", lambda: api)

    class _Client:
        def __init__(self, guarded, **_kwargs) -> None:
            self.guarded = guarded

        async def inspect(self, _repository):
            return _status()

    class _CancelledPublisher:
        def __init__(self, guarded, _inspector, _reviewed) -> None:
            self.guarded = guarded

        async def publish(self, _publication, *, base_commit, transition_proof):
            assert transition_proof == proof
            await self.guarded.before_write()
            raise signal

    monkeypatch.setattr(setup_state_module, "GitHubTeamStateClient", _Client)
    monkeypatch.setattr(github_publication_module, "GitHubApiPublisher", _CancelledPublisher)
    try:
        with pytest.raises(BaseException) as caught:
            await bridge.approve_member(
                request=request,
                enrollment=_Enrollment(preview, publication, proof),  # type: ignore[arg-type]
                sponsor_decision=_sponsor_decision(preview, request),
                sponsor_pre_assertion_sign_count=6,
                sponsor_assertion=b"secret-assertion",
                current=state,
                parent=None,  # type: ignore[arg-type]
                snapshot=None,  # type: ignore[arg-type]
                now=preview.invite.created_at,
            )
        assert caught.value is signal
        assert signal.args == () and signal.__dict__ == {}
        assert signal.__cause__ is None and signal.__context__ is None
        assert cleanup.args == () and cleanup.__dict__ == {}
        assert cleanup.__traceback__ is None
        traceback_cursor = signal.__traceback__
        values: list[str] = []
        while traceback_cursor is not None:
            values.extend(repr(value) for value in traceback_cursor.tb_frame.f_locals.values())
            traceback_cursor = traceback_cursor.tb_next
        retained = " ".join(values)
        assert "secret-assertion" not in retained and "secret-token" not in retained
        assert _draft(runtime).external_write_attempted is True
        assert _enrollment_receipt(runtime).phase == "publication-pending"
        assert api.closed == 1
    finally:
        directory.close()


@pytest.mark.anyio
async def test_restart_resumes_exact_approved_draft_without_replacing_authority(
    tmp_path, monkeypatch
) -> None:
    state, preview, publication, proof, request = _prepared(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(workspace_directory=directory)
    bridge = object.__new__(GitHubSetupBridge)
    bridge.service = SimpleNamespace(_runtime=runtime, _now=lambda: preview.invite.created_at)
    bridge.repository = "acme/project"
    bridge.request = SimpleNamespace(preview=SimpleNamespace())
    api = _Api()
    protection = _protection()
    status = _status(branch_commit=state.state_commit)
    pr_state = "open"

    async def preflight(_self, _api, _current, _certificate_id):
        return status, _Client(_api), protection

    bridge._member_preflight = MethodType(preflight, bridge)
    monkeypatch.setattr(setup_state_module, "github_api", lambda: api)

    class _Client:
        def __init__(self, guarded, **_kwargs) -> None:
            self.guarded = guarded

        async def inspect(self, _repository):
            return status

        async def open_publication_pr(
            self, _publication, *, expected_head_commit, transition_proof
        ):
            assert expected_head_commit == "3" * 40
            assert transition_proof == proof
            await self.guarded.before_write()
            return PublicationPullRequest(
                repository_id="github.com/acme/project",
                number=7,
                url="https://github.com/acme/project/pull/7",
                created=False,
            )

        async def publication_pull_request_state(self, *_args, **_kwargs):
            return pr_state

    class _RecoveredPublisher:
        def __init__(self, guarded, _inspector, _reviewed) -> None:
            self.guarded = guarded

        async def publish(self, _publication, *, base_commit, transition_proof):
            assert base_commit == state.state_commit
            assert transition_proof == proof
            assert _draft(runtime).external_write_attempted is True
            await self.guarded.before_write()
            return "3" * 40

    monkeypatch.setattr(setup_state_module, "GitHubTeamStateClient", _Client)
    monkeypatch.setattr(github_publication_module, "GitHubApiPublisher", _RecoveredPublisher)
    try:
        _draft(
            runtime,
            prepared=publication,
            transition_proof=proof,
            anchor=state.state_commit,
        )
        _enrollment_receipt(
            runtime,
            receipt=bridge._enrollment_receipt_for(
                request,
                publication,
                sponsor_decision=_sponsor_decision(preview, request),
                transition_proof=proof,
                phase="approved",
            ),
        )
        decision = _sponsor_decision(preview, request)
        result = await bridge.reconcile_member_approval(
            request=request,
            current=state,
            decision_repository_id=decision.credential.repository_id,
            decision_actor=decision.credential.actor,
        )
        assert result.pull_request_url == "https://github.com/acme/project/pull/7"
        assert _draft(runtime).publication_commit == "3" * 40
        assert _enrollment_receipt(runtime).phase == "pr-pending"
        assert _draft(runtime).authority == publication.authority
        pr_state = "closed"
        with pytest.raises(ValueError, match="requires reconciliation"):
            await bridge.reconcile_member_approval(
                request=request,
                current=state,
                decision_repository_id=decision.credential.repository_id,
                decision_actor=decision.credential.actor,
            )
        assert _enrollment_receipt(runtime).phase == "closed"
        restarted = await bridge.reconcile_member_approval(
            request=request,
            current=state,
            decision_repository_id=decision.credential.repository_id,
            decision_actor=decision.credential.actor,
            restart_closed=True,
        )
        assert restarted.state == "bootstrap_required"
        # The browser adapter atomically retires these together with its exact
        # session and approval metadata after this remote proof succeeds.
        assert _enrollment_receipt(runtime).phase == "closed"
        assert _draft(runtime).pull_request_number == 7
    finally:
        directory.close()


@pytest.mark.anyio
async def test_merge_reconciliation_persists_exact_merged_descendant_identity(
    tmp_path, monkeypatch
) -> None:
    state, preview, publication, proof, request = _prepared(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    directory = SecureDirectory.open(workspace)
    runtime = SimpleNamespace(workspace_directory=directory)
    bridge = object.__new__(GitHubSetupBridge)
    bridge.service = SimpleNamespace(_runtime=runtime, _now=lambda: preview.invite.created_at)
    bridge.repository = "acme/project"
    bridge.request = SimpleNamespace(preview=SimpleNamespace())
    api = _Api()
    merged_status = _status(branch_commit="4" * 40)

    class _MergedClient:
        def __init__(self, _api, **_kwargs) -> None:
            pass

        async def inspect(self, _repository):
            return merged_status

        async def confirm_publication_merge(
            self,
            candidate,
            *,
            expected_head_commit,
            expected_base_commit,
            pull_request_number,
            transition_proof,
        ):
            assert candidate == publication
            assert transition_proof == proof
            assert expected_head_commit == "3" * 40
            assert expected_base_commit == state.state_commit
            assert pull_request_number == 7
            return merged_status

    monkeypatch.setattr(setup_state_module, "github_api", lambda: api)
    monkeypatch.setattr(setup_state_module, "GitHubTeamStateClient", _MergedClient)
    approved_receipt = bridge._enrollment_receipt_for(
        request,
        publication,
        sponsor_decision=_sponsor_decision(preview, request),
        transition_proof=proof,
        phase="approved",
    )
    pending_receipt = approved_receipt.model_copy(update={"phase": "publication-pending"})
    receipt = pending_receipt.model_copy(
        update={
            "phase": "pr-pending",
            "publication_commit": "3" * 40,
            "pull_request_number": 7,
            "pull_request_url": "https://github.com/acme/project/pull/7",
        }
    )
    try:
        _enrollment_receipt(runtime, receipt=approved_receipt)
        _enrollment_receipt(runtime, receipt=pending_receipt)
        _enrollment_receipt(runtime, receipt=receipt)
        _draft(
            runtime,
            prepared=publication,
            transition_proof=proof,
            anchor=state.state_commit,
            external_write_attempted=True,
            publication_commit="3" * 40,
            pull_request_number=7,
            pull_request_url="https://github.com/acme/project/pull/7",
        )
        decision = _sponsor_decision(preview, request)
        result = await bridge.reconcile_member_approval(
            request=request,
            current=state,
            decision_repository_id=decision.credential.repository_id,
            decision_actor=decision.credential.actor,
        )
        merged = _enrollment_receipt(runtime)
        assert result.state == "published"
        assert merged.phase == "merged"
        assert merged.publication_commit == "3" * 40
        assert merged.merged_commit == "4" * 40
        assert merged.pull_request_number == 7
    finally:
        directory.close()
