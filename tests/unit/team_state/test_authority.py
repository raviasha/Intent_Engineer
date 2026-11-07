"""Canonical version-two team authority contracts."""

from __future__ import annotations

import base64
import hashlib
import traceback
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from intent_engineering.team_state.authority import (
    authority_digest,
    canonical_authority_attestation_preimage,
    canonical_authority_bytes,
    canonical_state_signature_preimage,
    derive_certificate_id,
    derive_member_id,
    derive_recipient_key_id,
    derive_root_key_id,
    derive_signature_id,
    issue_device_certificate,
    verify_v2_envelope,
)
from intent_engineering.team_state.models import (
    AuthorityAttestationV2,
    CertifiedStateSignatureV2,
    CiRecipientRecord,
    DeviceCertificateClaimsV2,
    DeviceRevocationV2,
    DeviceSignerCertificateV2,
    MemberRecordV2,
    StateSignatureEnvelopeV2,
    TeamAuthorityPolicyV2,
    TeamAuthorityRegistryV2,
    TeamRootTrustV2,
    TeamStateManifestV2,
)

PROJECT = "project"
REPOSITORY = "github.com/acme/project"
NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)


def _authority_traceback_locals(error: BaseException) -> list[dict[str, object]]:
    return [
        dict(frame.f_locals)
        for frame, _lineno in traceback.walk_tb(error.__traceback__)
        if frame.f_globals.get("__name__") == "intent_engineering.team_state.authority"
    ]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


ROOT_PUBLIC = _b64(b"r" * 32)
RECIPIENT_PUBLIC = _b64(b"x" * 32)
SIGNING_PUBLIC = _b64(b"s" * 32)
ROOT_SIGNATURE = _b64(b"z" * 64)
MEMBER_ID = derive_member_id(PROJECT, REPOSITORY, 1234)
ROOT_ID = derive_root_key_id(PROJECT, REPOSITORY, ROOT_PUBLIC)
RECIPIENT_ID = derive_recipient_key_id(PROJECT, REPOSITORY, RECIPIENT_PUBLIC)
SIGNATURE_ID = derive_signature_id(PROJECT, REPOSITORY, SIGNING_PUBLIC)


def claims(**changes: object) -> DeviceCertificateClaimsV2:
    values: dict[str, object] = {
        "project_id": PROJECT,
        "repository_id": REPOSITORY,
        "authority_epoch": 1,
        "member_id": MEMBER_ID,
        "device_id": "device:" + "1" * 32,
        "github_account_id": 1234,
        "github_login": "alice-dev",
        "recipient_key_id": RECIPIENT_ID,
        "recipient_public_key": RECIPIENT_PUBLIC,
        "signature_id": SIGNATURE_ID,
        "signing_public_key": SIGNING_PUBLIC,
        "webauthn_credential_digest": "sha256:" + "c" * 64,
        "serial": 1,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(days=366),
    }
    values.update(changes)
    return DeviceCertificateClaimsV2(**values)


def certificate(**changes: object) -> DeviceSignerCertificateV2:
    claim = changes.pop("claims", claims())
    assert isinstance(claim, DeviceCertificateClaimsV2)
    values: dict[str, object] = {
        "claims": claim,
        "certificate_id": derive_certificate_id(claim),
        "algorithm": "ed25519-root-certificate-v1",
        "root_key_id": ROOT_ID,
        "root_signature": ROOT_SIGNATURE,
    }
    values.update(changes)
    return DeviceSignerCertificateV2(**values)


def second_certificate(*, serial: int = 2) -> DeviceSignerCertificateV2:
    recipient_public = _b64(b"y" * 32)
    signing_public = _b64(b"t" * 32)
    return certificate(
        claims=claims(
            member_id=derive_member_id(PROJECT, REPOSITORY, 5678),
            device_id="device:" + "2" * 32,
            github_account_id=5678,
            github_login="bob-dev",
            recipient_key_id=derive_recipient_key_id(PROJECT, REPOSITORY, recipient_public),
            recipient_public_key=recipient_public,
            signature_id=derive_signature_id(PROJECT, REPOSITORY, signing_public),
            signing_public_key=signing_public,
            serial=serial,
        )
    )


def scoped_certificate(
    *,
    project_id: str = PROJECT,
    repository_id: str = REPOSITORY,
    authority_epoch: int = 1,
    root_public_key: str = ROOT_PUBLIC,
) -> DeviceSignerCertificateV2:
    recipient_public = _b64(b"y" * 32)
    signing_public = _b64(b"t" * 32)
    scoped_claims = DeviceCertificateClaimsV2(
        project_id=project_id,
        repository_id=repository_id,
        authority_epoch=authority_epoch,
        member_id=derive_member_id(project_id, repository_id, 5678),
        device_id="device:" + "2" * 32,
        github_account_id=5678,
        github_login="bob-dev",
        recipient_key_id=derive_recipient_key_id(project_id, repository_id, recipient_public),
        recipient_public_key=recipient_public,
        signature_id=derive_signature_id(project_id, repository_id, signing_public),
        signing_public_key=signing_public,
        webauthn_credential_digest="sha256:" + "d" * 64,
        serial=2,
        issued_at=NOW,
        expires_at=NOW + timedelta(days=366),
    )
    return DeviceSignerCertificateV2(
        claims=scoped_claims,
        certificate_id=derive_certificate_id(scoped_claims),
        root_key_id=derive_root_key_id(project_id, repository_id, root_public_key),
        root_signature=ROOT_SIGNATURE,
    )


def envelope_for(
    certificates: tuple[DeviceSignerCertificateV2, ...],
    *,
    attestation: AuthorityAttestationV2 | None = None,
) -> StateSignatureEnvelopeV2:
    certificates = tuple(sorted(certificates, key=lambda item: item.certificate_id))
    return StateSignatureEnvelopeV2(
        manifest_digest="sha256:" + "a" * 64,
        bundle_digest="sha256:" + "b" * 64,
        authority_digest=(
            attestation.authority_digest if attestation is not None else "sha256:" + "c" * 64
        ),
        certificates=certificates,
        signatures=tuple(
            CertifiedStateSignatureV2(
                certificate_id=item.certificate_id,
                signature_id=item.claims.signature_id,
                signature=_b64(b"q" * 64),
            )
            for item in certificates
        ),
        authority_attestation=attestation,
    )


def member_for(
    cert: DeviceSignerCertificateV2,
    *,
    role: Literal["sponsor", "member"],
    status: Literal["active", "revoked"] = "active",
) -> MemberRecordV2:
    claim = cert.claims
    return MemberRecordV2(
        member_id=claim.member_id,
        actor=f"github:{claim.github_account_id}",
        github_account_id=claim.github_account_id,
        github_login=claim.github_login,
        role=role,
        status=status,
        device_certificate_ids=(cert.certificate_id,),
        enrolled_at=NOW,
        revoked_at=NOW if status == "revoked" else None,
    )


def authority(**changes: object) -> TeamAuthorityRegistryV2:
    cert = changes.pop("certificate", certificate())
    assert isinstance(cert, DeviceSignerCertificateV2)
    member = MemberRecordV2(
        member_id=MEMBER_ID,
        actor="github:1234",
        github_account_id=1234,
        github_login="alice-dev",
        role="sponsor",
        status="active",
        device_certificate_ids=(cert.certificate_id,),
        enrolled_at=NOW,
    )
    root = TeamRootTrustV2(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        authority_epoch=1,
        root_key_id=ROOT_ID,
        root_public_key=ROOT_PUBLIC,
        created_at=NOW,
    )
    ci = CiRecipientRecord(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        runner_id="intent-state",
        public_key=_b64(b"c" * 32),
    )
    values: dict[str, object] = {
        "project_id": PROJECT,
        "repository_id": REPOSITORY,
        "authority_epoch": 1,
        "sequence": 1,
        "root": root,
        "policy": TeamAuthorityPolicyV2(),
        "members": (member,),
        "device_certificates": (cert,),
        "revocations": (),
        "ci_recipient": ci,
        "previous_authority_digest": None,
    }
    values.update(changes)
    return TeamAuthorityRegistryV2(**values)


def test_derived_ids_are_stable_and_domain_separated() -> None:
    """Catches identities being derived from mutable login text or reused across protocols."""
    expected = hashlib.sha256(
        b"intent.team-member.v2" + b"\0project\0github.com/acme/project\0" + b"1234"
    ).hexdigest()
    assert MEMBER_ID == f"member:sha256:{expected}"
    assert len({ROOT_ID, MEMBER_ID, RECIPIENT_ID, SIGNATURE_ID}) == 4
    assert derive_member_id(PROJECT, REPOSITORY, 1234) != derive_member_id(
        PROJECT, "github.com/acme/other", 1234
    )


def test_authority_has_one_canonical_bounded_round_trip() -> None:
    value = authority()
    encoded = canonical_authority_bytes(value)

    assert len(encoded) < 256 * 1024
    assert TeamAuthorityRegistryV2.model_validate_json(encoded) == value
    assert authority_digest(value) == "sha256:" + hashlib.sha256(encoded).hexdigest()
    with pytest.raises(ValueError, match="noncanonical"):
        TeamAuthorityRegistryV2.model_validate_json(encoded.replace(b"{", b"{ ", 1))
    duplicate = encoded.replace(
        b'{"authority_epoch":1', b'{"authority_epoch":1,"authority_epoch":1'
    )
    with pytest.raises(ValueError):
        TeamAuthorityRegistryV2.model_validate_json(duplicate)


def test_authority_derives_the_exact_active_human_plus_ci_recipient_set() -> None:
    value = authority()

    assert value.active_recipient_key_ids() == tuple(
        sorted((RECIPIENT_ID, value.ci_recipient.key_id))
    )


@pytest.mark.parametrize("bad", [[MEMBER_ID], ()])
def test_member_certificate_ids_require_nonempty_strict_tuples(bad: object) -> None:
    with pytest.raises(ValidationError):
        MemberRecordV2(
            member_id=MEMBER_ID,
            actor="github:1234",
            github_account_id=1234,
            github_login="alice-dev",
            role="sponsor",
            status="active",
            device_certificate_ids=bad,
            enrolled_at=NOW,
        )


@pytest.mark.parametrize(
    ("factory", "changes"),
    [
        (claims, {"recipient_public_key": _b64(b"short")}),
        (claims, {"recipient_key_id": "recipient:sha256:" + "0" * 64}),
        (claims, {"signature_id": "signer:sha256:" + "0" * 64}),
        (claims, {"expires_at": NOW + timedelta(days=366, seconds=1)}),
        (claims, {"issued_at": NOW.replace(microsecond=1)}),
        (claims, {"issued_at": NOW.astimezone(timezone(timedelta(hours=1)))}),
        (certificate, {"certificate_id": "certificate:sha256:" + "0" * 64}),
        (certificate, {"algorithm": "ed25519-v1"}),
        (certificate, {"root_signature": _b64(b"short")}),
    ],
)
def test_key_certificate_and_time_bindings_are_exact(
    factory: object, changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        factory(**changes)  # type: ignore[operator]


def test_authority_graph_rejects_cross_scope_unsorted_duplicate_and_inactive_devices() -> None:
    valid = authority()
    cert = valid.device_certificates[0]
    member = valid.members[0]
    bad_scope = cert.model_copy(
        update={"claims": cert.claims.model_copy(update={"repository_id": "github.com/acme/other"})}
    )
    cases = [
        {"certificate": bad_scope},
        {"members": (member, member)},
        {"device_certificates": (cert, cert)},
        {"members": (member.model_copy(update={"status": "revoked", "revoked_at": NOW}),)},
    ]
    for changes in cases:
        with pytest.raises(ValidationError):
            authority(**changes)


def test_authority_graph_rejects_unsorted_distinct_members_certificates_and_serials() -> None:
    first = certificate()
    second = second_certificate()
    members = (member_for(first, role="sponsor"), member_for(second, role="member"))
    sorted_members = tuple(sorted(members, key=lambda item: item.member_id))
    sorted_certificates = tuple(sorted((first, second), key=lambda item: item.certificate_id))

    assert authority(members=sorted_members, device_certificates=sorted_certificates)
    with pytest.raises(ValidationError):
        authority(members=tuple(reversed(sorted_members)), device_certificates=sorted_certificates)
    with pytest.raises(ValidationError):
        authority(members=sorted_members, device_certificates=tuple(reversed(sorted_certificates)))
    duplicate_serial = second_certificate(serial=1)
    with pytest.raises(ValidationError):
        authority(
            members=sorted_members,
            device_certificates=tuple(
                sorted((first, duplicate_serial), key=lambda item: item.certificate_id)
            ),
        )


def test_authority_graph_accepts_an_exact_revoked_member_graph() -> None:
    valid = authority()
    sponsor_cert = valid.device_certificates[0]
    revoked_cert = second_certificate()
    sponsor = member_for(sponsor_cert, role="sponsor")
    member = member_for(revoked_cert, role="member", status="revoked")
    revocation = DeviceRevocationV2(
        certificate_id=revoked_cert.certificate_id,
        revoked_at=NOW,
        reason="member-removed",
        sponsor_member_id=MEMBER_ID,
    )

    revoked = authority(
        members=tuple(sorted((sponsor, member), key=lambda item: item.member_id)),
        device_certificates=tuple(
            sorted((sponsor_cert, revoked_cert), key=lambda item: item.certificate_id)
        ),
        revocations=(revocation,),
    )

    assert any(item.status == "revoked" for item in revoked.members)
    assert revoked_cert.claims.recipient_key_id not in revoked.active_recipient_key_ids()
    with pytest.raises(ValidationError):
        authority(
            members=(member_for(sponsor_cert, role="sponsor", status="revoked"),),
            revocations=(
                DeviceRevocationV2(
                    certificate_id=sponsor_cert.certificate_id,
                    revoked_at=NOW,
                    reason="member-removed",
                    sponsor_member_id=MEMBER_ID,
                ),
            ),
        )


def test_authority_enforces_member_device_and_revocation_caps() -> None:
    valid = authority()
    with pytest.raises(ValidationError):
        authority(members=valid.members * 33)
    with pytest.raises(ValidationError):
        authority(device_certificates=valid.device_certificates * 64)
    with pytest.raises(ValidationError):
        authority(
            revocations=tuple(
                DeviceRevocationV2(
                    certificate_id="certificate:sha256:" + f"{index:064x}",
                    revoked_at=NOW,
                    reason="lost",
                    sponsor_member_id=MEMBER_ID,
                )
                for index in range(257)
            )
        )


def test_canonical_authority_bytes_revalidates_and_enforces_256_kib() -> None:
    forged = authority().model_copy(update={"members": authority().members * 33})
    with pytest.raises(ValidationError):
        canonical_authority_bytes(forged)
    with pytest.raises(ValueError, match="invalid team authority registry"):
        TeamAuthorityRegistryV2.model_validate_json(b"{" + b" " * (256 * 1024))


def test_v2_envelope_binds_each_sorted_signature_to_its_certificate() -> None:
    cert = certificate()
    value = StateSignatureEnvelopeV2(
        manifest_digest="sha256:" + "a" * 64,
        bundle_digest="sha256:" + "b" * 64,
        authority_digest="sha256:" + "c" * 64,
        certificates=(cert,),
        signatures=(
            CertifiedStateSignatureV2(
                certificate_id=cert.certificate_id,
                signature_id=cert.claims.signature_id,
                signature=_b64(b"q" * 64),
            ),
        ),
    )

    encoded = value.canonical_bytes()

    assert StateSignatureEnvelopeV2.model_validate_json(encoded) == value
    with pytest.raises(ValidationError):
        StateSignatureEnvelopeV2(
            **{
                **value.model_dump(),
                "signatures": (
                    value.signatures[0].model_copy(
                        update={"signature_id": "signer:sha256:" + "0" * 64}
                    ),
                ),
            }
        )


def test_v2_envelope_rejects_duplicate_device_signing_ids() -> None:
    first = certificate()
    duplicate_signer_claims = second_certificate().claims.model_copy(
        update={"signature_id": SIGNATURE_ID, "signing_public_key": SIGNING_PUBLIC}
    )
    second = certificate(claims=duplicate_signer_claims)
    certificates = tuple(sorted((first, second), key=lambda item: item.certificate_id))
    signatures = tuple(
        CertifiedStateSignatureV2(
            certificate_id=item.certificate_id,
            signature_id=item.claims.signature_id,
            signature=_b64(b"q" * 64),
        )
        for item in certificates
    )

    with pytest.raises(ValidationError):
        StateSignatureEnvelopeV2(
            manifest_digest="sha256:" + "a" * 64,
            bundle_digest="sha256:" + "b" * 64,
            authority_digest="sha256:" + "c" * 64,
            certificates=certificates,
            signatures=signatures,
        )


@pytest.mark.parametrize(
    "other",
    [
        scoped_certificate(repository_id="github.com/acme/other"),
        scoped_certificate(root_public_key=_b64(b"o" * 32)),
        scoped_certificate(authority_epoch=2),
    ],
    ids=("cross-repository", "mixed-root", "mixed-epoch"),
)
def test_v2_envelope_rejects_mixed_certificate_authority_scope(
    other: DeviceSignerCertificateV2,
) -> None:
    """Catches a release combining individually valid certificates from two authorities."""
    with pytest.raises(ValidationError):
        envelope_for((certificate(), other))


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": "other-project"},
        {"repository_id": "github.com/acme/other"},
        {"authority_epoch": 2},
        {"root_key_id": derive_root_key_id(PROJECT, REPOSITORY, _b64(b"o" * 32))},
        {"sponsor_device_certificate_id": "certificate:sha256:" + "0" * 64},
        {"sponsor_member_id": derive_member_id(PROJECT, REPOSITORY, 5678)},
    ],
    ids=("project", "repository", "epoch", "root", "unrelated-device", "unrelated-member"),
)
def test_v2_envelope_rejects_attestation_outside_certificate_scope(
    changes: dict[str, object],
) -> None:
    cert = certificate()
    attestation = AuthorityAttestationV2(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        authority_epoch=1,
        previous_authority_digest="sha256:" + "a" * 64,
        authority_digest="sha256:" + "b" * 64,
        parent_bundle_digest="sha256:" + "c" * 64,
        operation="enroll",
        subject_digest="sha256:" + "d" * 64,
        sponsor_member_id=MEMBER_ID,
        sponsor_device_certificate_id=cert.certificate_id,
        sponsor_decision_digest="sha256:" + "e" * 64,
        decided_at=NOW,
        root_key_id=ROOT_ID,
        root_signature=ROOT_SIGNATURE,
    ).model_copy(update=changes)

    with pytest.raises(ValidationError):
        envelope_for((cert,), attestation=attestation)


def test_authority_attestation_requires_exact_transition_and_envelope_digest() -> None:
    cert = certificate()
    attestation = AuthorityAttestationV2(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        authority_epoch=1,
        previous_authority_digest="sha256:" + "a" * 64,
        authority_digest="sha256:" + "b" * 64,
        parent_bundle_digest="sha256:" + "c" * 64,
        operation="enroll",
        subject_digest="sha256:" + "d" * 64,
        sponsor_member_id=MEMBER_ID,
        sponsor_device_certificate_id=cert.certificate_id,
        sponsor_decision_digest="sha256:" + "e" * 64,
        decided_at=NOW,
        root_key_id=ROOT_ID,
        root_signature=ROOT_SIGNATURE,
    )
    signature = CertifiedStateSignatureV2(
        certificate_id=cert.certificate_id,
        signature_id=cert.claims.signature_id,
        signature=_b64(b"q" * 64),
    )
    valid = StateSignatureEnvelopeV2(
        manifest_digest="sha256:" + "f" * 64,
        bundle_digest="sha256:" + "1" * 64,
        authority_digest=attestation.authority_digest,
        certificates=(cert,),
        signatures=(signature,),
        authority_attestation=attestation,
    )

    assert StateSignatureEnvelopeV2.model_validate_json(valid.canonical_bytes()) == valid

    with pytest.raises(ValidationError):
        StateSignatureEnvelopeV2(
            manifest_digest="sha256:" + "f" * 64,
            bundle_digest="sha256:" + "1" * 64,
            authority_digest="sha256:" + "0" * 64,
            certificates=(cert,),
            signatures=(signature,),
            authority_attestation=attestation,
        )
    with pytest.raises(ValidationError):
        AuthorityAttestationV2(
            **{
                **attestation.model_dump(),
                "previous_authority_digest": None,
            }
        )


class _RootStore:
    def __init__(self, root_private: bytes) -> None:
        self.private = Ed25519PrivateKey.from_private_bytes(root_private)
        public = _b64(self.private.public_key().public_bytes_raw())
        self.root = TeamRootTrustV2(
            project_id=PROJECT,
            repository_id=REPOSITORY,
            authority_epoch=1,
            root_key_id=derive_root_key_id(PROJECT, REPOSITORY, public),
            root_public_key=public,
            created_at=NOW,
        )

    def create(self, _binding: object) -> TeamRootTrustV2:
        return self.root

    def sign(self, root_key_id: str, preimage: bytes) -> bytes:
        assert root_key_id == self.root.root_key_id
        return self.private.sign(preimage)

    def root_trust(self) -> TeamRootTrustV2:
        return self.root


def _real_certificate(
    root_store: _RootStore,
    *,
    signing_private: bytes = b"s" * 32,
    account_id: int = 1234,
    login: str = "alice-dev",
    device_digit: str = "1",
    serial: int = 1,
    expires_at: datetime = NOW + timedelta(days=366),
) -> tuple[DeviceSignerCertificateV2, bytes]:
    signer = Ed25519PrivateKey.from_private_bytes(signing_private)
    recipient_public = _b64(bytes([serial % 251 + 1]) * 32)
    signing_public = _b64(signer.public_key().public_bytes_raw())
    claim = DeviceCertificateClaimsV2(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        authority_epoch=1,
        member_id=derive_member_id(PROJECT, REPOSITORY, account_id),
        device_id="device:" + device_digit * 32,
        github_account_id=account_id,
        github_login=login,
        recipient_key_id=derive_recipient_key_id(PROJECT, REPOSITORY, recipient_public),
        recipient_public_key=recipient_public,
        signature_id=derive_signature_id(PROJECT, REPOSITORY, signing_public),
        signing_public_key=signing_public,
        webauthn_credential_digest="sha256:" + device_digit * 64,
        serial=serial,
        issued_at=NOW,
        expires_at=expires_at,
    )
    return issue_device_certificate(claim, root_store), signing_private


def _verified_authority(
    root_store: _RootStore,
    certificates: tuple[DeviceSignerCertificateV2, ...],
    *,
    roles: tuple[Literal["sponsor", "member"], ...],
) -> TeamAuthorityRegistryV2:
    members = tuple(
        sorted(
            (member_for(cert, role=role) for cert, role in zip(certificates, roles, strict=True)),
            key=lambda item: item.member_id,
        )
    )
    return authority(
        certificate=certificates[0],
        root=root_store.root,
        members=members,
        device_certificates=tuple(sorted(certificates, key=lambda item: item.certificate_id)),
    )


def _manifest_for(
    parent: TeamAuthorityRegistryV2, *, digest: str | None = None
) -> TeamStateManifestV2:
    return TeamStateManifestV2(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        graph_version=3,
        parent_bundle_digest="sha256:" + "9" * 64,
        bundle_digest="sha256:" + "8" * 64,
        bundle_size=100,
        recipient_key_ids=parent.active_recipient_key_ids(),
        authority_digest=digest or authority_digest(parent),
        authority_epoch=1,
        root_key_id=parent.root.root_key_id,
        created_at=NOW,
    )


def _signed_envelope(
    manifest: TeamStateManifestV2,
    certificate: DeviceSignerCertificateV2,
    private: bytes,
    *,
    attestation: AuthorityAttestationV2 | None = None,
) -> StateSignatureEnvelopeV2:
    signature = Ed25519PrivateKey.from_private_bytes(private).sign(
        canonical_state_signature_preimage(manifest)
    )
    return StateSignatureEnvelopeV2(
        manifest_digest="sha256:" + hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
        bundle_digest=manifest.bundle_digest,
        authority_digest=manifest.authority_digest,
        certificates=(certificate,),
        signatures=(
            CertifiedStateSignatureV2(
                certificate_id=certificate.certificate_id,
                signature_id=certificate.claims.signature_id,
                signature=_b64(signature),
            ),
        ),
        authority_attestation=attestation,
    )


def test_root_issued_certificate_and_ordinary_state_signature_verify() -> None:
    """Catches accepting a device certificate or state signature under the wrong domain."""
    root_store = _RootStore(b"r" * 32)
    cert, private = _real_certificate(root_store)
    parent = _verified_authority(root_store, (cert,), roles=("sponsor",))
    manifest = _manifest_for(parent)
    envelope = _signed_envelope(manifest, cert, private)

    verified = verify_v2_envelope(manifest, envelope, root_store.root, parent, NOW)

    assert verified.signer_certificate_ids == (cert.certificate_id,)
    assert verified.authority_changed is False
    forged = envelope.model_copy(
        update={
            "signatures": (
                envelope.signatures[0].model_copy(update={"signature": cert.root_signature}),
            )
        }
    )
    with pytest.raises(ValueError, match="version two envelope verification failed"):
        verify_v2_envelope(manifest, forged, root_store.root, parent, NOW)


@pytest.mark.parametrize("case", ("expired", "revoked", "unknown", "wrong-parent", "empty"))
def test_v2_verifier_rejects_ineligible_or_insufficient_device_signatures(case: str) -> None:
    """Catches a non-active signer satisfying an ordinary publication threshold."""
    root_store = _RootStore(b"r" * 32)
    cert, private = _real_certificate(root_store)
    parent = _verified_authority(root_store, (cert,), roles=("sponsor",))
    manifest = _manifest_for(parent)
    envelope = _signed_envelope(manifest, cert, private)
    verify_at = NOW
    if case == "expired":
        cert, private = _real_certificate(root_store, expires_at=NOW + timedelta(days=1))
        parent = _verified_authority(root_store, (cert,), roles=("sponsor",))
        manifest = _manifest_for(parent).model_copy(update={"created_at": NOW + timedelta(days=2)})
        envelope = _signed_envelope(manifest, cert, private)
        verify_at = NOW + timedelta(days=2)
    elif case == "revoked":
        active, _ = _real_certificate(
            root_store,
            signing_private=b"a" * 32,
            device_digit="2",
            serial=2,
        )
        member = MemberRecordV2(
            member_id=cert.claims.member_id,
            actor="github:1234",
            github_account_id=1234,
            github_login="alice-dev",
            role="sponsor",
            status="active",
            device_certificate_ids=tuple(sorted((cert.certificate_id, active.certificate_id))),
            enrolled_at=NOW,
        )
        revocation = DeviceRevocationV2(
            certificate_id=cert.certificate_id,
            revoked_at=NOW,
            reason="lost",
            sponsor_member_id=member.member_id,
        )
        parent = authority(
            root=root_store.root,
            members=(member,),
            device_certificates=tuple(sorted((cert, active), key=lambda item: item.certificate_id)),
            revocations=(revocation,),
        )
    elif case == "unknown":
        unknown, unknown_private = _real_certificate(
            root_store,
            signing_private=b"u" * 32,
            account_id=2345,
            login="unknown-dev",
            device_digit="2",
            serial=2,
        )
        envelope = _signed_envelope(manifest, unknown, unknown_private)
    elif case == "wrong-parent":
        parent = parent.model_copy(
            update={"ci_recipient": parent.ci_recipient.model_copy(update={"runner_id": "other"})}
        )
    else:
        envelope = envelope.model_copy(update={"certificates": (), "signatures": ()})

    with pytest.raises(ValueError, match="version two envelope verification failed"):
        verify_v2_envelope(manifest, envelope, root_store.root, parent, verify_at)


def test_ordinary_member_cannot_authorize_an_authority_change() -> None:
    """Catches an active non-sponsor mutating membership with a valid device key."""
    root_store = _RootStore(b"r" * 32)
    sponsor, _ = _real_certificate(root_store)
    member, member_private = _real_certificate(
        root_store,
        signing_private=b"m" * 32,
        account_id=2345,
        login="bob-dev",
        device_digit="2",
        serial=2,
    )
    parent = _verified_authority(root_store, (sponsor, member), roles=("sponsor", "member"))
    next_digest = "sha256:" + "7" * 64
    manifest = _manifest_for(parent, digest=next_digest)
    unsigned = AuthorityAttestationV2(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        authority_epoch=1,
        previous_authority_digest=authority_digest(parent),
        authority_digest=next_digest,
        parent_bundle_digest=manifest.parent_bundle_digest,
        operation="enroll",
        subject_digest="sha256:" + "6" * 64,
        sponsor_member_id=member.claims.member_id,
        sponsor_device_certificate_id=member.certificate_id,
        sponsor_decision_digest="sha256:" + "5" * 64,
        decided_at=NOW,
        root_key_id=root_store.root.root_key_id,
        root_signature=_b64(b"0" * 64),
    )
    attestation = unsigned.model_copy(
        update={
            "root_signature": _b64(
                root_store.sign(
                    root_store.root.root_key_id,
                    canonical_authority_attestation_preimage(unsigned),
                )
            )
        }
    )
    envelope = _signed_envelope(manifest, member, member_private, attestation=attestation)

    with pytest.raises(ValueError, match="version two envelope verification failed"):
        verify_v2_envelope(manifest, envelope, root_store.root, parent, NOW)


def test_sponsor_authority_attestation_is_root_verified_under_its_own_domain() -> None:
    """Catches accepting a state-domain signature as a root authority attestation."""
    root_store = _RootStore(b"r" * 32)
    sponsor, private = _real_certificate(root_store)
    parent = _verified_authority(root_store, (sponsor,), roles=("sponsor",))
    next_digest = "sha256:" + "7" * 64
    manifest = _manifest_for(parent, digest=next_digest)
    unsigned = AuthorityAttestationV2(
        project_id=PROJECT,
        repository_id=REPOSITORY,
        authority_epoch=1,
        previous_authority_digest=authority_digest(parent),
        authority_digest=next_digest,
        parent_bundle_digest=manifest.parent_bundle_digest,
        operation="enroll",
        subject_digest="sha256:" + "6" * 64,
        sponsor_member_id=sponsor.claims.member_id,
        sponsor_device_certificate_id=sponsor.certificate_id,
        sponsor_decision_digest="sha256:" + "5" * 64,
        decided_at=NOW,
        root_key_id=root_store.root.root_key_id,
        root_signature=_b64(b"0" * 64),
    )
    attestation = unsigned.model_copy(
        update={
            "root_signature": _b64(
                root_store.sign(
                    root_store.root.root_key_id,
                    canonical_authority_attestation_preimage(unsigned),
                )
            )
        }
    )
    envelope = _signed_envelope(manifest, sponsor, private, attestation=attestation)

    assert verify_v2_envelope(manifest, envelope, root_store.root, parent, NOW).authority_changed

    wrong_domain = attestation.model_copy(
        update={
            "root_signature": _b64(
                root_store.sign(
                    root_store.root.root_key_id,
                    canonical_state_signature_preimage(manifest),
                )
            )
        }
    )
    forged = _signed_envelope(manifest, sponsor, private, attestation=wrong_domain)
    with pytest.raises(ValueError, match="version two envelope verification failed"):
        verify_v2_envelope(manifest, forged, root_store.root, parent, NOW)


def test_certificate_issuance_cancellation_scrubs_root_store_args_and_locals() -> None:
    """Catches root-store secrets surviving certificate-issuance cancellation."""

    class Cancelled(BaseException):
        pass

    cancellation = Cancelled("root-store-private-token")

    class CancellingRootStore(_RootStore):
        def sign(self, root_key_id: str, preimage: bytes) -> bytes:
            del root_key_id, preimage
            raise cancellation

    root_store = CancellingRootStore(b"r" * 32)
    public = _b64(Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key().public_bytes_raw())
    value = claims(
        signing_public_key=public,
        signature_id=derive_signature_id(PROJECT, REPOSITORY, public),
    )

    with pytest.raises(Cancelled) as caught:
        issue_device_certificate(value, root_store)

    assert caught.value is cancellation
    assert caught.value.args == ()
    locals_by_frame = _authority_traceback_locals(caught.value)
    assert all(root_store not in values.values() for values in locals_by_frame)
    assert all("root_store" not in values and "error" not in values for values in locals_by_frame)
