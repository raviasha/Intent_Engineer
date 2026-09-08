"""End-to-end team recipient enrollment through GitHub identity and WebAuthn."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from intent_engineering.control_plane.service import ControlPlaneError, ControlPlaneService
from intent_engineering.control_plane.web import build_control_plane_app
from intent_engineering.control_plane.webauthn_service import (
    RegistrationRequest,
    VerifiedRegistration,
)
from intent_engineering.team_state.keys import (
    GitHubIdentity,
    InMemoryRecipientKeyStore,
    RecipientEnrollmentBinding,
    RecipientKeyStore,
)
from tests.e2e.test_intent_dev_web_runtime import _run
from tests.integration.control_plane.test_service import (
    NOW,
    ORIGIN,
    _harness,
    _registration_response,
    _Verifier,
)

CSRF = "team-enrollment-csrf"


@dataclass
class _IdentityVerifier:
    identity: GitHubIdentity
    expected_proof: bytes = b"github-device-proof"
    failure: BaseException | None = None

    def verify(self, proof: bytes) -> GitHubIdentity:
        if self.failure is not None:
            raise self.failure
        if proof != self.expected_proof:
            raise ValueError("identity proof mismatch")
        return self.identity


class _Stores:
    def __init__(self) -> None:
        self.bindings: list[RecipientEnrollmentBinding] = []
        self.stores: list[RecipientKeyStore] = []

    def __call__(self, binding: RecipientEnrollmentBinding) -> RecipientKeyStore:
        for previous, store in zip(self.bindings, self.stores, strict=True):
            if previous == binding:
                return store
        self.bindings.append(binding)
        store = InMemoryRecipientKeyStore(binding, private_key_source=lambda: b"r" * 32)
        self.stores.append(store)
        return store


class _TeamVerifier(_Verifier):
    def verify_registration(
        self, response: bytes, request: RegistrationRequest
    ) -> VerifiedRegistration:
        assert response == _registration_response(request)
        if self.registration_hook is not None:
            self.registration_hook()
        return VerifiedRegistration(
            credential_id=b"team-control-plane-credential",
            public_key=b"team-control-plane-public-key",
            sign_count=0,
            user_verified=True,
        )


def _team_service(
    tmp_path,
    *,
    login: str = "asha",
    aliases: tuple[str, ...] = ("github:asha",),
    clock=None,
    challenge_source=None,
    stores: _Stores | None = None,
):
    harness = _harness(tmp_path, actor="local:owner", aliases=aliases)
    harness.service.close()
    verifier = _TeamVerifier()
    harness.verifier = verifier
    identity = _IdentityVerifier(GitHubIdentity(account_id="101", login=login))
    stores = stores or _Stores()
    service = ControlPlaneService(
        harness.runtime,
        origin=ORIGIN,
        clock=clock or (lambda: NOW),
        challenge_source=challenge_source or (lambda: b"t" * 32),
        webauthn_verifier=verifier,
        team_repository_id="github.com/acme/project",
        github_identity_verifier=identity,
        recipient_key_store_factory=stores,
    )
    return harness, service, identity, stores


def _headers() -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "Cookie": f"intent_csrf={CSRF}",
        "X-Intent-CSRF": CSRF,
        "Content-Type": "application/json",
    }


def test_team_enrollment_binds_verified_identity_webauthn_and_recipient_key(tmp_path) -> None:
    """Catches team enrollment completing without all four authority identities agreeing."""
    harness, service, _identity, stores = _team_service(tmp_path)
    try:
        options = service.team_enrollment_options(b"github-device-proof")
        assert json.loads(options)["publicKey"]["userVerification"] == "required"
        request = harness.verifier.registration_requests[-1]

        record = service.complete_team_enrollment(_registration_response(request))

        assert record.project_id == harness.runtime.config.project_id
        assert record.repository_id == "github.com/acme/project"
        assert record.actor == "local:owner"
        assert record.github_account_id == "101"
        assert record.github_login == "asha"
        assert record.webauthn_credential_id == "dGVhbS1jb250cm9sLXBsYW5lLWNyZWRlbnRpYWw"
        assert (
            stores.bindings[0].webauthn_credential_public_key
            == record.webauthn_credential_public_key
        )
        assert stores.stores[0].private_key(record.key_id) == b"r" * 32
        assert service.team_enrollment_status() == {
            "schema_version": 1,
            "status": "enrolled",
            "repository_id": "github.com/acme/project",
            "recipient_key_id": record.key_id,
            "github_login": "asha",
        }
    finally:
        service.close()
        harness.runtime.close()


def test_team_enrollment_stays_local_only_until_webauthn_completes(tmp_path) -> None:
    """Catches identity verification alone presenting a publishable team enrollment."""
    harness, service, _identity, stores = _team_service(tmp_path)
    try:
        assert service.team_enrollment_status()["status"] == "local_only"
        service.team_enrollment_options(b"github-device-proof")

        assert service.team_enrollment_status()["status"] == "local_only"
        assert stores.stores == []
    finally:
        service.close()
        harness.runtime.close()


@pytest.mark.parametrize("login", ("mallory", "Asha"))
def test_wrong_or_noncanonical_github_identity_cannot_start_enrollment(
    tmp_path, login: str
) -> None:
    """Catches provider identity bypassing exact model or approved actor-alias policy."""
    if login == "Asha":
        with pytest.raises(ValueError):
            GitHubIdentity(account_id="101", login=login)
        return
    harness, service, _identity, stores = _team_service(tmp_path, login=login)
    try:
        with pytest.raises(ControlPlaneError, match="control plane unavailable"):
            service.team_enrollment_options(b"github-device-proof")
        assert stores.stores == []
    finally:
        service.close()
        harness.runtime.close()


def test_duplicate_or_replacement_enrollment_is_rejected(tmp_path) -> None:
    """Catches a later credential ceremony silently replacing an enrolled recipient."""
    harness, service, _identity, stores = _team_service(tmp_path)
    try:
        service.team_enrollment_options(b"github-device-proof")
        first_request = harness.verifier.registration_requests[-1]
        service.complete_team_enrollment(_registration_response(first_request))

        with pytest.raises(ControlPlaneError, match="control plane unavailable"):
            service.team_enrollment_options(b"github-device-proof")
        assert len(stores.stores) == 1
    finally:
        service.close()
        harness.runtime.close()


def test_cancelled_account_a_response_cannot_complete_account_b_enrollment(tmp_path) -> None:
    """Catches a revoked GitHub-A challenge being relabeled as GitHub-B enrollment."""
    challenges = iter((b"a" * 32, b"b" * 32))
    harness, service, identity, stores = _team_service(
        tmp_path,
        aliases=("github:asha", "github:bela"),
        challenge_source=lambda: next(challenges),
    )
    try:
        service.team_enrollment_options(b"github-device-proof")
        account_a = harness.verifier.registration_requests[-1]
        assert service.cancel_team_enrollment()["status"] == "cancelled"
        identity.identity = GitHubIdentity(account_id="202", login="bela")
        service.team_enrollment_options(b"github-device-proof")

        with pytest.raises(ControlPlaneError, match="control plane unavailable"):
            service.complete_team_enrollment(_registration_response(account_a))

        assert service.team_enrollment_status()["status"] == "local_only"
        assert stores.stores == []
    finally:
        service.close()
        harness.runtime.close()


def test_cancel_and_complete_have_one_linearizable_outcome(tmp_path) -> None:
    """Catches cancellation reporting success before an in-flight completion publishes."""
    harness, service, _identity, _stores = _team_service(tmp_path)
    cancel_started = threading.Barrier(2)
    cancel_result: dict[str, object] = {}
    cancel_workers: list[threading.Thread] = []

    def cancel() -> None:
        cancel_started.wait()
        cancel_result.update(service.cancel_team_enrollment())

    def during_verification() -> None:
        worker = threading.Thread(target=cancel)
        cancel_workers.append(worker)
        worker.start()
        cancel_started.wait()
        worker.join(timeout=1)

    harness.verifier.registration_hook = during_verification
    try:
        service.team_enrollment_options(b"github-device-proof")
        request = harness.verifier.registration_requests[-1]
        completed = False
        try:
            service.complete_team_enrollment(_registration_response(request))
            completed = True
        except ControlPlaneError:
            pass
        cancel_workers[0].join(timeout=3)
        assert not cancel_workers[0].is_alive()

        cancelled = cancel_result.get("status") == "cancelled"
        assert (cancelled, completed) in {(True, False), (False, True)}
        assert service.team_enrollment_status()["status"] == (
            "local_only" if cancelled else "enrolled"
        )
    finally:
        service.close()
        harness.runtime.close()


def test_enrolled_recipient_is_reconstructed_after_service_restart(tmp_path) -> None:
    """Catches restart forgetting durable enrollment and permitting credential replacement."""
    stores = _Stores()
    harness, service, identity, stores = _team_service(tmp_path, stores=stores)
    service.team_enrollment_options(b"github-device-proof")
    request = harness.verifier.registration_requests[-1]
    enrolled = service.complete_team_enrollment(_registration_response(request))
    service.close()

    restarted = ControlPlaneService(
        harness.runtime,
        origin=ORIGIN,
        clock=lambda: NOW,
        challenge_source=lambda: b"u" * 32,
        webauthn_verifier=harness.verifier,
        team_repository_id="github.com/acme/project",
        github_identity_verifier=identity,
        recipient_key_store_factory=stores,
    )
    try:
        assert restarted.team_enrollment_status() == {
            "schema_version": 1,
            "status": "enrolled",
            "repository_id": "github.com/acme/project",
            "recipient_key_id": enrolled.key_id,
            "github_login": "asha",
        }
        with pytest.raises(ControlPlaneError, match="control plane unavailable"):
            restarted.team_enrollment_options(b"github-device-proof")
    finally:
        restarted.close()
        harness.runtime.close()


def test_expired_pending_enrollment_is_revoked_before_fresh_options(tmp_path) -> None:
    """Catches an abandoned ceremony permanently blocking or authenticating a later attempt."""
    current = [NOW]
    challenges = iter((b"a" * 32, b"b" * 32))
    harness, service, _identity, stores = _team_service(
        tmp_path,
        clock=lambda: current[0],
        challenge_source=lambda: next(challenges),
    )
    try:
        service.team_enrollment_options(b"github-device-proof")
        expired = harness.verifier.registration_requests[-1]
        current[0] = NOW + timedelta(minutes=6)

        service.team_enrollment_options(b"github-device-proof")

        assert len(harness.verifier.registration_requests) == 2
        with pytest.raises(ControlPlaneError, match="control plane unavailable"):
            service.complete_team_enrollment(_registration_response(expired))
        assert stores.stores == []
    finally:
        service.close()
        harness.runtime.close()


def test_team_enrollment_cancellation_preserves_type_and_creates_no_key(tmp_path) -> None:
    """Catches cancellation becoming a generic failure or leaving recipient material behind."""

    class Cancelled(BaseException):
        pass

    harness, service, identity, stores = _team_service(tmp_path)
    identity.failure = Cancelled("device flow cancelled")
    try:
        with pytest.raises(Cancelled) as caught:
            service.team_enrollment_options(b"github-device-proof")
        assert caught.value.__cause__ is None
        assert stores.stores == []
        assert service.team_enrollment_status()["status"] == "local_only"
    finally:
        service.close()
        harness.runtime.close()


def test_browser_team_enrollment_endpoint_and_cancel_are_secret_free(tmp_path) -> None:
    """Catches the browser path returning identity proof or retaining enrollment after cancel."""
    harness, service, _identity, stores = _team_service(tmp_path)
    client = TestClient(
        build_control_plane_app(service, origin=ORIGIN, csrf_secret=CSRF), base_url=ORIGIN
    )
    try:
        options = client.post(
            "/api/v1/team/enrollment/options",
            content=b'{"identity_proof":"github-device-proof"}',
            headers=_headers(),
        )
        assert options.status_code == 200
        assert "github-device-proof" not in options.text

        cancelled = client.post(
            "/api/v1/team/enrollment/cancel",
            content=b"{}",
            headers=_headers(),
        )
        assert cancelled.json() == {"schema_version": 1, "status": "cancelled"}
        assert service.team_enrollment_status()["status"] == "local_only"
        assert stores.stores == []
    finally:
        service.close()
        harness.runtime.close()


def test_shipped_browser_completes_team_enrollment_without_rendering_identity_proof() -> None:
    """Catches the team-state UI skipping GitHub proof, WebAuthn, cleanup, or status refresh."""
    result = _run(
        r"""
await settle(); respond(take("/_intent/browser/bootstrap"), { status: "ok" }); await settle();
respond(take("/api/v1/status"), statusProjection); await settle();
respond(take("/api/v1/development/observation"), { current_revision: "a", command_ids: [], changed_paths: [], evidence_candidates: [] }); await settle();
nav.find((item) => item.dataset.view === "team_state").click(); await settle();
respond(take("/api/v1/team/enrollment"), { schema_version: 1, status: "local_only", repository_id: "github.com/acme/project", recipient_key_id: null, github_login: null }); await settle();
const proof = walk(app).find((node) => node.tagName === "input" && node.type === "password");
proof.value = "PRIVATE-GITHUB-PROOF";
button("Verify GitHub identity and enroll this device").click(); await settle();
const options = take("/api/v1/team/enrollment/options"); const submitted = JSON.parse(options.init.body);
respond(options, { publicKey: { challenge: "Y2hhbGxlbmdl", user: { id: "bG9jYWwtb3duZXI", name: "local:owner", displayName: "Local owner" }, authenticatorSelection: { userVerification: "preferred" } } }); await settle();
const verify = take("/api/v1/team/enrollment/verify"); respond(verify, { schema_version: 1, key_id: "recipient:sha256:key" }); await settle();
const refresh = take("/api/v1/team/enrollment"); respond(refresh, { schema_version: 1, status: "enrolled", repository_id: "github.com/acme/project", recipient_key_id: "recipient:sha256:key", github_login: "asha" }); await settle();
process.stdout.write(JSON.stringify({ submitted, proofValue: proof.value, app: app.textContent, status: status.textContent, userVerification: lastCreateOptions.publicKey.authenticatorSelection.userVerification }));
"""
    )

    assert result["submitted"] == {"identity_proof": "PRIVATE-GITHUB-PROOF"}
    assert result["proofValue"] == ""
    assert "PRIVATE-GITHUB-PROOF" not in result["app"]
    assert result["userVerification"] == "required"
    assert "Team recipient enrolled" in result["status"]
