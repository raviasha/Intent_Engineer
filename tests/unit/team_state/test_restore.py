"""Version-dispatched parsing for public team-state artifacts."""

from __future__ import annotations

import base64
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
