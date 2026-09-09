"""Real local enrollment composition with only platform and provider boundaries replaced."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from types import SimpleNamespace

import anyio
import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.control_plane.service import ControlPlaneService
from intent_engineering.control_plane.webauthn_service import VerifiedAuthentication
from tests.e2e.test_team_enrollment import _TeamVerifier
from tests.helpers.shared_state import git, ready_project
from tests.integration.control_plane.test_service import ORIGIN, _registration_response
from tests.unit.team_state.test_enrollment import (
    IDENTITY_PROOF,
    NOW,
    _Backend,
    _fixture,
    _GitHubVerifier,
)


class _BrowserVerifier(_TeamVerifier):
    def authentication_options(self, request):
        self.authentication_request = request
        return json.dumps(
            {
                "publicKey": {
                    "challenge": base64.urlsafe_b64encode(request.challenge).rstrip(b"=").decode(),
                    "userVerification": "required",
                }
            }
        ).encode()

    def verify_authentication(self, response, request):
        body = json.loads(response)
        assert body["origin"] == request.expected_origin
        assert body["rp_id"] == request.rp_id
        return VerifiedAuthentication(
            base64.urlsafe_b64decode(body["credential_id"] + "=="), body["new_sign_count"], True
        )


def test_sponsor_invite_session_finishes_public_handoff_and_allows_next_command(
    tmp_path, monkeypatch
):
    """Session completion must permit approve-join without weakening in-flight ownership."""
    from intent_engineering.cli.team_enrollment import read_invite
    from intent_engineering.control_plane import team_enrollment as journey

    sponsor, _, state, identity, _ = _fixture(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    ready_project(project)
    git(project, "init", "--initial-branch=main")
    git(project, "remote", "add", "origin", "https://github.com/acme/project.git")
    runtime = load_runtime(project)
    service = ControlPlaneService(
        runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=_BrowserVerifier()
    )
    output = tmp_path / "invite.json"
    request = journey.save_enrollment_request(
        runtime,
        action="invite",
        project_id="project",
        repository_id=state.authority.repository_id,
        identity=identity,
        output=str(output),
    )

    async def context(self):
        return SimpleNamespace(enrollment=sponsor, current=state)

    monkeypatch.setattr(journey.MembershipSession, "_sponsor", context)

    async def run():
        session = journey.MembershipSession(service)
        try:
            result = await session.action("create-invite", request.session_id)
            assert result["state"] == "invitation-ready"
            assert result["can_cancel"] is True
        finally:
            session.close()
        assert read_invite(output).intended_github_account_id == 200
        restarted = journey.MembershipSession(service)
        try:
            assert (await restarted.action("create-invite", request.session_id))[
                "state"
            ] == "invitation-ready"
        finally:
            restarted.close()

    try:
        anyio.run(run)
        assert output.stat().st_mode & 0o777 == 0o600
        from tests.unit.team_state.test_enrollment import _join

        _, _, _, same_invite, response = _join(tmp_path)
        assert same_invite == read_invite(output)
        next_request = journey.save_enrollment_request(
            runtime,
            action="approve-join",
            project_id="project",
            repository_id=state.authority.repository_id,
            response=response,
        )
        assert next_request.session_id != request.session_id
    finally:
        service.close()
        runtime.close()


@pytest.mark.parametrize("export_failure", [False, True])
def test_b_local_browser_ceremony_writes_only_public_response_and_restart_receipt(
    tmp_path, monkeypatch, export_failure
):
    """Catches a browser adapter fabricating decisions or transporting B's private keys."""
    from intent_engineering.cli.team_enrollment import read_response
    from intent_engineering.control_plane import team_enrollment as journey
    from intent_engineering.team_state.keys import KeyringDeviceKeyStore
    from intent_engineering.team_state.local_trust import LocalTrustProvider

    sponsor, _, state, identity, _ = _fixture(tmp_path)
    invite = sponsor.create_invite(state=state, intended_identity=identity, now=NOW)
    project = tmp_path / "project"
    project.mkdir()
    ready_project(project)
    git(project, "init", "--initial-branch=main")
    git(project, "remote", "add", "origin", "https://github.com/acme/project.git")
    backend = _Backend()
    monkeypatch.setattr(journey, "local_identity_proof", lambda: IDENTITY_PROOF)
    monkeypatch.setattr(journey, "identity_verifier", lambda: _GitHubVerifier(identity))
    monkeypatch.setattr(
        journey,
        "device_store",
        lambda binding: KeyringDeviceKeyStore(
            binding, backend=backend, lock_root=tmp_path / "device-locks"
        ),
    )
    verifier = _BrowserVerifier()
    runtime = load_runtime(project)
    current = [NOW]
    service = ControlPlaneService(
        runtime, origin=ORIGIN, clock=lambda: current[0], webauthn_verifier=verifier
    )
    output = tmp_path / "response.json"
    request = journey.save_enrollment_request(
        runtime,
        action="join",
        project_id="project",
        repository_id=invite.repository_id,
        invite=invite,
        output=str(output),
    )

    async def run():
        session = journey.MembershipSession(service)
        try:
            registration = await session.action("register-options", request.session_id)
            assert registration["publicKey"]["userVerification"] == "required"
            await session.action(
                "register-verify",
                request.session_id,
                response=_registration_response(verifier.registration_requests[-1]),
            )
            review = await session.action("preview", request.session_id)
            assert review["identity"] == {"account_id": "200", "login": "bob"}
            options = await session.action("options", request.session_id)
            assert options["publicKey"]["userVerification"] == "required"
            credential = session.webauthn._current_credentials("github:200")[0]
            current[0] += timedelta(seconds=30)
            assertion = json.dumps(
                {
                    "credential_id": credential.credential_id,
                    "new_sign_count": 12,
                    "origin": ORIGIN,
                    "rp_id": "localhost",
                    "user_verified": True,
                }
            ).encode()
            if export_failure:
                from intent_engineering.cli import team_enrollment as files

                writer = files.write_public_file
                monkeypatch.setattr(
                    files,
                    "write_public_file",
                    lambda *_args: (_ for _ in ()).throw(OSError("interrupted public export")),
                )
                with pytest.raises(ValueError, match="team enrollment unavailable"):
                    await session.action("verify", request.session_id, response=assertion)
                session.close()
                monkeypatch.setattr(files, "write_public_file", writer)
                session = journey.MembershipSession(service)
                result = await session.action("reconcile", request.session_id)
            else:
                result = await session.action("verify", request.session_id, response=assertion)
            assert result["state"] == "response-ready"
            assert IDENTITY_PROOF.decode() not in json.dumps(result)
        finally:
            session.close()

    try:
        anyio.run(run)
        response = read_response(output)
        assert response.github_account_id == 200
        assert response.webauthn_decision.verified_at == NOW + timedelta(seconds=30)
        assert LocalTrustProvider(project).load_pending_join().response == response
        sponsor._expected_origin = ORIGIN
        assert (
            sponsor.preview_approval(
                invite=invite, response=response, current=state, now=current[0]
            ).response
            == response
        )
        restarted = journey.MembershipSession(service)
        try:
            assert journey.enrollment_status(runtime)["state"] == "response-ready"
        finally:
            restarted.close()
        all_public = output.read_bytes() + b"".join(
            path.read_bytes() for path in (project / ".intent").rglob("*") if path.is_file()
        )
        assert IDENTITY_PROOF not in all_public
        for private in backend.values.values():
            assert private.encode() not in all_public
    finally:
        service.close()
        runtime.close()


def test_sponsor_preview_rehydrates_exact_approval_after_restart(tmp_path, monkeypatch):
    from intent_engineering.control_plane import team_enrollment as journey
    from tests.unit.team_state.test_enrollment import _credential
    from tests.unit.team_state.test_setup import _prepared

    state, preview, _, _, approval = _prepared(tmp_path)
    project = tmp_path / "publication" / "project"
    git(project, "init", "--initial-branch=main")
    git(project, "remote", "add", "origin", "https://github.com/acme/project.git")
    runtime = load_runtime(project)
    service = ControlPlaneService(
        runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=_BrowserVerifier()
    )
    request = journey.save_enrollment_request(
        runtime,
        action="approve-join",
        project_id="project",
        repository_id=state.authority.repository_id,
        response=preview.response,
    )
    journey.save_approval_request(runtime, request, approval)
    sponsor, _, _, _, _ = _fixture(tmp_path)
    credential = _credential(100, "alice", "github:100")
    # Persist an existing sponsor credential, never enroll a replacement for this action.
    target = runtime.workspace_directory.file("team-webauthn-credentials.jsonl")
    target.atomic_write(credential.canonical_bytes())
    target.close()

    async def context(self):
        return SimpleNamespace(enrollment=sponsor, current=state, bridge=None)

    monkeypatch.setattr(journey.MembershipSession, "_sponsor", context)

    async def run():
        for _restart in range(2):
            session = journey.MembershipSession(service)
            try:
                result = await session.action("preview", request.session_id)
                assert result["state"] == "preview_ready"
                assert result["preview"]["approval_request_digest"] == approval.digest()
                assert session.pending_payload.result_digest == approval.digest()
                assert session.pending_payload.repository_id == credential.repository_id
                assert "encrypted_identity_proof" not in json.dumps(result)
                assert "webauthn_assertion" not in json.dumps(result)
            finally:
                session.close()

    try:
        anyio.run(run)
    finally:
        service.close()
        runtime.close()


def test_enrolled_member_opens_normal_publication_without_another_command(tmp_path, monkeypatch):
    from intent_engineering.control_plane import team_enrollment as journey
    from intent_engineering.team_state.keys import DeviceEnrollmentBinding
    from intent_engineering.team_state.local_trust import LocalTrustConfigV2
    from intent_engineering.team_state.publication import PublicationAuthorityV2, PublicationService
    from intent_engineering.team_state.restore import VerifiedReleaseV2
    from tests.unit.team_state.test_setup import _prepared

    _, preview, enrolled, _, _ = _prepared(tmp_path)
    response = preview.response
    project = tmp_path / "publication" / "project"
    git(project, "init", "--initial-branch=main")
    git(project, "remote", "add", "origin", "https://github.com/acme/project.git")
    runtime = load_runtime(project)
    service = ControlPlaneService(
        runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=_BrowserVerifier()
    )
    request = journey.save_enrollment_request(
        runtime,
        action="join",
        project_id="project",
        repository_id=response.repository_id,
        invite=preview.invite,
        output=str(tmp_path / "public-response.json"),
    )
    _, member, _, _, _ = _fixture(tmp_path)
    binding = DeviceEnrollmentBinding(
        project_id=response.project_id,
        repository_id=response.repository_id,
        actor=response.actor,
        github_account_id=response.github_account_id,
        github_login=response.github_login,
        device_id=response.device_id,
    )
    member._device_store.create(binding)
    parent = VerifiedReleaseV2(
        enrolled.manifest, enrolled.manifest_bytes, enrolled.authority, "3" * 40
    )
    trust = LocalTrustConfigV2(
        project_id=response.project_id,
        repository_id=response.repository_id,
        root=enrolled.authority.root,
        member_id=response.proposed_member.member_id,
        device_certificate_id=preview.certificate.certificate_id,
        recipient_key_id=response.recipient_key_id,
        signature_id=response.signature_id,
        accepted_authority_digest=enrolled.manifest.authority_digest,
        accepted_authority_sequence=enrolled.authority.sequence,
        accepted_bundle_digest=enrolled.manifest.bundle_digest,
        device_binding=binding,
    )
    target = runtime.workspace_directory.file("team-trust.json")
    target.atomic_write(trust.canonical_bytes())
    target.close()
    (project / ".intent").chmod(0o700)
    (project / ".intent" / "team-trust.json").chmod(0o600)
    credential_target = runtime.workspace_directory.file("team-webauthn-credentials.jsonl")
    credential_target.atomic_write(response.credential.canonical_bytes())
    credential_target.close()
    authority = PublicationAuthorityV2(
        enrolled.authority, trust.member_id, trust.device_certificate_id, parent, parent.commit
    )
    publication = PublicationService(
        runtime,
        repository_id=response.repository_id,
        decision_repository_id=response.credential.repository_id,
        authority=lambda: authority,
        publisher=SimpleNamespace(publish=lambda *_a, **_kw: None),
        device_signer=member._device_store,
    )

    async def context(self, *, require_sponsor=True):
        assert require_sponsor is False
        return SimpleNamespace(
            publication=publication,
            trust=trust,
            store=member._device_store,
            current=SimpleNamespace(authority=enrolled.authority, state_commit=parent.commit),
        )

    monkeypatch.setattr(journey.MembershipSession, "_sponsor", context)

    async def run():
        session = journey.MembershipSession(service)
        try:
            assert journey.enrollment_status(runtime)["state"] == "member-active"
            result = await session.action("publish-preview", request.session_id)
            assert result["state"] == "publication_preview"
            assert session.pending_payload.actor == "github:200"
            assert (await session.action("publish-options", request.session_id))["publicKey"][
                "userVerification"
            ] == "required"
        finally:
            session.close()

    try:
        anyio.run(run)
    finally:
        service.close()
        runtime.close()
