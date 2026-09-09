"""Public, no-private-key-transport team enrollment ceremony."""

from __future__ import annotations

import base64
import hashlib
import json
import traceback
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from intent_engineering.control_plane.models import CredentialRecord
from intent_engineering.control_plane.webauthn_service import (
    PythonWebAuthnVerifier,
    VerifiedAuthentication,
    VerifiedHumanDecision,
)
from intent_engineering.team_state.authority import (
    authority_digest,
    derive_member_id,
    issue_device_certificate,
)
from intent_engineering.team_state.enrollment import (
    TeamEnrollmentError,
    TeamEnrollmentService,
    VerifiedRemoteStateV2,
    build_join_decision_payload,
    build_sponsor_decision_payload,
    export_join_response,
    export_team_invite,
    parse_join_response,
    parse_team_invite,
)
from intent_engineering.team_state.keys import (
    DeviceEnrollmentBinding,
    GitHubIdentity,
    KeyringDeviceKeyStore,
)
from intent_engineering.team_state.models import (
    CiRecipientRecord,
    DeviceCertificateClaimsV2,
    MemberRecordV2,
    TeamAuthorityPolicyV2,
    TeamAuthorityRegistryV2,
    TeamRootTrustV2,
)
from intent_engineering.team_state.signing import RootEnrollmentBinding

NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)
PROJECT = "project"
REPOSITORY = "github.com/acme/project"
IDENTITY_PROOF = b"one-time-github-oauth-proof"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class _Backend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        del self.values[(service, account)]


class _RootStore:
    def __init__(self) -> None:
        self._private = Ed25519PrivateKey.from_private_bytes(b"r" * 32)
        public = _b64(self._private.public_key().public_bytes_raw())
        from intent_engineering.team_state.authority import derive_root_key_id

        self._root = TeamRootTrustV2(
            project_id=PROJECT,
            repository_id=REPOSITORY,
            authority_epoch=1,
            root_key_id=derive_root_key_id(PROJECT, REPOSITORY, public),
            root_public_key=public,
            created_at=NOW - timedelta(days=10),
        )

    def create(self, binding: RootEnrollmentBinding) -> TeamRootTrustV2:
        del binding
        return self._root

    def root_trust(self) -> TeamRootTrustV2:
        return self._root

    def sign(self, root_key_id: str, preimage: bytes) -> bytes:
        assert root_key_id == self._root.root_key_id
        return self._private.sign(preimage)


class _Verifier:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def authentication_options(self, request: object) -> bytes:
        del request
        return b"options"

    def verify_authentication(self, response: bytes, request: object) -> VerifiedAuthentication:
        self.requests.append(request)
        body = json.loads(response)
        assert body["origin"] == request.expected_origin
        assert body["rp_id"] == request.rp_id
        return VerifiedAuthentication(
            credential_id=base64.urlsafe_b64decode(body["credential_id"] + "=="),
            new_sign_count=body["new_sign_count"],
            user_verified=body["user_verified"],
        )


class _IdentityLookup:
    def __init__(self, identities: dict[int, GitHubIdentity]) -> None:
        self.identities = identities
        self.calls: list[int] = []

    def lookup(self, account_id: int) -> GitHubIdentity:
        self.calls.append(account_id)
        return self.identities[account_id]


class _GitHubVerifier:
    def __init__(self, identity: GitHubIdentity) -> None:
        self.identity = identity
        self.proof_digests: list[str] = []

    def verify(self, proof: bytes) -> GitHubIdentity:
        self.proof_digests.append(hashlib.sha256(proof).hexdigest())
        if proof != IDENTITY_PROOF:
            raise ValueError("invalid GitHub proof")
        return self.identity


def _assertion(credential: CredentialRecord, *, origin: str = "https://intent.local") -> bytes:
    return json.dumps(
        {
            "credential_id": credential.credential_id,
            "new_sign_count": credential.sign_count,
            "origin": origin,
            "rp_id": "localhost",
            "user_verified": True,
        },
        sort_keys=True,
    ).encode()


def _credential(account: int, login: str, actor: str) -> CredentialRecord:
    repository_id = (
        "repo:sha256:"
        + hashlib.sha256(
            b"intent.team-enrollment.local-repository.v2\0" + REPOSITORY.encode()
        ).hexdigest()
    )
    return CredentialRecord(
        id=f"credential:{account}",
        project_id=PROJECT,
        repository_id=repository_id,
        actor=actor,
        credential_id=_b64(f"credential-{account}".encode()),
        public_key=_b64((f"public-{account}" * 4).encode()),
        sign_count=7,
        created_at=NOW - timedelta(days=2),
        local_only=False,
        github_account_id=str(account),
        github_login=login,
    )


def _device_store(
    tmp_path: object,
    *,
    account: int,
    login: str,
    device_digit: str,
    recipient: bytes,
    signing: bytes,
) -> KeyringDeviceKeyStore:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    binding = DeviceEnrollmentBinding(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        actor=f"github:{account}",
        github_account_id=account,
        github_login=login,
        device_id="device:" + device_digit * 32,
    )
    return KeyringDeviceKeyStore(
        binding,
        backend=_Backend(),
        recipient_private_key_source=lambda: recipient,
        signing_private_key_source=lambda: signing,
        lock_root=tmp_path / f"locks-{account}",
    )


def _fixture(
    tmp_path: object,
) -> tuple[
    TeamEnrollmentService,
    TeamEnrollmentService,
    VerifiedRemoteStateV2,
    GitHubIdentity,
    CredentialRecord,
]:
    root_store = _RootStore()
    a_store = _device_store(
        tmp_path,
        account=100,
        login="alice",
        device_digit="1",
        recipient=b"a" * 32,
        signing=b"s" * 32,
    )
    a_binding = DeviceEnrollmentBinding(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        actor="github:100",
        github_account_id=100,
        github_login="alice",
        device_id="device:" + "1" * 32,
    )
    a_public = a_store.create(a_binding)
    a_credential = _credential(100, "alice", "github:100")
    claims = DeviceCertificateClaimsV2(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        authority_epoch=1,
        member_id=derive_member_id(PROJECT, REPOSITORY, 100),
        device_id=a_public.device_id,
        github_account_id=100,
        github_login="alice",
        recipient_key_id=a_public.recipient_key_id,
        recipient_public_key=_b64(a_public.recipient_public_key),
        signature_id=a_public.signature_id,
        signing_public_key=_b64(a_public.signing_public_key),
        webauthn_credential_digest="sha256:"
        + hashlib.sha256(a_credential.canonical_bytes()).hexdigest(),
        serial=1,
        issued_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=365),
    )
    certificate = issue_device_certificate(claims, root_store)
    member = MemberRecordV2(
        member_id=claims.member_id,
        actor="github:100",
        github_account_id=100,
        github_login="alice",
        role="sponsor",
        status="active",
        device_certificate_ids=(certificate.certificate_id,),
        enrolled_at=claims.issued_at,
    )
    ci = CiRecipientRecord(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        runner_id="intent-runner",
        public_key=_b64(
            X25519PrivateKey.from_private_bytes(b"c" * 32).public_key().public_bytes_raw()
        ),
    )
    authority = TeamAuthorityRegistryV2(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        authority_epoch=1,
        sequence=1,
        root=root_store.root_trust(),
        policy=TeamAuthorityPolicyV2(),
        members=(member,),
        device_certificates=(certificate,),
        revocations=(),
        ci_recipient=ci,
        previous_authority_digest=None,
    )
    state = VerifiedRemoteStateV2(
        authority=authority,
        state_commit="1" * 40,
        bundle_digest="sha256:" + "b" * 64,
        default_branch="main",
        default_branch_commit="2" * 40,
        tooling_digest="sha256:" + "d" * 64,
    )
    b_store = _device_store(
        tmp_path,
        account=200,
        login="bob",
        device_digit="2",
        recipient=b"b" * 32,
        signing=b"t" * 32,
    )
    a_service = TeamEnrollmentService(
        device_store=a_store,
        root_store=root_store,
        sponsor_certificate_id=certificate.certificate_id,
        webauthn_verifier=_Verifier(),
        expected_origin="https://intent.local",
        expected_rp_id="localhost",
        github_identity_verifier=_GitHubVerifier(GitHubIdentity(account_id="200", login="bob")),
        identity_lookup=_IdentityLookup(
            {
                100: GitHubIdentity(account_id="100", login="alice"),
                200: GitHubIdentity(account_id="200", login="bob"),
            }
        ),
        nonce_source=lambda: b"n" * 32,
        challenge_private_key_source=lambda: b"e" * 32,
    )
    b_service = TeamEnrollmentService(device_store=b_store)
    return (
        a_service,
        b_service,
        state,
        GitHubIdentity(account_id="200", login="bob"),
        _credential(200, "bob", "github:200"),
    )


def _join(
    tmp_path: object,
) -> tuple[TeamEnrollmentService, TeamEnrollmentService, VerifiedRemoteStateV2, object, object]:
    a_service, b_service, state, identity, credential = _fixture(tmp_path)
    invite = a_service.create_invite(state=state, intended_identity=identity, now=NOW)
    material = b_service.device_public_material(invite=invite, local_identity=identity)
    payload = build_join_decision_payload(
        invite=invite,
        identity=identity,
        material=material,
        credential=credential,
        identity_proof=IDENTITY_PROOF,
        pre_assertion_sign_count=6,
        challenge=b"j" * 32,
        now=NOW,
    )
    decision = VerifiedHumanDecision(payload=payload, credential=credential, verified_at=NOW)
    response = b_service.create_join_response(
        invite=invite,
        local_identity=identity,
        decision=decision,
        identity_proof=IDENTITY_PROOF,
        pre_assertion_sign_count=6,
        webauthn_assertion=_assertion(credential),
        now=NOW,
    )
    return a_service, b_service, state, invite, response


def test_join_decision_expires_with_a_nearly_expired_invitation(tmp_path: object) -> None:
    """Catches a decision lifetime extending past the public invitation it authorizes."""
    a_service, b_service, state, identity, credential = _fixture(tmp_path)
    invite = a_service.create_invite(
        state=state,
        intended_identity=identity,
        now=NOW - timedelta(hours=23, minutes=58),
    )
    material = b_service.device_public_material(invite=invite, local_identity=identity)

    payload = build_join_decision_payload(
        invite=invite,
        identity=identity,
        material=material,
        credential=credential,
        identity_proof=IDENTITY_PROOF,
        pre_assertion_sign_count=6,
        challenge=b"j" * 32,
        now=NOW,
    )

    assert payload.expires_at == invite.expires_at


def test_public_exchange_uses_independent_keyrings_and_approves_exact_transition(
    tmp_path: object,
) -> None:
    """Catches transport of B's private key or an unsigned/unbound authority update."""
    a_service, b_service, state, invite, response = _join(tmp_path)

    invite_bytes = export_team_invite(invite)
    response_bytes = export_join_response(response)
    assert parse_team_invite(invite_bytes) == invite
    assert parse_join_response(response_bytes) == response
    assert len(invite_bytes) <= 32 * 1024
    assert len(response_bytes) <= 64 * 1024
    assert IDENTITY_PROOF not in response_bytes
    assert not hasattr(a_service, "create_join_challenge")
    assert "ceremony" not in type(response).model_fields
    assert _b64(b"b" * 32).encode() not in invite_bytes + response_bytes
    assert _b64(b"t" * 32).encode() not in invite_bytes + response_bytes

    preview = a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)
    sponsor_credential = _credential(100, "alice", "github:100")
    payload = build_sponsor_decision_payload(
        preview=preview,
        credential=sponsor_credential,
        challenge=b"k" * 32,
        now=NOW,
    )
    approved = a_service.approve(
        preview=preview,
        sponsor_decision=VerifiedHumanDecision(
            payload=payload, credential=sponsor_credential, verified_at=NOW
        ),
        sponsor_pre_assertion_sign_count=6,
        sponsor_assertion=_assertion(sponsor_credential),
        current=state,
        now=NOW,
    )

    assert approved.authority.sequence == 2
    assert approved.authority.previous_authority_digest == authority_digest(state.authority)
    assert {member.github_account_id for member in approved.authority.members} == {100, 200}
    assert approved.attestation.operation == "enroll"
    assert approved.attestation.authority_digest == authority_digest(approved.authority)
    assert b_service not in vars(a_service).values()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("project_id", "other"),
        ("repository_id", "github.com/acme/other"),
        ("base_state_commit", "3" * 40),
        ("base_bundle_digest", "sha256:" + "3" * 64),
        ("default_branch_commit", "4" * 40),
        ("tooling_digest", "sha256:" + "4" * 64),
    ),
)
def test_approval_rejects_stale_or_cross_scope_invitation(
    tmp_path: object, field: str, value: str
) -> None:
    """Catches previewing against a repository, root, base, or tooling not signed by A."""
    a_service, _b_service, state, invite, response = _join(tmp_path)
    changed = invite.model_copy(update={field: value})
    with pytest.raises(TeamEnrollmentError, match="^team enrollment changed$"):
        a_service.preview_approval(invite=changed, response=response, current=state, now=NOW)


def test_invite_expiry_future_and_identity_rename_fail_closed(tmp_path: object) -> None:
    """Catches stale invitations, future timestamps, and login/account substitution."""
    a_service, b_service, state, identity, credential = _fixture(tmp_path)
    invite = a_service.create_invite(state=state, intended_identity=identity, now=NOW)
    material = b_service.device_public_material(invite=invite, local_identity=identity)
    decision = VerifiedHumanDecision(
        payload=build_join_decision_payload(
            invite=invite,
            identity=identity,
            material=material,
            credential=credential,
            identity_proof=IDENTITY_PROOF,
            pre_assertion_sign_count=6,
            challenge=b"j" * 32,
            now=NOW,
        ),
        credential=credential,
        verified_at=NOW,
    )
    for bad_identity, at in (
        (GitHubIdentity(account_id="201", login="bob"), NOW),
        (GitHubIdentity(account_id="200", login="bob-renamed"), NOW),
        (identity, NOW + timedelta(hours=25)),
        (identity, NOW - timedelta(minutes=6)),
    ):
        with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
            b_service.create_join_response(
                invite=invite,
                local_identity=bad_identity,
                decision=decision,
                identity_proof=IDENTITY_PROOF,
                pre_assertion_sign_count=6,
                webauthn_assertion=_assertion(credential),
                now=at,
            )


def test_tampered_public_material_and_webauthn_binding_are_rejected(tmp_path: object) -> None:
    """Catches Ed25519/X25519 proof and WebAuthn subject/credential substitution."""
    a_service, _b_service, state, invite, response = _join(tmp_path)
    tampered_values = (
        {"device_possession_signature": _b64(b"0" * 64)},
        {"recipient_possession_proof": _b64(b"0" * 32)},
        {"signing_public_key": _b64(b"0" * 32)},
        {"credential": response.credential.model_copy(update={"public_key": _b64(b"wrong" * 8)})},
        {"github_identity_proof_ciphertext": _b64(b"0" * 64)},
    )
    for values in tampered_values:
        hostile = response.model_copy(update=values)
        with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
            a_service.preview_approval(invite=invite, response=hostile, current=state, now=NOW)


def test_replay_duplicate_member_and_non_sponsor_fail_closed(tmp_path: object) -> None:
    """Catches approving an invite twice, duplicate identity, or member-issued enrollment."""
    a_service, _b_service, state, invite, response = _join(tmp_path)
    preview = a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)
    credential = _credential(100, "alice", "github:100")
    decision = VerifiedHumanDecision(
        payload=build_sponsor_decision_payload(
            preview=preview, credential=credential, challenge=b"k" * 32, now=NOW
        ),
        credential=credential,
        verified_at=NOW,
    )
    a_service.approve(
        preview=preview,
        sponsor_decision=decision,
        sponsor_pre_assertion_sign_count=6,
        sponsor_assertion=_assertion(credential),
        current=state,
        now=NOW,
    )
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.approve(
            preview=preview,
            sponsor_decision=decision,
            sponsor_pre_assertion_sign_count=6,
            sponsor_assertion=_assertion(credential),
            current=state,
            now=NOW,
        )

    sponsor = state.authority.members[0].model_copy(update={"role": "member"})
    non_sponsor = state.model_copy(
        update={"authority": state.authority.model_copy(update={"members": (sponsor,)})}
    )
    fresh_root = tmp_path / "fresh"
    fresh_root.mkdir()
    fresh_a, _fresh_b, _state, fresh_invite, fresh_response = _join(fresh_root)
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        fresh_a.preview_approval(
            invite=fresh_invite, response=fresh_response, current=non_sponsor, now=NOW
        )


def test_public_parsers_reject_duplicate_and_oversized_hostile_json_without_leak(
    tmp_path: object,
) -> None:
    """Catches ambiguous JSON, resource exhaustion, and hostile values in fixed errors."""
    _a, _b, _state, invite, response = _join(tmp_path)
    duplicate = export_team_invite(invite).replace(b'{"', b'{"schema_version":2,"', 1)
    secret = "private-token-should-not-leak"
    for parser, content in (
        (parse_team_invite, duplicate),
        (parse_team_invite, json.dumps({"secret": secret, "padding": "x" * 33000}).encode()),
        (parse_join_response, json.dumps({"secret": secret, "padding": "x" * 66000}).encode()),
        (parse_join_response, export_join_response(response) + b" "),
    ):
        with pytest.raises(TeamEnrollmentError) as caught:
            parser(content)
        assert str(caught.value) in {"team enrollment unavailable", "team enrollment changed"}
        assert secret not in repr(caught.value)


def test_capacity_and_duplicate_device_are_rejected_before_root_signing(tmp_path: object) -> None:
    """Catches exceeding the 32-member/63-device/64-recipient authority bounds."""
    a_service, _b_service, state, invite, response = _join(tmp_path)
    # A duplicate account is rejected even if the response otherwise has valid proofs.
    duplicate = response.model_copy(
        update={"github_account_id": 100, "github_login": "alice", "actor": "github:100"}
    )
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.preview_approval(invite=invite, response=duplicate, current=state, now=NOW)

    full_policy = state.authority.policy.model_copy(update={"max_active_members": 1})
    # Bypass model construction deliberately to emulate a lower live deployment cap.
    object.__setattr__(state.authority, "policy", full_policy)
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)


def test_public_failures_scrub_hostile_values_and_preserve_cancellation(tmp_path: object) -> None:
    """Catches enrollment wrappers retaining stores, private challenges, or attacker text."""
    secret = "hostile-private-token"
    content = json.dumps({"secret": secret}).encode()
    with pytest.raises(TeamEnrollmentError) as caught:
        parse_join_response(content)
    local_reprs = [
        repr(frame.f_locals)
        for frame, _line in traceback.walk_tb(caught.value.__traceback__)
        if frame.f_globals.get("__name__") == "intent_engineering.team_state.enrollment"
    ]
    assert all(secret not in value for value in local_reprs)

    class Cancelled(BaseException):
        pass

    cancellation = Cancelled("ephemeral-private-token")

    def cancel() -> bytes:
        raise cancellation

    a_service, _b_service, state, identity, _credential_value = _fixture(tmp_path)
    a_service._challenge_source = cancel
    with pytest.raises(Cancelled) as cancelled:
        a_service.create_invite(state=state, intended_identity=identity, now=NOW)
    assert cancelled.value is cancellation
    assert cancelled.value.args == ()
    frames = [
        frame
        for frame, _line in traceback.walk_tb(cancelled.value.__traceback__)
        if frame.f_globals.get("__name__") == "intent_engineering.team_state.enrollment"
    ]
    assert all(a_service not in frame.f_locals.values() for frame in frames)
    assert all("challenge_private" not in frame.f_locals for frame in frames)


def test_duplicate_invitation_identifier_never_replaces_retained_challenge(
    tmp_path: object,
) -> None:
    """Catches deterministic/randomness failure overwriting an issued invite's proof key."""
    a_service, _b_service, state, identity, _credential_value = _fixture(tmp_path)
    first = a_service.create_invite(state=state, intended_identity=identity, now=NOW)
    assert a_service._challenge_store.has(first.invite_id)

    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.create_invite(state=state, intended_identity=identity, now=NOW)

    assert a_service._challenge_store.has(first.invite_id)


def test_approve_recomputes_preview_instead_of_attesting_caller_supplied_authority(
    tmp_path: object,
) -> None:
    """Catches a forged preview promoting B or otherwise changing the computed registry."""
    a_service, _b_service, state, invite, response = _join(tmp_path)
    preview = a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)
    forged_members = tuple(
        member.model_copy(update={"role": "sponsor"}) if member.github_account_id == 200 else member
        for member in preview.authority_after.members
    )
    forged_authority = preview.authority_after.model_copy(update={"members": forged_members})
    forged = preview.model_copy(
        update={
            "authority_after": forged_authority,
            "authority_after_digest": authority_digest(forged_authority),
        }
    )
    credential = _credential(100, "alice", "github:100")
    decision = VerifiedHumanDecision(
        payload=build_sponsor_decision_payload(
            preview=forged, credential=credential, challenge=b"k" * 32, now=NOW
        ),
        credential=credential,
        verified_at=NOW,
    )

    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.approve(
            preview=forged,
            sponsor_decision=decision,
            sponsor_pre_assertion_sign_count=6,
            sponsor_assertion=_assertion(credential),
            current=state,
            now=NOW,
        )


def test_a_independently_verifies_join_assertion_origin_rp_uv_and_counter(tmp_path: object) -> None:
    """Catches accepting B's serialized decision receipt as cryptographic proof."""
    a_service, _b_service, state, invite, response = _join(tmp_path)
    bad_assertion = _assertion(
        response.webauthn_decision.credential, origin="https://attacker.invalid"
    )
    hostile = response.model_copy(update={"webauthn_assertion": _b64(bad_assertion)})

    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.preview_approval(invite=invite, response=hostile, current=state, now=NOW)


def test_python_webauthn_verifier_receives_exact_pre_assertion_counter(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches re-verifying against the post-assertion counter, which rejects a valid assertion."""
    a_service, _b_service, state, invite, response = _join(tmp_path)
    a_service._webauthn_verifier = PythonWebAuthnVerifier()
    captured: list[dict[str, object]] = []
    parsed = SimpleNamespace(
        raw_id=base64.urlsafe_b64decode(response.credential.credential_id + "==")
    )
    monkeypatch.setattr(
        "intent_engineering.control_plane.webauthn_service.parse_authentication_credential_json",
        lambda _value: parsed,
    )

    def verify(**kwargs: object) -> object:
        captured.append(kwargs)
        return SimpleNamespace(
            credential_id=parsed.raw_id,
            new_sign_count=response.credential.sign_count,
            user_verified=True,
        )

    monkeypatch.setattr(
        "intent_engineering.control_plane.webauthn_service.verify_authentication_response",
        verify,
    )

    a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)

    assert captured[0]["credential_current_sign_count"] == 6
    assert captured[0]["credential_public_key"] == base64.urlsafe_b64decode(
        response.credential.public_key + "=="
    )


def test_encrypted_github_proof_rejects_wrong_proof_and_authoritative_rename(
    tmp_path: object,
) -> None:
    """Catches treating self-asserted identity fields or live lookup alone as identity proof."""
    a_service, b_service, state, identity, credential = _fixture(tmp_path)
    invite = a_service.create_invite(state=state, intended_identity=identity, now=NOW)
    material = b_service.device_public_material(invite=invite, local_identity=identity)
    wrong_proof = b"wrong-one-time-github-proof"
    payload = build_join_decision_payload(
        invite=invite,
        identity=identity,
        material=material,
        credential=credential,
        identity_proof=wrong_proof,
        pre_assertion_sign_count=6,
        challenge=b"j" * 32,
        now=NOW,
    )
    response = b_service.create_join_response(
        invite=invite,
        local_identity=identity,
        decision=VerifiedHumanDecision(payload=payload, credential=credential, verified_at=NOW),
        identity_proof=wrong_proof,
        pre_assertion_sign_count=6,
        webauthn_assertion=_assertion(credential),
        now=NOW,
    )
    assert wrong_proof not in export_join_response(response)
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$") as caught:
        a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)
    frames = [
        frame
        for frame, _line in traceback.walk_tb(caught.value.__traceback__)
        if frame.f_globals.get("__name__") == "intent_engineering.team_state.enrollment"
    ]
    assert all(wrong_proof not in frame.f_locals.values() for frame in frames)

    fresh = tmp_path / "renamed"
    fresh.mkdir()
    a_service, _b_service, state, invite, response = _join(fresh)
    verifier = a_service._github_identity_verifier
    assert isinstance(verifier, _GitHubVerifier)
    verifier.identity = GitHubIdentity(account_id="200", login="bob-renamed")
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)


@pytest.mark.parametrize(
    "change",
    (
        {"rp_id": "attacker.invalid"},
        {"user_verified": False},
        {"new_sign_count": 8},
    ),
)
def test_join_assertion_rejects_wrong_rp_uv_or_counter(
    tmp_path: object, change: dict[str, object]
) -> None:
    a_service, _b_service, state, invite, response = _join(tmp_path)
    credential = response.webauthn_decision.credential
    body = json.loads(_assertion(credential))
    body.update(change)
    hostile = response.model_copy(
        update={"webauthn_assertion": _b64(json.dumps(body, sort_keys=True).encode())}
    )
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.preview_approval(invite=invite, response=hostile, current=state, now=NOW)

    equal_nonzero = response.model_copy(update={"webauthn_pre_assertion_sign_count": 7})
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.preview_approval(
            invite=invite,
            response=equal_nonzero,
            current=state,
            now=NOW,
        )


def test_zero_counter_authenticator_allows_only_zero_to_zero(tmp_path: object) -> None:
    """Catches rejecting the WebAuthn-defined 0→0 counter case or allowing another equality."""
    a_service, b_service, state, identity, credential = _fixture(tmp_path)
    credential = credential.model_copy(update={"sign_count": 0})
    invite = a_service.create_invite(state=state, intended_identity=identity, now=NOW)
    material = b_service.device_public_material(invite=invite, local_identity=identity)
    payload = build_join_decision_payload(
        invite=invite,
        identity=identity,
        material=material,
        credential=credential,
        identity_proof=IDENTITY_PROOF,
        pre_assertion_sign_count=0,
        challenge=b"j" * 32,
        now=NOW,
    )
    response = b_service.create_join_response(
        invite=invite,
        local_identity=identity,
        decision=VerifiedHumanDecision(payload=payload, credential=credential, verified_at=NOW),
        identity_proof=IDENTITY_PROOF,
        pre_assertion_sign_count=0,
        webauthn_assertion=_assertion(credential),
        now=NOW,
    )

    assert (
        a_service.preview_approval(
            invite=invite,
            response=response,
            current=state,
            now=NOW,
        ).response
        == response
    )


def test_preview_and_approve_use_actual_now_not_replayed_preview_timestamp(
    tmp_path: object,
) -> None:
    """Catches reusing an expired response or sponsor assertion with deterministic cert time."""
    a_service, _b_service, state, invite, response = _join(tmp_path)
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.preview_approval(
            invite=invite,
            response=response,
            current=state,
            now=NOW + timedelta(minutes=6),
        )

    preview = a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)
    credential = _credential(100, "alice", "github:100")
    decision = VerifiedHumanDecision(
        payload=build_sponsor_decision_payload(
            preview=preview, credential=credential, challenge=b"k" * 32, now=NOW
        ),
        credential=credential,
        verified_at=NOW,
    )
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.approve(
            preview=preview,
            sponsor_decision=decision,
            sponsor_pre_assertion_sign_count=6,
            sponsor_assertion=_assertion(credential),
            current=state,
            now=NOW + timedelta(minutes=6),
        )


def test_live_identity_and_sponsor_credential_are_rechecked_at_approval(tmp_path: object) -> None:
    """Catches login/account drift and a sponsor assertion from an uncertified credential."""
    a_service, _b_service, state, invite, response = _join(tmp_path)
    lookup = a_service._identity_lookup
    assert isinstance(lookup, _IdentityLookup)
    lookup.identities[200] = GitHubIdentity(account_id="200", login="bob-renamed")
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)
    lookup.identities[200] = GitHubIdentity(account_id="200", login="bob")

    preview = a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)
    wrong = _credential(100, "alice", "github:100").model_copy(
        update={"public_key": _b64(b"different-sponsor-public-key" * 2)}
    )
    decision = VerifiedHumanDecision(
        payload=build_sponsor_decision_payload(
            preview=preview, credential=wrong, challenge=b"k" * 32, now=NOW
        ),
        credential=wrong,
        verified_at=NOW,
    )
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.approve(
            preview=preview,
            sponsor_decision=decision,
            sponsor_pre_assertion_sign_count=6,
            sponsor_assertion=_assertion(wrong),
            current=state,
            now=NOW,
        )


def test_sponsor_assertion_is_verified_through_trusted_local_boundary(tmp_path: object) -> None:
    a_service, _b_service, state, invite, response = _join(tmp_path)
    preview = a_service.preview_approval(invite=invite, response=response, current=state, now=NOW)
    credential = _credential(100, "alice", "github:100")
    decision = VerifiedHumanDecision(
        payload=build_sponsor_decision_payload(
            preview=preview, credential=credential, challenge=b"k" * 32, now=NOW
        ),
        credential=credential,
        verified_at=NOW,
    )
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.approve(
            preview=preview,
            sponsor_decision=decision,
            sponsor_pre_assertion_sign_count=6,
            sponsor_assertion=_assertion(credential, origin="https://attacker.invalid"),
            current=state,
            now=NOW,
        )


@pytest.mark.parametrize("at", (NOW - timedelta(days=2), NOW + timedelta(days=366)))
def test_create_invite_rejects_future_or_expired_sponsor_certificate(
    tmp_path: object, at: datetime
) -> None:
    """Catches an unissued/expired certificate remaining usable because its member is active."""
    a_service, _b_service, state, identity, _credential_value = _fixture(tmp_path)
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.create_invite(
            state=state,
            intended_identity=identity,
            now=at,
        )


def test_full_join_preimage_binds_transition_certificate_state_and_expiry(tmp_path: object) -> None:
    """Catches proof reuse after changing any sponsor-computed join transition field."""
    a_service, _b_service, state, invite, response = _join(tmp_path)
    changed = response.proposed_certificate_claims.model_copy(update={"github_login": "mallory"})
    hostile = response.model_copy(update={"proposed_certificate_claims": changed})
    with pytest.raises(TeamEnrollmentError, match="^team enrollment unavailable$"):
        a_service.preview_approval(invite=invite, response=hostile, current=state, now=NOW)
