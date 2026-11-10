"""Canonical identifiers and bytes for version-two team authority."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from intent_engineering.team_state.models import (
    _PROJECT_ID,
    _REPOSITORY_ID,
    MAX_AUTHORITY_BYTES,
    AuthorityAttestationV2,
    DeviceCertificateClaimsV2,
    DeviceSignerCertificateV2,
    StateSignatureEnvelopeV2,
    TeamAuthorityRegistryV2,
    TeamRootTrustV2,
    TeamStateManifestV2,
    _canonical_json,
    _derived_member_id,
    _derived_recipient_key_id,
    _derived_root_key_id,
    _derived_signature_id,
)

if TYPE_CHECKING:
    from intent_engineering.team_state.signing import TeamRootKeyStore

_MAX_CLOCK_SKEW = timedelta(minutes=5)
_CERTIFICATE_DOMAIN = b"intent.team-device-certificate.root.v2\0"
_STATE_DOMAIN = "intent.team-state-signature.v2"
_AUTHORITY_DOMAIN = "intent.team-authority-attestation.v2"


def _require_scope(project_id: object, repository_id: object) -> tuple[str, str]:
    if type(project_id) is not str or type(repository_id) is not str:
        raise ValueError("invalid team authority scope")
    try:
        if (
            _PROJECT_ID.fullmatch(project_id) is None
            or _REPOSITORY_ID.fullmatch(repository_id) is None
        ):
            raise ValueError("invalid team authority scope")
        _host, owner, repository = repository_id.split("/")
        if owner in {".", ".."} or repository in {".", ".."} or repository.endswith(".git"):
            raise ValueError("invalid team authority scope")
    except (AttributeError, ValueError) as error:
        raise ValueError("invalid team authority scope") from error
    return project_id, repository_id


def derive_root_key_id(project_id: str, repository_id: str, public_key: str) -> str:
    project_id, repository_id = _require_scope(project_id, repository_id)
    if type(public_key) is not str:
        raise ValueError("invalid root public key")
    return _derived_root_key_id(project_id, repository_id, public_key)


def derive_member_id(project_id: str, repository_id: str, github_account_id: int) -> str:
    project_id, repository_id = _require_scope(project_id, repository_id)
    if type(github_account_id) is not int or github_account_id <= 0:
        raise ValueError("invalid GitHub account identity")
    return _derived_member_id(project_id, repository_id, github_account_id)


def derive_recipient_key_id(project_id: str, repository_id: str, public_key: str) -> str:
    project_id, repository_id = _require_scope(project_id, repository_id)
    if type(public_key) is not str:
        raise ValueError("invalid recipient public key")
    return _derived_recipient_key_id(project_id, repository_id, public_key)


def derive_signature_id(project_id: str, repository_id: str, public_key: str) -> str:
    project_id, repository_id = _require_scope(project_id, repository_id)
    if type(public_key) is not str:
        raise ValueError("invalid signing public key")
    return _derived_signature_id(project_id, repository_id, public_key)


def canonical_certificate_claims_bytes(claims: DeviceCertificateClaimsV2) -> bytes:
    if type(claims) is not DeviceCertificateClaimsV2:
        raise TypeError("claims must be DeviceCertificateClaimsV2")
    validated = DeviceCertificateClaimsV2.model_validate(claims.model_dump(mode="python"))
    content = _canonical_json(validated.model_dump(mode="json"))
    if not content or len(content) > 16 * 1024:
        raise ValueError("device certificate claims are oversized")
    return content


def derive_certificate_id(claims: DeviceCertificateClaimsV2) -> str:
    return (
        "certificate:sha256:"
        + hashlib.sha256(
            b"intent.team-device-certificate.v2\0" + canonical_certificate_claims_bytes(claims)
        ).hexdigest()
    )


def canonical_authority_bytes(authority: TeamAuthorityRegistryV2) -> bytes:
    if type(authority) is not TeamAuthorityRegistryV2:
        raise TypeError("authority must be TeamAuthorityRegistryV2")
    validated = TeamAuthorityRegistryV2.model_validate(authority.model_dump(mode="python"))
    content = _canonical_json(validated.model_dump(mode="json"))
    if not content or len(content) > MAX_AUTHORITY_BYTES:
        raise ValueError("team authority registry is oversized")
    return content


def authority_digest(authority: TeamAuthorityRegistryV2) -> str:
    return "sha256:" + hashlib.sha256(canonical_authority_bytes(authority)).hexdigest()


def _b64encode(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def _b64decode(value: str, size: int) -> bytes:
    if type(value) is not str or not value or "=" in value:
        raise ValueError("invalid authority signature")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (UnicodeError, ValueError) as error:
        raise ValueError("invalid authority signature") from error
    if len(decoded) != size or _b64encode(decoded) != value:
        raise ValueError("invalid authority signature")
    return decoded


def canonical_certificate_signing_preimage(claims: DeviceCertificateClaimsV2) -> bytes:
    """Return the domain-separated root preimage for one device certificate."""
    return _CERTIFICATE_DOMAIN + canonical_certificate_claims_bytes(claims)


def canonical_state_signature_preimage(manifest: TeamStateManifestV2) -> bytes:
    """Return the exact device-signature preimage for a v2 state manifest."""
    if type(manifest) is not TeamStateManifestV2:
        raise TypeError("manifest must be TeamStateManifestV2")
    value = TeamStateManifestV2.model_validate(manifest.model_dump(mode="python"))
    return _canonical_json(
        {
            "authority_digest": value.authority_digest,
            "authority_epoch": value.authority_epoch,
            "bundle_digest": value.bundle_digest,
            "domain": _STATE_DOMAIN,
            "manifest_digest": "sha256:" + hashlib.sha256(value.canonical_bytes()).hexdigest(),
            "parent_bundle_digest": value.parent_bundle_digest,
            "project_id": value.project_id,
            "repository_id": value.repository_id,
            "schema_version": 2,
        }
    )


def canonical_authority_attestation_preimage(attestation: AuthorityAttestationV2) -> bytes:
    """Return the root-signature preimage without its signature field."""
    if type(attestation) is not AuthorityAttestationV2:
        raise TypeError("attestation must be AuthorityAttestationV2")
    value = AuthorityAttestationV2.model_validate(attestation.model_dump(mode="python"))
    return _canonical_json(
        {
            "attestation": value.model_dump(mode="json", exclude={"root_signature"}),
            "domain": _AUTHORITY_DOMAIN,
        }
    )


def _prepare_authority_failure(error: BaseException, message: str) -> BaseException:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    error.args = ()
    if isinstance(error, Exception):
        return ValueError(message)
    return error


def issue_device_certificate(
    claims: DeviceCertificateClaimsV2,
    root_store: TeamRootKeyStore,
) -> DeviceSignerCertificateV2:
    """Certify exact public device claims with the locally held team root."""
    failure: BaseException | None = None
    value: DeviceCertificateClaimsV2 | None = None
    root: TeamRootTrustV2 | None = None
    signature = b""
    try:
        value = DeviceCertificateClaimsV2.model_validate(claims.model_dump(mode="python"))
        root = root_store.root_trust()
        if (
            root.project_id != value.project_id
            or root.repository_id != value.repository_id
            or root.authority_epoch != value.authority_epoch
        ):
            raise ValueError("root scope changed")
        signature = root_store.sign(root.root_key_id, canonical_certificate_signing_preimage(value))
        if type(signature) is not bytes or len(signature) != 64:
            raise ValueError("invalid root signature")
        return DeviceSignerCertificateV2(
            claims=value,
            certificate_id=derive_certificate_id(value),
            root_key_id=root.root_key_id,
            root_signature=_b64encode(signature),
        )
    except BaseException as error:  # noqa: BLE001 - fixed public crypto boundary
        failure = _prepare_authority_failure(error, "device certificate issuance failed")
    finally:
        signature = b""
    assert failure is not None
    del value, root, signature, claims, root_store
    raise failure.with_traceback(None)


@dataclass(frozen=True, slots=True)
class VerifiedEnvelopeV2:
    manifest_digest: str
    authority_digest: str
    signer_certificate_ids: tuple[str, ...]
    authority_changed: bool


def _verify_root_certificate(certificate: DeviceSignerCertificateV2, root: TeamRootTrustV2) -> None:
    if certificate.root_key_id != root.root_key_id:
        raise ValueError("certificate root changed")
    Ed25519PublicKey.from_public_bytes(_b64decode(root.root_public_key, 32)).verify(
        _b64decode(certificate.root_signature, 64),
        canonical_certificate_signing_preimage(certificate.claims),
    )


def _active_member_role(
    authority: TeamAuthorityRegistryV2,
    certificate: DeviceSignerCertificateV2,
    release_time: datetime,
) -> str:
    known = {item.certificate_id: item for item in authority.device_certificates}.get(
        certificate.certificate_id
    )
    if known != certificate:
        raise ValueError("unknown device certificate")
    member = {item.member_id: item for item in authority.members}.get(certificate.claims.member_id)
    if member is None or member.status != "active":
        raise ValueError("inactive team member")
    revoked = {
        item.certificate_id
        for item in authority.revocations
        if item.revoked_at <= release_time + _MAX_CLOCK_SKEW
    }
    if certificate.certificate_id in revoked:
        raise ValueError("revoked device certificate")
    return member.role


def _verify_v2_envelope(
    manifest: TeamStateManifestV2,
    envelope: StateSignatureEnvelopeV2,
    root: TeamRootTrustV2,
    parent_authority: TeamAuthorityRegistryV2 | None,
    now: datetime,
) -> VerifiedEnvelopeV2:
    manifest = TeamStateManifestV2.model_validate(manifest.model_dump(mode="python"))
    envelope = StateSignatureEnvelopeV2.model_validate(envelope.model_dump(mode="python"))
    root = TeamRootTrustV2.model_validate(root.model_dump(mode="python"))
    if (
        type(now) is not datetime
        or now.tzinfo is None
        or now.utcoffset() != timedelta(0)
        or now.microsecond != 0
    ):
        raise ValueError("invalid verification time")
    now = now.astimezone(UTC)
    manifest_digest = "sha256:" + hashlib.sha256(manifest.canonical_bytes()).hexdigest()
    if (
        manifest.project_id != root.project_id
        or manifest.repository_id != root.repository_id
        or manifest.authority_epoch != root.authority_epoch
        or manifest.root_key_id != root.root_key_id
        or manifest.created_at > now + _MAX_CLOCK_SKEW
        or envelope.manifest_digest != manifest_digest
        or envelope.bundle_digest != manifest.bundle_digest
        or envelope.authority_digest != manifest.authority_digest
    ):
        raise ValueError("manifest authority changed")

    parent_digest: str | None = None
    if parent_authority is not None:
        parent_authority = TeamAuthorityRegistryV2.model_validate(
            parent_authority.model_dump(mode="python")
        )
        parent_digest = authority_digest(parent_authority)
        if (
            parent_authority.project_id != manifest.project_id
            or parent_authority.repository_id != manifest.repository_id
            or parent_authority.authority_epoch != manifest.authority_epoch
            or parent_authority.root != root
        ):
            raise ValueError("parent authority scope changed")

    authority_changed = parent_digest != manifest.authority_digest
    attestation = envelope.authority_attestation
    if not authority_changed:
        if (
            parent_authority is None
            or attestation is not None
            or envelope.migration_proof is not None
            or manifest.migration is not None
            or manifest.recipient_key_ids != parent_authority.active_recipient_key_ids()
        ):
            raise ValueError("ordinary release authority changed")
    else:
        if attestation is None:
            raise ValueError("authority attestation missing")
        if (
            attestation.project_id != manifest.project_id
            or attestation.repository_id != manifest.repository_id
            or attestation.authority_epoch != manifest.authority_epoch
            or attestation.previous_authority_digest != parent_digest
            or attestation.authority_digest != manifest.authority_digest
            or attestation.parent_bundle_digest != manifest.parent_bundle_digest
            or attestation.root_key_id != root.root_key_id
            or abs(attestation.decided_at - manifest.created_at) > _MAX_CLOCK_SKEW
        ):
            raise ValueError("authority attestation binding changed")
        Ed25519PublicKey.from_public_bytes(_b64decode(root.root_public_key, 32)).verify(
            _b64decode(attestation.root_signature, 64),
            canonical_authority_attestation_preimage(attestation),
        )
        migration = parent_authority is None
        if migration != (attestation.operation == "v1-migration"):
            raise ValueError("invalid authority migration")

    preimage = canonical_state_signature_preimage(manifest)
    roles: dict[str, str] = {}
    for certificate, signature in zip(envelope.certificates, envelope.signatures, strict=True):
        _verify_root_certificate(certificate, root)
        if (
            certificate.claims.issued_at > manifest.created_at + _MAX_CLOCK_SKEW
            or certificate.claims.expires_at < manifest.created_at - _MAX_CLOCK_SKEW
        ):
            raise ValueError("device certificate expired")
        role = "sponsor"
        if parent_authority is not None:
            role = _active_member_role(parent_authority, certificate, manifest.created_at)
        roles[certificate.certificate_id] = role
        Ed25519PublicKey.from_public_bytes(
            _b64decode(certificate.claims.signing_public_key, 32)
        ).verify(_b64decode(signature.signature, 64), preimage)

    threshold = (
        1 if parent_authority is None else parent_authority.policy.ordinary_signature_threshold
    )
    if len(roles) < threshold:
        raise ValueError("insufficient device signatures")
    if authority_changed:
        assert attestation is not None
        if roles.get(attestation.sponsor_device_certificate_id) != "sponsor":
            raise ValueError("authority change requires an active sponsor")
    return VerifiedEnvelopeV2(
        manifest_digest=manifest_digest,
        authority_digest=manifest.authority_digest,
        signer_certificate_ids=tuple(item.certificate_id for item in envelope.certificates),
        authority_changed=authority_changed,
    )


def verify_v2_envelope(
    manifest: TeamStateManifestV2,
    envelope: StateSignatureEnvelopeV2,
    root: TeamRootTrustV2,
    parent_authority: TeamAuthorityRegistryV2 | None,
    now: datetime,
) -> VerifiedEnvelopeV2:
    """Verify a v2 release against stable root trust and its exact parent registry."""
    try:
        return _verify_v2_envelope(manifest, envelope, root, parent_authority, now)
    except BaseException as error:  # noqa: BLE001 - fixed public verification boundary
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        if not isinstance(error, Exception):
            raise error.with_traceback(None)
        raise ValueError("version two envelope verification failed") from None


__all__ = [
    "VerifiedEnvelopeV2",
    "authority_digest",
    "canonical_authority_attestation_preimage",
    "canonical_authority_bytes",
    "canonical_certificate_claims_bytes",
    "canonical_certificate_signing_preimage",
    "canonical_state_signature_preimage",
    "derive_certificate_id",
    "derive_member_id",
    "derive_recipient_key_id",
    "derive_root_key_id",
    "derive_signature_id",
    "issue_device_certificate",
    "verify_v2_envelope",
]
