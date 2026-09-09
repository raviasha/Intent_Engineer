"""Version-dispatched parsing for public team-state artifacts."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

import pytest

from intent_engineering.team_state.authority import (
    derive_certificate_id,
    derive_member_id,
    derive_recipient_key_id,
    derive_root_key_id,
    derive_signature_id,
)
from intent_engineering.team_state.models import (
    CertifiedStateSignatureV2,
    DeviceCertificateClaimsV2,
    DeviceSignerCertificateV2,
    StateSignature,
    StateSignatureEnvelope,
    StateSignatureEnvelopeV2,
    TeamStateManifest,
    TeamStateManifestV2,
    canonical_manifest_bytes,
)
from intent_engineering.team_state.restore import parse_signature_envelope, parse_team_manifest

NOW = datetime(2026, 9, 9, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def test_version_dispatch_preserves_canonical_v1_types() -> None:
    manifest = TeamStateManifest(
        project_id="project",
        repository_id="github.com/acme/project",
        graph_version=1,
        bundle_digest=DIGEST_A,
        bundle_size=1,
        recipient_key_ids=("recipient:alice",),
        required_signature_ids=("signer:alice",),
        created_at=NOW,
    )
    envelope = StateSignatureEnvelope(
        manifest_digest=DIGEST_B,
        bundle_digest=DIGEST_A,
        signatures=(StateSignature(signature_id="signer:alice", signature=_b64(b"z" * 64)),),
    )

    assert parse_team_manifest(canonical_manifest_bytes(manifest)) == manifest
    assert parse_signature_envelope(envelope.canonical_bytes()) == envelope


def test_version_dispatch_accepts_exact_canonical_v2_types() -> None:
    repository = "github.com/acme/project"
    recipient_public = _b64(b"x" * 32)
    signing_public = _b64(b"s" * 32)
    root_public = _b64(b"r" * 32)
    claims = DeviceCertificateClaimsV2(
        project_id="project",
        repository_id=repository,
        authority_epoch=1,
        member_id=derive_member_id("project", repository, 1234),
        device_id="device:" + "1" * 32,
        github_account_id=1234,
        github_login="alice-dev",
        recipient_key_id=derive_recipient_key_id("project", repository, recipient_public),
        recipient_public_key=recipient_public,
        signature_id=derive_signature_id("project", repository, signing_public),
        signing_public_key=signing_public,
        webauthn_credential_digest="sha256:" + "c" * 64,
        serial=1,
        issued_at=NOW,
        expires_at=datetime(2027, 9, 9, tzinfo=UTC),
    )
    certificate = DeviceSignerCertificateV2(
        claims=claims,
        certificate_id=derive_certificate_id(claims),
        root_key_id=derive_root_key_id("project", repository, root_public),
        root_signature=_b64(b"r" * 64),
    )
    manifest = TeamStateManifestV2(
        project_id="project",
        repository_id=repository,
        graph_version=2,
        parent_bundle_digest=DIGEST_A,
        bundle_digest=DIGEST_B,
        bundle_size=2,
        recipient_key_ids=("recipient:ci:sha256:" + "1" * 64, claims.recipient_key_id),
        authority_digest="sha256:" + "d" * 64,
        authority_epoch=1,
        root_key_id=certificate.root_key_id,
        created_at=NOW,
    )
    envelope = StateSignatureEnvelopeV2(
        manifest_digest="sha256:" + "e" * 64,
        bundle_digest=DIGEST_B,
        authority_digest=manifest.authority_digest,
        certificates=(certificate,),
        signatures=(
            CertifiedStateSignatureV2(
                certificate_id=certificate.certificate_id,
                signature_id=claims.signature_id,
                signature=_b64(b"s" * 64),
            ),
        ),
    )

    assert parse_team_manifest(canonical_manifest_bytes(manifest)) == manifest
    assert parse_signature_envelope(envelope.canonical_bytes()) == envelope


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"schema_version":"2"}',
        b'{"schema_version":3}',
        b'{"schema_version":2,"schema_version":2}',
    ],
)
def test_version_dispatch_rejects_missing_typed_unknown_and_duplicate_versions(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_team_manifest(raw)
    with pytest.raises(ValueError):
        parse_signature_envelope(raw)


def test_exact_dual_signed_v1_to_v2_bridge_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a first v2 release that is not authorized by the exact legacy signer set."""
    from datetime import timedelta

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from intent_engineering.team_state.authority import (
        authority_digest,
        canonical_authority_attestation_preimage,
        canonical_state_signature_preimage,
    )
    from intent_engineering.team_state.models import (
        AuthorityAttestationV2,
        CertifiedStateSignatureV2,
        StateSignature,
        StateSignatureEnvelopeV2,
        TeamStateManifest,
        TeamStateManifestV2,
        V1MigrationBinding,
        V1MigrationProof,
    )
    from intent_engineering.team_state.restore import (
        TrustedSigningKey,
        VerifiedV1Release,
        verify_v1_migration,
    )
    from intent_engineering.team_state.signing import canonical_v1_migration_preimage
    from tests.unit.team_state.test_authority import (
        _real_certificate,
        _RootStore,
        _verified_authority,
    )

    legacy_private = Ed25519PrivateKey.from_private_bytes(b"l" * 32)
    legacy_manifest = TeamStateManifest(
        project_id="project",
        repository_id="github.com/acme/project",
        graph_version=2,
        bundle_digest=DIGEST_A,
        bundle_size=100,
        recipient_key_ids=("recipient:legacy",),
        required_signature_ids=("signer:legacy",),
        created_at=NOW - timedelta(days=1),
    )
    current = VerifiedV1Release(
        manifest=legacy_manifest,
        manifest_bytes=canonical_manifest_bytes(legacy_manifest),
        signing_keys=(
            TrustedSigningKey("signer:legacy", legacy_private.public_key().public_bytes_raw()),
        ),
    )
    root_store = _RootStore(b"r" * 32)
    certificate, device_private_bytes = _real_certificate(root_store)
    authority = _verified_authority(root_store, (certificate,), roles=("sponsor",))
    migration_now = certificate.claims.issued_at
    new_manifest = TeamStateManifestV2(
        project_id="project",
        repository_id="github.com/acme/project",
        graph_version=3,
        parent_bundle_digest=legacy_manifest.bundle_digest,
        bundle_digest=DIGEST_B,
        bundle_size=101,
        recipient_key_ids=authority.active_recipient_key_ids(),
        authority_digest=authority_digest(authority),
        authority_epoch=1,
        root_key_id=root_store.root.root_key_id,
        created_at=migration_now,
        migration=V1MigrationBinding(
            prior_manifest_digest="sha256:" + hashlib.sha256(current.manifest_bytes).hexdigest(),
            legacy_signature_ids=("signer:legacy",),
        ),
    )
    attestation = AuthorityAttestationV2(
        project_id="project",
        repository_id="github.com/acme/project",
        authority_epoch=1,
        previous_authority_digest=None,
        authority_digest=new_manifest.authority_digest,
        parent_bundle_digest=legacy_manifest.bundle_digest,
        operation="v1-migration",
        subject_digest="sha256:" + "5" * 64,
        sponsor_member_id=certificate.claims.member_id,
        sponsor_device_certificate_id=certificate.certificate_id,
        sponsor_decision_digest="sha256:" + "6" * 64,
        decided_at=migration_now,
        root_key_id=root_store.root.root_key_id,
        root_signature=_b64(b"0" * 64),
    )
    attestation = attestation.model_copy(
        update={
            "root_signature": _b64(
                root_store.private.sign(canonical_authority_attestation_preimage(attestation))
            )
        }
    )
    device_signature = Ed25519PrivateKey.from_private_bytes(device_private_bytes).sign(
        canonical_state_signature_preimage(new_manifest)
    )
    legacy_signature = legacy_private.sign(canonical_v1_migration_preimage(current, new_manifest))
    envelope = StateSignatureEnvelopeV2(
        manifest_digest="sha256:" + hashlib.sha256(new_manifest.canonical_bytes()).hexdigest(),
        bundle_digest=new_manifest.bundle_digest,
        authority_digest=new_manifest.authority_digest,
        certificates=(certificate,),
        signatures=(
            CertifiedStateSignatureV2(
                certificate_id=certificate.certificate_id,
                signature_id=certificate.claims.signature_id,
                signature=_b64(device_signature),
            ),
        ),
        authority_attestation=attestation,
        migration_proof=V1MigrationProof(
            prior_manifest_digest=new_manifest.migration.prior_manifest_digest,
            legacy_signatures=(
                StateSignature(signature_id="signer:legacy", signature=_b64(legacy_signature)),
            ),
        ),
    )

    verified = verify_v1_migration(
        current=current,
        manifest=new_manifest,
        envelope=envelope,
        root=root_store.root,
        authority=authority,
        expected_ci_recipient=authority.ci_recipient,
        now=migration_now,
    )

    assert verified.authority_digest == new_manifest.authority_digest
    wrong = envelope.model_copy(
        update={
            "migration_proof": envelope.migration_proof.model_copy(
                update={
                    "legacy_signatures": (
                        StateSignature(signature_id="signer:legacy", signature=_b64(b"x" * 64)),
                    )
                }
            )
        }
    )
    from intent_engineering.team_state import restore as restore_module

    v2_verifications = 0
    real_verify_v2 = restore_module.verify_v2_envelope

    def record_v2_verification(*args, **kwargs):
        nonlocal v2_verifications
        v2_verifications += 1
        return real_verify_v2(*args, **kwargs)

    monkeypatch.setattr(restore_module, "verify_v2_envelope", record_v2_verification)
    with pytest.raises(ValueError, match="version one migration verification failed"):
        verify_v1_migration(
            current=current,
            manifest=new_manifest,
            envelope=wrong,
            root=root_store.root,
            authority=authority,
            expected_ci_recipient=authority.ci_recipient,
            now=migration_now,
        )
    assert v2_verifications == 0

    extra_proof = envelope.migration_proof.model_copy(
        update={
            "legacy_signatures": (
                StateSignature(signature_id="signer:extra", signature=_b64(b"e" * 64)),
                *envelope.migration_proof.legacy_signatures,
            )
        }
    )
    cases = (
        {"envelope": envelope.model_copy(update={"migration_proof": extra_proof})},
        {
            "manifest": new_manifest.model_copy(
                update={"parent_bundle_digest": "sha256:" + "7" * 64}
            )
        },
        {
            "expected_ci_recipient": authority.ci_recipient.model_copy(
                update={"runner_id": "other-runner"}
            )
        },
        {"root": root_store.root.model_copy(update={"root_public_key": _b64(b"q" * 32)})},
    )
    baseline = {
        "current": current,
        "manifest": new_manifest,
        "envelope": envelope,
        "root": root_store.root,
        "authority": authority,
        "expected_ci_recipient": authority.ci_recipient,
        "now": migration_now,
    }
    for change in cases:
        with pytest.raises(ValueError, match="version one migration verification failed"):
            verify_v1_migration(**(baseline | change))

    class Cancellation(BaseException):
        pass

    cancellation = Cancellation("secret-sentinel")
    cancellation.secret_marker = "secret-sentinel"

    def cancel_legacy_preimage(_current: object, _manifest: object) -> bytes:
        secret_local = "secret-sentinel"
        assert secret_local
        raise cancellation

    monkeypatch.setattr(
        restore_module,
        "canonical_v1_migration_preimage",
        cancel_legacy_preimage,
    )
    with pytest.raises(Cancellation) as caught:
        verify_v1_migration(**baseline)
    assert caught.value is cancellation
    assert caught.value.args == ()
    assert caught.value.__dict__ == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    trace = caught.value.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_name == "cancel_legacy_preimage":
            assert "secret-sentinel" not in repr(dict(trace.tb_frame.f_locals))
        trace = trace.tb_next


def test_schema1_is_a_rollback_only_after_local_v2_acceptance() -> None:
    """Catches accepting a valid legacy release after stable-root trust has advanced."""
    from intent_engineering.team_state.restore import rejects_schema1_after_v2

    assert rejects_schema1_after_v2(accepted_schema_version=2, candidate_schema_version=1)
    assert not rejects_schema1_after_v2(accepted_schema_version=1, candidate_schema_version=1)
