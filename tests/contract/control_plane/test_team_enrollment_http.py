"""Second-developer handoff uses the existing hostile HTTP boundary."""

from __future__ import annotations

from starlette.testclient import TestClient

from intent_engineering.control_plane.web import build_control_plane_app
from tests.e2e.test_team_enrollment import CSRF, _headers
from tests.helpers.shared_state import git
from tests.integration.control_plane.test_service import ORIGIN, _harness


def _session(tmp_path):
    from intent_engineering.control_plane.team_enrollment import save_enrollment_request
    from intent_engineering.team_state.keys import GitHubIdentity

    harness = _harness(tmp_path)
    git(harness.project, "init", "--initial-branch=main")
    git(harness.project, "remote", "add", "origin", "https://github.com/acme/project.git")
    request = save_enrollment_request(
        harness.runtime,
        action="invite",
        project_id=harness.runtime.config.project_id,
        repository_id="github.com/acme/project",
        identity=GitHubIdentity(account_id="200", login="bob"),
        output=str(tmp_path / "public-invite.json"),
    )
    return harness, request


def test_browser_receives_opaque_session_public_preview_and_can_cancel_before_writes(tmp_path):
    """Catches disclosure of local paths or browser cancellation losing durable state."""
    harness, request = _session(tmp_path)
    try:
        client = TestClient(
            build_control_plane_app(harness.service, origin=ORIGIN, csrf_secret=CSRF),
            base_url=ORIGIN,
        )
        status = client.get("/api/v1/team/membership")
        assert status.status_code == 200
        assert status.json()["session_id"] == request.session_id
        assert status.json()["can_cancel"] is True
        assert status.json()["identity"] == {"account_id": "200", "login": "bob"}
        assert str(tmp_path) not in status.text
        assert "default-src 'self'" in status.headers["content-security-policy"]
        rejected = client.post(
            "/api/v1/team/membership/cancel", json={"session_id": "0" * 64}, headers=_headers()
        )
        assert rejected.status_code == 503
        assert client.get("/api/v1/team/membership").json()["session_id"] == request.session_id
        cancelled = client.post(
            "/api/v1/team/membership/cancel",
            json={"session_id": request.session_id},
            headers=_headers(),
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"
        assert client.get("/api/v1/team/membership").json() == {"state": "unconfigured"}
    finally:
        harness.service.close()
        harness.runtime.close()


def test_enrollment_posts_reject_foreign_origin_and_missing_csrf_without_mutation(tmp_path):
    """Catches a new enrollment route bypassing the shared CSRF/origin checks."""
    harness, request = _session(tmp_path)
    try:
        client = TestClient(
            build_control_plane_app(harness.service, origin=ORIGIN, csrf_secret=CSRF),
            base_url=ORIGIN,
        )
        for action in (
            "register-options",
            "register-verify",
            "preview",
            "options",
            "verify",
            "cancel",
            "create-invite",
            "reconcile",
            "restart",
            "publish-preview",
            "publish-options",
            "publish-verify",
            "publish-reconcile",
            "publish-restart",
        ):
            for headers in ({"Origin": "https://attacker.example"}, {"Origin": ORIGIN}):
                response = client.post(
                    f"/api/v1/team/membership/{action}",
                    json={"session_id": request.session_id},
                    headers=headers,
                )
                assert response.status_code == 403
                assert response.json() == {
                    "schema_version": 1,
                    "status": "rejected",
                    "reason": "request_unavailable",
                }
        assert client.get("/api/v1/team/membership").json()["session_id"] == request.session_id
    finally:
        harness.service.close()
        harness.runtime.close()


def test_exact_approval_request_rehydrates_without_exposing_response_body(tmp_path):
    """Catches restart losing approved preimages or status reflecting sealed identity proof."""
    from intent_engineering.control_plane import team_enrollment as journey
    from tests.unit.team_state.test_setup import _prepared

    (tmp_path / "fixtures").mkdir()
    _state, preview, _publication, _proof, approval = _prepared(tmp_path / "fixtures")
    (tmp_path / "local").mkdir()
    harness = _harness(tmp_path / "local")
    git(harness.project, "init", "--initial-branch=main")
    git(harness.project, "remote", "add", "origin", "https://github.com/acme/project.git")
    try:
        request = journey.save_enrollment_request(
            harness.runtime,
            action="approve-join",
            project_id="project",
            repository_id=preview.invite.repository_id,
            response=preview.response,
        )
        journey.save_approval_request(harness.runtime, request, approval)
        reloaded = journey.load_approval_request(harness.runtime, request)
        assert reloaded.canonical_bytes() == approval.canonical_bytes()
        visible = journey.enrollment_status(harness.runtime)
        assert (
            visible["preview"]["authority_before_digest"]
            == approval.preview.authority_before_digest
        )
        assert (
            visible["preview"]["authority_after_digest"] == approval.preview.authority_after_digest
        )
        assert visible["preview"]["certificate"] == approval.preview.certificate.model_dump(
            mode="json"
        )
        assert visible["preview"]["github_preflight"] == approval.github_preflight.model_dump(
            mode="json"
        )
        assert preview.response.github_identity_proof_ciphertext not in str(visible)
        assert preview.response.webauthn_assertion not in str(visible)
    finally:
        harness.service.close()
        harness.runtime.close()


def test_live_http_service_replaces_cached_session_after_next_cli_command(tmp_path, monkeypatch):
    """The long-running control plane must observe the authoritative replacement session."""
    from intent_engineering.control_plane import team_enrollment as journey
    from intent_engineering.team_state.keys import GitHubIdentity
    from tests.unit.team_state.test_enrollment import _join

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _sponsor, _member, state, invite, response = _join(fixtures)
    local = tmp_path / "local"
    local.mkdir()
    harness = _harness(local)
    git(harness.project, "init", "--initial-branch=main")
    git(harness.project, "remote", "add", "origin", "https://github.com/acme/project.git")
    first = journey.save_enrollment_request(
        harness.runtime,
        action="invite",
        project_id="project",
        repository_id=state.authority.repository_id,
        identity=GitHubIdentity(account_id="200", login="bob"),
        output=str(tmp_path / "invite.json"),
        created_invite=invite,
    )

    async def action(self, _action, session_id, *, response=b""):
        assert session_id == self.request.session_id
        return {"state": "observed", "action": self.request.action}

    monkeypatch.setattr(journey.MembershipSession, "action", action)
    try:
        client = TestClient(
            build_control_plane_app(harness.service, origin=ORIGIN, csrf_secret=CSRF),
            base_url=ORIGIN,
        )
        assert (
            client.post(
                "/api/v1/team/membership/create-invite",
                json={"session_id": first.session_id},
                headers=_headers(),
            ).json()["action"]
            == "invite"
        )
        second = journey.save_enrollment_request(
            harness.runtime,
            action="approve-join",
            project_id="project",
            repository_id=state.authority.repository_id,
            response=response,
        )
        observed = client.post(
            "/api/v1/team/membership/preview",
            json={"session_id": second.session_id},
            headers=_headers(),
        )
        assert observed.status_code == 200
        assert observed.json()["action"] == "approve-join"
    finally:
        harness.service.close()
        harness.runtime.close()


def test_cancel_retires_exact_approval_before_a_fresh_invitation(tmp_path):
    """Cancellation must not leave approval metadata that poisons the next workflow."""
    from intent_engineering.control_plane import team_enrollment as journey
    from intent_engineering.team_state.keys import GitHubIdentity
    from tests.unit.team_state.test_setup import _prepared

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    state, preview, _publication, _proof, approval = _prepared(fixtures)
    local = tmp_path / "local"
    local.mkdir()
    harness = _harness(local)
    git(harness.project, "init", "--initial-branch=main")
    git(harness.project, "remote", "add", "origin", "https://github.com/acme/project.git")
    try:
        request = journey.save_enrollment_request(
            harness.runtime,
            action="approve-join",
            project_id="project",
            repository_id=state.authority.repository_id,
            response=preview.response,
        )
        journey.save_approval_request(harness.runtime, request, approval)
        assert journey.cancel_enrollment(harness.runtime, request.session_id) == {
            "state": "cancelled"
        }
        fresh = journey.save_enrollment_request(
            harness.runtime,
            action="invite",
            project_id="project",
            repository_id=state.authority.repository_id,
            identity=GitHubIdentity(account_id="201", login="carol"),
            output=str(tmp_path / "fresh-invite.json"),
        )
        assert journey.enrollment_status(harness.runtime)["session_id"] == fresh.session_id
        assert not (harness.project / ".intent" / "team-enrollment-approval.json").exists()
    finally:
        harness.service.close()
        harness.runtime.close()


def test_rejected_request_preserves_owner_only_readable_session(tmp_path):
    """Transaction rollback must not relax the mode of an existing opaque session."""
    import stat

    import pytest

    from intent_engineering.control_plane import team_enrollment as journey
    from intent_engineering.team_state.keys import GitHubIdentity

    harness, request = _session(tmp_path)
    target = harness.project / ".intent" / "team-enrollment-session.json"
    before = target.read_bytes()
    try:
        with pytest.raises(ValueError, match="team enrollment unavailable"):
            journey.save_enrollment_request(
                harness.runtime,
                action="invite",
                project_id=harness.runtime.config.project_id,
                repository_id="github.com/acme/project",
                identity=GitHubIdentity(account_id="201", login="carol"),
                output=str(tmp_path / "other-invite.json"),
            )
        assert target.read_bytes() == before
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert journey.load_enrollment_request(harness.runtime) == request
    finally:
        harness.service.close()
        harness.runtime.close()
