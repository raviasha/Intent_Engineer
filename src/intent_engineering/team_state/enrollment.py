"""Bounded public exchange for sponsor-approved version-two team enrollment."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import traceback
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Never, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ConfigDict, Field, field_validator, model_validator

from intent_engineering.control_plane.models import (
    CredentialRecord,
    DecisionAction,
    DecisionSubject,
    HumanDecisionPayload,
)
from intent_engineering.control_plane.webauthn_service import (
    AuthenticationRequest,
    VerifiedAuthentication,
    VerifiedHumanDecision,
    WebAuthnVerifier,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.team_state.archive import build_archive_v2
from intent_engineering.team_state.authority import (
    authority_digest,
    canonical_authority_attestation_preimage,
    canonical_certificate_signing_preimage,
    canonical_state_signature_preimage,
    derive_certificate_id,
    derive_member_id,
    issue_device_certificate,
    verify_v2_envelope,
)
from intent_engineering.team_state.crypto import (
    AuthenticatedBundleContextV2,
    _encrypt_bundle_for_public_keys,
    canonical_authenticated_context_bytes,
    canonical_encrypted_bundle_bytes,
    verify_recipient_possession_proof,
)
from intent_engineering.team_state.keys import (
    DeviceEnrollmentBinding,
    DeviceKeyStore,
    DevicePublicMaterial,
    GitHubIdentity,
    GitHubIdentityVerifier,
)
from intent_engineering.team_state.models import (
    MAX_BUNDLE_BYTES,
    AuthorityAttestationV2,
    CanonicalStateSnapshot,
    CertifiedStateSignatureV2,
    CiRecipientRecord,
    DeviceCertificateClaimsV2,
    DeviceSignerCertificateV2,
    MemberRecordV2,
    StateSignatureEnvelopeV2,
    TeamAuthorityRegistryV2,
    TeamRootTrustV2,
    TeamStateManifestV2,
)
from intent_engineering.team_state.restore import VerifiedReleaseV2, _validate_v2_snapshot
from intent_engineering.team_state.signing import TeamRootKeyStore

_INVITE_MAX = 32 * 1024
_RESPONSE_MAX = 64 * 1024
_INVITE_LIFETIME = timedelta(hours=24)
_DECISION_LIFETIME = timedelta(minutes=5)
_CLOCK_SKEW = timedelta(minutes=5)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
_BASE64 = re.compile(r"^[A-Za-z0-9_-]+$")
_INVITE_ID = re.compile(r"^invite:sha256:[0-9a-f]{64}$")
_RESPONSE_DOMAIN = b"intent.team-enrollment-response.v2\0"
_INVITE_DOMAIN = b"intent.team-enrollment-invite.v2\0"
_IDENTITY_PROOF_AAD_DOMAIN = b"intent.team-enrollment-github-proof-aad.v2\0"
_PROPOSED_ADDITION_DOMAIN = b"intent.team-enrollment-proposed-addition.v2\0"


class TeamEnrollmentError(ValueError):
    """One coarse, secret-safe enrollment boundary failure."""

    def __init__(
        self,
        message: Literal[
            "team enrollment unavailable",
            "team enrollment changed",
            "enrollment transition plan changed",
            "enrollment publication authentication failed",
        ],
    ) -> None:
        super().__init__(message)


class _EnrollmentModel(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        revalidate_instances="always",
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _b64(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode()


def _decode(value: str, size: int) -> bytes:
    if type(value) is not str or not value or "=" in value or _BASE64.fullmatch(value) is None:
        raise ValueError("invalid enrollment encoding")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(decoded) != size or _b64(decoded) != value:
        raise ValueError("invalid enrollment encoding")
    return decoded


def _decode_variable(value: str, maximum: int) -> bytes:
    if type(value) is not str or not value or "=" in value or _BASE64.fullmatch(value) is None:
        raise ValueError("invalid enrollment encoding")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if not decoded or len(decoded) > maximum or _b64(decoded) != value:
        raise ValueError("invalid enrollment encoding")
    return decoded


def _utc_second(value: datetime) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.microsecond != 0
    ):
        raise ValueError("invalid enrollment time")
    return value.astimezone(UTC)


def _prepared_failure(
    error: BaseException,
    message: Literal[
        "team enrollment unavailable",
        "team enrollment changed",
        "enrollment transition plan changed",
        "enrollment publication authentication failed",
    ],
) -> BaseException:
    error_traceback = error.__traceback__
    if error_traceback is not None:
        traceback.clear_frames(error_traceback)
    error_traceback = None
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    error.args = ()
    error.__dict__.clear()
    if not isinstance(error, Exception):
        return error
    return TeamEnrollmentError(message)


class VerifiedRemoteStateV2(_EnrollmentModel):
    """Exact verified remote and protected-tooling preimage used by enrollment."""

    authority: TeamAuthorityRegistryV2
    state_commit: Annotated[str, Field(pattern=_COMMIT.pattern)]
    bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    default_branch: Annotated[str, Field(pattern=_BRANCH.pattern)]
    default_branch_commit: Annotated[str, Field(pattern=_COMMIT.pattern)]
    tooling_digest: Annotated[str, Field(pattern=_SHA256.pattern)]

    @field_validator("default_branch")
    @classmethod
    def require_safe_branch(cls, value: str) -> str:
        if value.startswith("/") or any(token in value for token in ("..", "//", "@{", "\\")):
            raise ValueError("invalid default branch")
        return value


class LiveGitHubIdentityLookup(Protocol):
    def lookup(self, account_id: int) -> GitHubIdentity: ...


class EnrollmentChallengeKeyStore(Protocol):
    """Non-exporting ephemeral X25519 seam that Task 5 can make durable."""

    def create(self, invite_id: str, private_key: bytes) -> bytes: ...

    def verify(
        self,
        invite_id: str,
        recipient_public_key: bytes,
        subject: bytes,
        proof: bytes,
    ) -> bool: ...

    def decrypt_identity_proof(
        self,
        invite_id: str,
        recipient_public_key: bytes,
        nonce: bytes,
        ciphertext: bytes,
        aad: bytes,
    ) -> bytes: ...

    def delete(self, invite_id: str) -> None: ...

    def has(self, invite_id: str) -> bool: ...


class InMemoryEnrollmentChallengeKeyStore:
    def __init__(self) -> None:
        self._keys: dict[str, bytes] = {}

    def create(self, invite_id: str, private_key: bytes) -> bytes:
        if invite_id in self._keys or type(private_key) is not bytes or len(private_key) != 32:
            raise ValueError("enrollment challenge unavailable")
        public = X25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes_raw()
        self._keys[invite_id] = private_key
        return public

    def verify(
        self,
        invite_id: str,
        recipient_public_key: bytes,
        subject: bytes,
        proof: bytes,
    ) -> bool:
        private = self._keys.get(invite_id)
        return private is not None and verify_recipient_possession_proof(
            recipient_public_key, private, subject, proof
        )

    def decrypt_identity_proof(
        self,
        invite_id: str,
        recipient_public_key: bytes,
        nonce: bytes,
        ciphertext: bytes,
        aad: bytes,
    ) -> bytes:
        private = shared = key = b""
        stored: bytes | None = None
        try:
            stored = self._keys.get(invite_id)
            if (
                stored is None
                or type(recipient_public_key) is not bytes
                or len(recipient_public_key) != 32
                or type(nonce) is not bytes
                or len(nonce) != 12
                or type(ciphertext) is not bytes
                or not 16 < len(ciphertext) <= 16 * 1024 + 16
                or type(aad) is not bytes
                or not aad
                or len(aad) > 64 * 1024
            ):
                raise ValueError("identity proof unavailable")
            private = stored
            shared = X25519PrivateKey.from_private_bytes(private).exchange(
                X25519PublicKey.from_public_bytes(recipient_public_key)
            )
            key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=b"intent.team-enrollment-github-proof.v2\0" + aad,
            ).derive(shared)
            return AESGCM(key).decrypt(nonce, ciphertext, aad)
        finally:
            private = shared = key = b""
            stored = None

    def delete(self, invite_id: str) -> None:
        if invite_id not in self._keys:
            raise ValueError("enrollment challenge unavailable")
        del self._keys[invite_id]

    def has(self, invite_id: str) -> bool:
        return invite_id in self._keys


def _invite_unsigned(
    value: TeamInviteV2 | dict[str, object], *, include_id: bool
) -> dict[str, object]:
    if isinstance(value, TeamInviteV2):
        fields = value.model_dump(mode="json", exclude={"sponsor_signature"})
    else:
        fields = dict(value)
        fields.pop("sponsor_signature", None)
    if not include_id:
        fields.pop("invite_id", None)
    return fields


class TeamInviteV2(_EnrollmentModel):
    schema_version: Literal[2] = 2
    invite_id: Annotated[str, Field(pattern=_INVITE_ID.pattern)]
    nonce: str
    challenge_public_key: str
    project_id: Annotated[str, Field(min_length=1, max_length=128)]
    repository_id: Annotated[str, Field(min_length=1, max_length=255)]
    default_branch: Annotated[str, Field(pattern=_BRANCH.pattern)]
    default_branch_commit: Annotated[str, Field(pattern=_COMMIT.pattern)]
    tooling_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    state_branch: Literal["intent-state"] = "intent-state"
    intended_github_account_id: Annotated[int, Field(gt=0)]
    intended_github_login: Annotated[str, Field(min_length=1, max_length=39)]
    intended_role: Literal["member"] = "member"
    authority_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    authority_sequence: Annotated[int, Field(ge=1)]
    next_certificate_serial: Annotated[int, Field(ge=1)]
    root: TeamRootTrustV2
    sponsor_member_id: str
    sponsor_certificate: DeviceSignerCertificateV2
    base_state_commit: Annotated[str, Field(pattern=_COMMIT.pattern)]
    base_bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    created_at: datetime
    expires_at: datetime
    sponsor_signature: str

    @field_validator("default_branch")
    @classmethod
    def require_safe_branch(cls, value: str) -> str:
        if value.startswith("/") or any(token in value for token in ("..", "//", "@{", "\\")):
            raise ValueError("invalid default branch")
        return value

    @field_validator("nonce", "challenge_public_key")
    @classmethod
    def require_public_32(cls, value: str) -> str:
        _decode(value, 32)
        return value

    @field_validator("sponsor_signature")
    @classmethod
    def require_signature(cls, value: str) -> str:
        _decode(value, 64)
        return value

    @field_validator("created_at", "expires_at")
    @classmethod
    def require_time(cls, value: datetime) -> datetime:
        return _utc_second(value)

    @field_validator(
        "intended_github_account_id", "authority_sequence", "next_certificate_serial", mode="before"
    )
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid enrollment integer")
        return value

    @model_validator(mode="after")
    def require_bindings(self) -> TeamInviteV2:
        expected_id = "invite:" + _digest(_canonical(_invite_unsigned(self, include_id=False)))
        if (
            self.invite_id != expected_id
            or self.expires_at <= self.created_at
            or self.expires_at > self.created_at + _INVITE_LIFETIME
            or self.project_id != self.root.project_id
            or self.repository_id != self.root.repository_id
            or self.sponsor_certificate.claims.member_id != self.sponsor_member_id
            or self.sponsor_certificate.root_key_id != self.root.root_key_id
            or self.sponsor_certificate.claims.project_id != self.project_id
            or self.sponsor_certificate.claims.repository_id != self.repository_id
            or self.sponsor_certificate.claims.authority_epoch != self.root.authority_epoch
            or self.sponsor_certificate.claims.issued_at > self.created_at
            or self.sponsor_certificate.claims.expires_at < self.created_at
        ):
            raise ValueError("invalid invitation binding")
        return self


class EnrollmentReplayStateStore(Protocol):
    """Public replay-state seam; Task 5 may replace the in-memory implementation."""

    def put_invite(self, invite: TeamInviteV2) -> None: ...

    def get_invite(self, invite_id: str) -> TeamInviteV2 | None: ...

    def is_consumed(self, invite_id: str) -> bool: ...

    def consume(self, invite_id: str) -> None: ...


class InMemoryEnrollmentReplayStateStore:
    def __init__(self) -> None:
        self._invites: dict[str, TeamInviteV2] = {}
        self._consumed: set[str] = set()

    def put_invite(self, invite: TeamInviteV2) -> None:
        if invite.invite_id in self._invites or invite.invite_id in self._consumed:
            raise ValueError("duplicate enrollment invitation")
        self._invites[invite.invite_id] = invite

    def get_invite(self, invite_id: str) -> TeamInviteV2 | None:
        return self._invites.get(invite_id)

    def is_consumed(self, invite_id: str) -> bool:
        return invite_id in self._consumed

    def consume(self, invite_id: str) -> None:
        if invite_id not in self._invites or invite_id in self._consumed:
            raise ValueError("enrollment invitation unavailable")
        self._consumed.add(invite_id)


class JoinResponseV2(_EnrollmentModel):
    schema_version: Literal[2] = 2
    invite_id: Annotated[str, Field(pattern=_INVITE_ID.pattern)]
    invite_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    project_id: str
    repository_id: str
    authority_before_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    base_state_commit: Annotated[str, Field(pattern=_COMMIT.pattern)]
    base_bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    github_account_id: Annotated[int, Field(gt=0)]
    github_login: str
    actor: str
    device_id: str
    recipient_key_id: str
    recipient_public_key: str
    signature_id: str
    signing_public_key: str
    credential: CredentialRecord
    webauthn_pre_assertion_sign_count: Annotated[int, Field(ge=0, le=2**32 - 1)]
    github_identity_proof_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    github_identity_proof_algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"]
    github_identity_proof_nonce: str
    github_identity_proof_ciphertext: str
    proposed_certificate_claims: DeviceCertificateClaimsV2
    proposed_member: MemberRecordV2
    transition_commitment: Annotated[str, Field(pattern=_SHA256.pattern)]
    created_at: datetime
    expires_at: datetime
    device_possession_signature: str
    recipient_possession_proof: str
    webauthn_decision: VerifiedHumanDecision
    webauthn_assertion: str

    @field_validator("github_account_id", "webauthn_pre_assertion_sign_count", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid GitHub identity")
        return value

    @field_validator("recipient_public_key", "signing_public_key")
    @classmethod
    def require_key(cls, value: str) -> str:
        _decode(value, 32)
        return value

    @field_validator("github_identity_proof_nonce")
    @classmethod
    def require_proof_nonce(cls, value: str) -> str:
        _decode(value, 12)
        return value

    @field_validator("github_identity_proof_ciphertext")
    @classmethod
    def require_proof_ciphertext(cls, value: str) -> str:
        decoded = _decode_variable(value, 16 * 1024 + 16)
        if len(decoded) <= 16:
            raise ValueError("invalid GitHub identity proof")
        return value

    @field_validator("device_possession_signature")
    @classmethod
    def require_device_signature(cls, value: str) -> str:
        _decode(value, 64)
        return value

    @field_validator("recipient_possession_proof")
    @classmethod
    def require_recipient_proof(cls, value: str) -> str:
        _decode(value, 32)
        return value

    @field_validator("webauthn_assertion")
    @classmethod
    def require_bounded_assertion(cls, value: str) -> str:
        if len(value) > 24 * 1024:
            raise ValueError("WebAuthn assertion is oversized")
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if not decoded or len(decoded) > 16 * 1024 or _b64(decoded) != value:
            raise ValueError("invalid WebAuthn assertion")
        return value

    @field_validator("created_at", "expires_at")
    @classmethod
    def require_time(cls, value: datetime) -> datetime:
        return _utc_second(value)

    @model_validator(mode="after")
    def require_response_time(self) -> JoinResponseV2:
        if (
            self.expires_at <= self.created_at
            or self.expires_at > self.created_at + _DECISION_LIFETIME
            or self.credential.sign_count < self.webauthn_pre_assertion_sign_count
            or (
                self.credential.sign_count == self.webauthn_pre_assertion_sign_count
                and self.credential.sign_count != 0
            )
        ):
            raise ValueError("invalid join response lifetime")
        return self


class SponsorJoinApprovalV2(_EnrollmentModel):
    schema_version: Literal[2] = 2
    invite_id: str
    join_response_digest: str
    authority_before_digest: str
    authority_after_digest: str
    certificate_id: str
    sponsor_member_id: str
    sponsor_decision_digest: str


class JoinApprovalPreviewV2(_EnrollmentModel):
    invite: TeamInviteV2
    response: JoinResponseV2
    response_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    authority_before_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    authority_after_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    certificate: DeviceSignerCertificateV2
    authority_after: TeamAuthorityRegistryV2
    base_state_commit: str
    base_bundle_digest: str
    default_branch_commit: str
    tooling_digest: str

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EnrollmentPublicationPlanV2(_EnrollmentModel):
    """Exact unsigned encrypted release bytes reviewed before sponsor WebAuthn."""

    manifest: TeamStateManifestV2
    authority: TeamAuthorityRegistryV2
    bundle: str = Field(max_length=MAX_BUNDLE_BYTES * 2)
    branch: str
    bundle_path: str
    signature_path: str
    snapshot_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    parent_manifest_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    parent_commit: Annotated[str, Field(pattern=_COMMIT.pattern)]

    @model_validator(mode="after")
    def require_exact_artifacts(self) -> EnrollmentPublicationPlanV2:
        bundle = _decode_variable(self.bundle, MAX_BUNDLE_BYTES)
        digest_hex = self.manifest.bundle_digest.removeprefix("sha256:")
        release = f"{self.manifest.graph_version}-{digest_hex}"
        if (
            _digest(bundle) != self.manifest.bundle_digest
            or len(bundle) != self.manifest.bundle_size
            or authority_digest(self.authority) != self.manifest.authority_digest
            or self.authority.active_recipient_key_ids() != self.manifest.recipient_key_ids
            or self.branch != f"intent-publication/{digest_hex}"
            or self.bundle_path != f"bundles/{release}.intent"
            or self.signature_path != f"signatures/{release}.json"
        ):
            raise ValueError("enrollment publication plan changed")
        return self

    def bundle_bytes(self) -> bytes:
        return _decode_variable(self.bundle, MAX_BUNDLE_BYTES)

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))

    def digest(self) -> str:
        return _digest(self.canonical_bytes())


class ApprovedAuthorityTransitionV2(_EnrollmentModel):
    approval: SponsorJoinApprovalV2
    certificate: DeviceSignerCertificateV2
    authority: TeamAuthorityRegistryV2
    attestation: AuthorityAttestationV2


@dataclass(frozen=True, slots=True)
class PreparedEnrollmentPublicationV2:
    """Three exact Git artifacts for one sponsor-approved authority transition."""

    repository_id: str
    branch: str
    manifest: TeamStateManifestV2
    manifest_bytes: bytes
    bundle: bytes
    envelope: StateSignatureEnvelopeV2
    signatures: bytes
    bundle_path: str
    signature_path: str
    authority: TeamAuthorityRegistryV2

    def __post_init__(self) -> None:
        digest_hex = self.manifest.bundle_digest.removeprefix("sha256:")
        release = f"{self.manifest.graph_version}-{digest_hex}"
        if (
            self.repository_id != self.manifest.repository_id
            or self.branch != f"intent-publication/{digest_hex}"
            or self.manifest_bytes != self.manifest.canonical_bytes()
            or self.signatures != self.envelope.canonical_bytes()
            or self.bundle_path != f"bundles/{release}.intent"
            or self.signature_path != f"signatures/{release}.json"
            or len(self.bundle) != self.manifest.bundle_size
            or _digest(self.bundle) != self.manifest.bundle_digest
            or authority_digest(self.authority) != self.manifest.authority_digest
            or self.authority.active_recipient_key_ids() != self.manifest.recipient_key_ids
            or self.envelope.authority_attestation is None
            or self.envelope.authority_attestation.operation != "enroll"
            or self.envelope.migration_proof is not None
        ):
            raise ValueError("prepared enrollment publication changed")


class EnrollmentTransitionProofV2(_EnrollmentModel):
    """Bounded root-signed proof of the parent inventory and approved descendant."""

    schema_version: Literal[2] = 2
    root: TeamRootTrustV2
    authority_before_sequence: int = Field(ge=1)
    authority_after_sequence: int = Field(ge=2)
    authority_before_digest: str
    authority_after_digest: str
    parent_members_digest: str
    parent_certificates_digest: str
    parent_revocations_digest: str
    parent_policy_digest: str
    parent_ci_recipient: CiRecipientRecord
    sponsor_member: MemberRecordV2
    sponsor_certificate: DeviceSignerCertificateV2
    invite_id: str
    response_digest: str
    new_member: MemberRecordV2
    new_certificate: DeviceSignerCertificateV2
    base_state_commit: str
    base_bundle_digest: str
    default_branch_commit: str
    tooling_digest: str
    publication_manifest_digest: str
    authority_attestation: AuthorityAttestationV2
    root_signature: str

    @field_validator(
        "authority_before_digest",
        "authority_after_digest",
        "parent_members_digest",
        "parent_certificates_digest",
        "parent_revocations_digest",
        "parent_policy_digest",
        "response_digest",
        "base_bundle_digest",
        "tooling_digest",
        "publication_manifest_digest",
    )
    @classmethod
    def require_digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("invalid enrollment transition proof digest")
        return value

    @field_validator("base_state_commit", "default_branch_commit")
    @classmethod
    def require_commit(cls, value: str) -> str:
        if _COMMIT.fullmatch(value) is None:
            raise ValueError("invalid enrollment transition proof commit")
        return value

    @field_validator("root_signature")
    @classmethod
    def require_root_signature(cls, value: str) -> str:
        _decode(value, 64)
        return value

    @model_validator(mode="after")
    def require_bindings(self) -> EnrollmentTransitionProofV2:
        if (
            self.authority_after_sequence != self.authority_before_sequence + 1
            or self.root.repository_id != self.parent_ci_recipient.repository_id
            or self.root.project_id != self.parent_ci_recipient.project_id
            or self.root.project_id != self.authority_attestation.project_id
            or self.root.repository_id != self.authority_attestation.repository_id
            or self.root.authority_epoch != self.authority_attestation.authority_epoch
            or self.root.root_key_id != self.authority_attestation.root_key_id
            or self.sponsor_member.status != "active"
            or self.sponsor_member.role != "sponsor"
            or self.sponsor_member.actor != f"github:{self.sponsor_member.github_account_id}"
            or self.sponsor_certificate.certificate_id
            not in self.sponsor_member.device_certificate_ids
            or self.sponsor_certificate.claims.member_id != self.sponsor_member.member_id
            or self.sponsor_certificate.claims.github_account_id
            != self.sponsor_member.github_account_id
            or self.sponsor_certificate.claims.github_login != self.sponsor_member.github_login
            or self.sponsor_certificate.claims.project_id != self.root.project_id
            or self.sponsor_certificate.claims.repository_id != self.root.repository_id
            or self.sponsor_certificate.claims.authority_epoch != self.root.authority_epoch
            or self.sponsor_certificate.root_key_id != self.root.root_key_id
            or self.new_member.status != "active"
            or self.new_member.role != "member"
            or self.new_member.actor != f"github:{self.new_member.github_account_id}"
            or self.new_member.member_id == self.sponsor_member.member_id
            or self.new_member.github_account_id == self.sponsor_member.github_account_id
            or self.new_member.device_certificate_ids != (self.new_certificate.certificate_id,)
            or self.new_certificate.certificate_id not in self.new_member.device_certificate_ids
            or self.new_certificate.claims.member_id != self.new_member.member_id
            or self.new_certificate.claims.github_account_id != self.new_member.github_account_id
            or self.new_certificate.claims.github_login != self.new_member.github_login
            or self.new_certificate.claims.project_id != self.root.project_id
            or self.new_certificate.claims.repository_id != self.root.repository_id
            or self.new_certificate.claims.authority_epoch != self.root.authority_epoch
            or self.new_certificate.root_key_id != self.root.root_key_id
            or self.new_member.enrolled_at != self.new_certificate.claims.issued_at
            or self.authority_attestation.decided_at != self.new_certificate.claims.issued_at
            or self.authority_attestation.operation != "enroll"
            or self.authority_attestation.previous_authority_digest != self.authority_before_digest
            or self.authority_attestation.authority_digest != self.authority_after_digest
            or self.authority_attestation.parent_bundle_digest != self.base_bundle_digest
            or self.authority_attestation.subject_digest != self.response_digest
            or self.authority_attestation.sponsor_member_id != self.sponsor_member.member_id
            or self.authority_attestation.sponsor_device_certificate_id
            != self.sponsor_certificate.certificate_id
        ):
            raise ValueError("enrollment transition proof changed")
        try:
            Ed25519PublicKey.from_public_bytes(_decode(self.root.root_public_key, 32)).verify(
                _decode(self.root_signature, 64), self.signing_preimage()
            )
        except InvalidSignature:
            raise ValueError("enrollment transition proof changed") from None
        return self

    def signing_preimage(self) -> bytes:
        return _canonical(
            {
                "domain": "intent.team.enrollment-transition-receipt.v2",
                "proof": self.model_dump(mode="json", exclude={"root_signature"}),
            }
        )

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


def enrollment_transition_plan_digest(
    preview: JoinApprovalPreviewV2,
    plan: EnrollmentPublicationPlanV2,
) -> str:
    """Commit to the deterministic transition proof fields available before approval."""
    try:
        if type(preview) is not JoinApprovalPreviewV2:
            raise ValueError("enrollment transition plan changed")
        plan = EnrollmentPublicationPlanV2.model_validate(plan.model_dump(mode="python"))
        after = plan.authority
        new_member = preview.response.proposed_member
        new_certificate = preview.certificate
        remaining_members = tuple(item for item in after.members if item != new_member)
        remaining_certificates = tuple(
            item for item in after.device_certificates if item != new_certificate
        )
        sponsor_member = next(
            item for item in remaining_members if item.member_id == preview.invite.sponsor_member_id
        )
        sponsor_certificate = next(
            item
            for item in remaining_certificates
            if item.certificate_id == preview.invite.sponsor_certificate.certificate_id
        )
        if (
            preview.authority_after != after
            or preview.authority_after_digest != authority_digest(after)
            or after.previous_authority_digest != preview.authority_before_digest
            or after.sequence != preview.invite.authority_sequence + 1
            or after.root != preview.invite.root
            or after.ci_recipient.project_id != after.project_id
            or after.ci_recipient.repository_id != after.repository_id
            or len(remaining_members) + 1 != len(after.members)
            or len(remaining_certificates) + 1 != len(after.device_certificates)
            or sponsor_member.role != "sponsor"
            or sponsor_member.status != "active"
            or sponsor_certificate != preview.invite.sponsor_certificate
            or new_member.role != "member"
            or new_member.status != "active"
            or new_member.device_certificate_ids != (new_certificate.certificate_id,)
            or new_certificate.claims.member_id != new_member.member_id
            or new_certificate.claims.github_account_id != new_member.github_account_id
            or new_certificate.claims.github_login != new_member.github_login
            or new_certificate.claims.recipient_key_id != preview.response.recipient_key_id
            or new_certificate.claims.recipient_public_key != preview.response.recipient_public_key
            or new_certificate.claims.signature_id != preview.response.signature_id
            or new_certificate.claims.signing_public_key != preview.response.signing_public_key
        ):
            raise ValueError("enrollment transition plan changed")
        return _digest(
            _canonical(
                {
                    "domain": "intent.team.enrollment-transition-plan.v2",
                    "root": after.root.model_dump(mode="json"),
                    "authority_before_sequence": preview.invite.authority_sequence,
                    "authority_after_sequence": after.sequence,
                    "authority_before_digest": preview.authority_before_digest,
                    "authority_after_digest": preview.authority_after_digest,
                    "parent_members_digest": _digest(
                        _canonical([item.model_dump(mode="json") for item in remaining_members])
                    ),
                    "parent_certificates_digest": _digest(
                        _canonical(
                            [item.model_dump(mode="json") for item in remaining_certificates]
                        )
                    ),
                    "parent_revocations_digest": _digest(
                        _canonical([item.model_dump(mode="json") for item in after.revocations])
                    ),
                    "parent_policy_digest": _digest(
                        _canonical(after.policy.model_dump(mode="json"))
                    ),
                    "parent_ci_recipient": after.ci_recipient.model_dump(mode="json"),
                    "sponsor_member": sponsor_member.model_dump(mode="json"),
                    "sponsor_certificate": sponsor_certificate.model_dump(mode="json"),
                    "invite_id": preview.invite.invite_id,
                    "response_digest": preview.response_digest,
                    "new_member": new_member.model_dump(mode="json"),
                    "new_certificate": new_certificate.model_dump(mode="json"),
                    "base_state_commit": preview.base_state_commit,
                    "base_bundle_digest": preview.base_bundle_digest,
                    "default_branch_commit": preview.default_branch_commit,
                    "tooling_digest": preview.tooling_digest,
                    "publication_manifest_digest": _digest(plan.manifest.canonical_bytes()),
                    "attestation": {
                        "project_id": after.project_id,
                        "repository_id": after.repository_id,
                        "authority_epoch": after.authority_epoch,
                        "previous_authority_digest": preview.authority_before_digest,
                        "authority_digest": preview.authority_after_digest,
                        "parent_bundle_digest": preview.base_bundle_digest,
                        "operation": "enroll",
                        "subject_digest": preview.response_digest,
                        "sponsor_member_id": sponsor_member.member_id,
                        "sponsor_device_certificate_id": sponsor_certificate.certificate_id,
                        "decided_at": plan.manifest.created_at.isoformat().replace("+00:00", "Z"),
                        "root_key_id": after.root.root_key_id,
                    },
                }
            )
        )
    except BaseException as error:  # noqa: BLE001 - fixed public verification boundary
        failure = _prepared_failure(error, "enrollment transition plan changed")
        del preview, plan, error
        if not isinstance(failure, Exception):
            raise failure.with_traceback(None) from None
        raise ValueError("enrollment transition plan changed") from None


def authenticate_enrollment_publication(
    publication: PreparedEnrollmentPublicationV2,
    proof: EnrollmentTransitionProofV2,
) -> PreparedEnrollmentPublicationV2:
    """Authenticate an enrollment release from its bounded root-signed parent proof."""
    try:
        publication = replace(publication)
        proof = EnrollmentTransitionProofV2.model_validate(proof.model_dump(mode="python"))
        after = publication.authority
        manifest = publication.manifest
        envelope = publication.envelope
        attestation = envelope.authority_attestation
        remaining_members = tuple(item for item in after.members if item != proof.new_member)
        remaining_certificates = tuple(
            item for item in after.device_certificates if item != proof.new_certificate
        )
        if (
            attestation is None
            or proof.new_member not in after.members
            or after.members.count(proof.new_member) != 1
            or proof.new_certificate not in after.device_certificates
            or after.device_certificates.count(proof.new_certificate) != 1
            or proof.sponsor_member not in remaining_members
            or proof.sponsor_certificate not in remaining_certificates
            or after.root != proof.root
            or after.project_id != proof.root.project_id
            or after.repository_id != proof.root.repository_id
            or after.sequence != proof.authority_after_sequence
            or after.previous_authority_digest != proof.authority_before_digest
            or authority_digest(after) != proof.authority_after_digest
            or after.ci_recipient != proof.parent_ci_recipient
            or _digest(_canonical([item.model_dump(mode="json") for item in remaining_members]))
            != proof.parent_members_digest
            or _digest(
                _canonical([item.model_dump(mode="json") for item in remaining_certificates])
            )
            != proof.parent_certificates_digest
            or _digest(_canonical([item.model_dump(mode="json") for item in after.revocations]))
            != proof.parent_revocations_digest
            or _digest(_canonical(after.policy.model_dump(mode="json")))
            != proof.parent_policy_digest
            or manifest.authority_digest != proof.authority_after_digest
            or manifest.parent_bundle_digest != proof.base_bundle_digest
            or _digest(publication.manifest_bytes) != proof.publication_manifest_digest
            or envelope.manifest_digest != _digest(publication.manifest_bytes)
            or envelope.bundle_digest != manifest.bundle_digest
            or envelope.bundle_digest != _digest(publication.bundle)
            or envelope.authority_digest != manifest.authority_digest
            or envelope.authority_digest != authority_digest(after)
            or attestation != proof.authority_attestation
            or attestation.decided_at != manifest.created_at
            or envelope.certificates != (proof.sponsor_certificate,)
            or len(envelope.signatures) != 1
            or envelope.signatures[0].certificate_id != proof.sponsor_certificate.certificate_id
            or envelope.signatures[0].signature_id != proof.sponsor_certificate.claims.signature_id
            or not proof.sponsor_certificate.claims.issued_at
            <= attestation.decided_at
            < proof.sponsor_certificate.claims.expires_at
            or any(
                revocation.certificate_id == proof.sponsor_certificate.certificate_id
                and revocation.revoked_at <= attestation.decided_at
                for revocation in after.revocations
            )
        ):
            raise ValueError("enrollment publication authentication failed")
        root_key = Ed25519PublicKey.from_public_bytes(_decode(proof.root.root_public_key, 32))
        for certificate in after.device_certificates:
            root_key.verify(
                _decode(certificate.root_signature, 64),
                canonical_certificate_signing_preimage(certificate.claims),
            )
        root_key.verify(
            _decode(attestation.root_signature, 64),
            canonical_authority_attestation_preimage(attestation),
        )
        Ed25519PublicKey.from_public_bytes(
            _decode(proof.sponsor_certificate.claims.signing_public_key, 32)
        ).verify(
            _decode(envelope.signatures[0].signature, 64),
            canonical_state_signature_preimage(manifest),
        )
        return publication
    except BaseException as error:  # noqa: BLE001 - fixed public verification boundary
        failure = _prepared_failure(error, "enrollment publication authentication failed")
        del publication, proof, error
        if not isinstance(failure, Exception):
            raise failure.with_traceback(None) from None
        raise ValueError("enrollment publication authentication failed") from None


def _export_team_invite_unsafe(invite: TeamInviteV2) -> bytes:
    try:
        value = TeamInviteV2.model_validate(invite)
        content = _canonical(value.model_dump(mode="json"))
        if not content or len(content) > _INVITE_MAX:
            raise ValueError("invalid invitation size")
        return content
    except BaseException as error:  # noqa: BLE001
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        if not isinstance(error, Exception):
            raise error.with_traceback(None)
        raise TeamEnrollmentError("team enrollment unavailable") from None


def export_team_invite(invite: TeamInviteV2) -> bytes:
    try:
        return _export_team_invite_unsafe(invite)
    except BaseException as error:  # noqa: BLE001
        failure = _prepared_failure(error, "team enrollment unavailable")
        del invite, error
        raise failure.with_traceback(None) from None


def _parse_team_invite_unsafe(content: bytes) -> TeamInviteV2:
    try:
        if type(content) is not bytes or not content or len(content) > _INVITE_MAX:
            raise ValueError("invalid invitation")
        text = content.decode("utf-8")
        loads_strict_object(text)
        value = TeamInviteV2.model_validate_json(content)
        if export_team_invite(value) != content:
            raise ValueError("noncanonical invitation")
        return value
    except BaseException as error:  # noqa: BLE001
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        if not isinstance(error, Exception):
            raise error.with_traceback(None)
        raise TeamEnrollmentError("team enrollment unavailable") from None


def parse_team_invite(content: bytes) -> TeamInviteV2:
    try:
        return _parse_team_invite_unsafe(content)
    except BaseException as error:  # noqa: BLE001
        failure = _prepared_failure(error, "team enrollment unavailable")
        del content, error
        raise failure.with_traceback(None) from None


def _export_join_response_unsafe(response: JoinResponseV2) -> bytes:
    try:
        value = JoinResponseV2.model_validate(response)
        content = _canonical(value.model_dump(mode="json"))
        if not content or len(content) > _RESPONSE_MAX:
            raise ValueError("invalid response size")
        return content
    except BaseException as error:  # noqa: BLE001
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        if not isinstance(error, Exception):
            raise error.with_traceback(None)
        raise TeamEnrollmentError("team enrollment unavailable") from None


def export_join_response(response: JoinResponseV2) -> bytes:
    try:
        return _export_join_response_unsafe(response)
    except BaseException as error:  # noqa: BLE001
        failure = _prepared_failure(error, "team enrollment unavailable")
        del response, error
        raise failure.with_traceback(None) from None


def _parse_join_response_unsafe(content: bytes) -> JoinResponseV2:
    try:
        if type(content) is not bytes or not content or len(content) > _RESPONSE_MAX:
            raise ValueError("invalid response")
        text = content.decode("utf-8")
        loads_strict_object(text)
        value = JoinResponseV2.model_validate_json(content)
        if export_join_response(value) != content:
            raise ValueError("noncanonical response")
        return value
    except BaseException as error:  # noqa: BLE001
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        if not isinstance(error, Exception):
            raise error.with_traceback(None)
        raise TeamEnrollmentError("team enrollment unavailable") from None


def parse_join_response(content: bytes) -> JoinResponseV2:
    try:
        return _parse_join_response_unsafe(content)
    except BaseException as error:  # noqa: BLE001
        failure = _prepared_failure(error, "team enrollment unavailable")
        del content, error
        raise failure.with_traceback(None) from None


def _credential_digest(credential: CredentialRecord) -> str:
    return _digest(credential.canonical_bytes())


def _decision_repository_id(repository_id: str) -> str:
    return (
        "repo:sha256:"
        + hashlib.sha256(
            b"intent.team-enrollment.local-repository.v2\0" + repository_id.encode()
        ).hexdigest()
    )


def _proposed_addition(
    *,
    invite: TeamInviteV2,
    identity: GitHubIdentity,
    material: DevicePublicMaterial,
    credential: CredentialRecord,
    issued_at: datetime,
) -> tuple[DeviceCertificateClaimsV2, MemberRecordV2]:
    member_id = derive_member_id(invite.project_id, invite.repository_id, int(identity.account_id))
    claims = DeviceCertificateClaimsV2(
        project_id=invite.project_id,
        repository_id=invite.repository_id,
        authority_epoch=invite.root.authority_epoch,
        member_id=member_id,
        device_id=material.device_id,
        github_account_id=int(identity.account_id),
        github_login=identity.login,
        recipient_key_id=material.recipient_key_id,
        recipient_public_key=_b64(material.recipient_public_key),
        signature_id=material.signature_id,
        signing_public_key=_b64(material.signing_public_key),
        webauthn_credential_digest=_credential_digest(credential),
        serial=invite.next_certificate_serial,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(days=366),
    )
    member = MemberRecordV2(
        member_id=member_id,
        actor=f"github:{identity.account_id}",
        github_account_id=int(identity.account_id),
        github_login=identity.login,
        role="member",
        status="active",
        device_certificate_ids=(derive_certificate_id(claims),),
        enrolled_at=issued_at,
    )
    return claims, member


def _proposed_addition_commitment(
    *,
    invite_digest: str,
    authority_before_digest: str,
    base_state_commit: str,
    base_bundle_digest: str,
    claims: DeviceCertificateClaimsV2,
    member: MemberRecordV2,
) -> str:
    return _digest(
        _PROPOSED_ADDITION_DOMAIN
        + _canonical(
            {
                "authority_before_digest": authority_before_digest,
                "base_bundle_digest": base_bundle_digest,
                "base_state_commit": base_state_commit,
                "certificate_claims": claims.model_dump(mode="json"),
                "invite_digest": invite_digest,
                "member": member.model_dump(mode="json"),
                "operation": "enroll",
                "schema_version": 2,
            }
        )
    )


def _join_subject(
    *,
    invite: TeamInviteV2,
    identity: GitHubIdentity,
    material: DevicePublicMaterial,
    credential: CredentialRecord,
    pre_assertion_sign_count: int,
    identity_proof_digest: str,
    proposed_certificate_claims: DeviceCertificateClaimsV2,
    proposed_member: MemberRecordV2,
    transition_commitment: str,
    created_at: datetime,
    expires_at: datetime,
) -> bytes:
    return _canonical(
        {
            "actor": f"github:{identity.account_id}",
            "authority_before_digest": invite.authority_digest,
            "base_bundle_digest": invite.base_bundle_digest,
            "base_state_commit": invite.base_state_commit,
            "credential": credential.model_dump(mode="json"),
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "device_id": material.device_id,
            "github_account_id": int(identity.account_id),
            "github_login": identity.login,
            "invite_digest": _digest(export_team_invite(invite)),
            "github_identity_proof_algorithm": "x25519-hkdf-sha256-aes256gcm-v1",
            "github_identity_proof_digest": identity_proof_digest,
            "recipient_key_id": material.recipient_key_id,
            "recipient_public_key": _b64(material.recipient_public_key),
            "signature_id": material.signature_id,
            "signing_public_key": _b64(material.signing_public_key),
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "project_id": invite.project_id,
            "repository_id": invite.repository_id,
            "schema_version": 2,
            "proposed_certificate_claims": proposed_certificate_claims.model_dump(mode="json"),
            "proposed_member": proposed_member.model_dump(mode="json"),
            "transition_commitment": transition_commitment,
            "webauthn_pre_assertion_sign_count": pre_assertion_sign_count,
        }
    )


def build_join_decision_payload(
    *,
    invite: TeamInviteV2,
    identity: GitHubIdentity,
    material: DevicePublicMaterial,
    credential: CredentialRecord,
    identity_proof: bytes,
    pre_assertion_sign_count: int,
    challenge: bytes,
    now: datetime,
) -> HumanDecisionPayload:
    now = _utc_second(now)
    identity_proof_digest = ""
    try:
        if (
            type(challenge) is not bytes
            or len(challenge) < 16
            or type(identity_proof) is not bytes
            or not identity_proof
            or len(identity_proof) > 16 * 1024
            or type(pre_assertion_sign_count) is not int
            or not 0 <= pre_assertion_sign_count <= 2**32 - 1
            or credential.sign_count < pre_assertion_sign_count
            or (credential.sign_count == pre_assertion_sign_count and credential.sign_count != 0)
        ):
            raise ValueError("invalid decision challenge")
        identity_proof_digest = _digest(identity_proof)
    finally:
        identity_proof = b""
    expires_at = min(invite.expires_at, now + _DECISION_LIFETIME)
    claims, member = _proposed_addition(
        invite=invite,
        identity=identity,
        material=material,
        credential=credential,
        issued_at=now,
    )
    invite_digest = _digest(export_team_invite(invite))
    commitment = _proposed_addition_commitment(
        invite_digest=invite_digest,
        authority_before_digest=invite.authority_digest,
        base_state_commit=invite.base_state_commit,
        base_bundle_digest=invite.base_bundle_digest,
        claims=claims,
        member=member,
    )
    subject = _join_subject(
        invite=invite,
        identity=identity,
        material=material,
        credential=credential,
        pre_assertion_sign_count=pre_assertion_sign_count,
        identity_proof_digest=identity_proof_digest,
        proposed_certificate_claims=claims,
        proposed_member=member,
        transition_commitment=commitment,
        created_at=now,
        expires_at=expires_at,
    )
    return HumanDecisionPayload(
        project_id=invite.project_id,
        repository_id=_decision_repository_id(invite.repository_id),
        actor=f"github:{identity.account_id}",
        action=DecisionAction.APPROVE_EXTERNAL_WRITE,
        graph_version=invite.authority_sequence,
        parent_bundle_digest=invite.base_bundle_digest,
        subject=DecisionSubject(
            kind="team_join", id="team_join:" + invite.invite_id.removeprefix("invite:")
        ),
        subject_digest=_digest(subject),
        result_digest=_digest(material.recipient_public_key + material.signing_public_key),
        challenge="challenge:" + hashlib.sha256(challenge).hexdigest(),
        issued_at=now,
        expires_at=expires_at,
    )


def build_sponsor_decision_payload(
    *,
    preview: JoinApprovalPreviewV2,
    credential: CredentialRecord,
    challenge: bytes,
    now: datetime,
    approval_request_digest: str | None = None,
) -> HumanDecisionPayload:
    now = _utc_second(now)
    if (
        type(challenge) is not bytes
        or len(challenge) < 16
        or (
            approval_request_digest is not None
            and _SHA256.fullmatch(approval_request_digest) is None
        )
    ):
        raise ValueError("invalid decision challenge")
    sponsor = next(
        member
        for member in preview.authority_after.members
        if member.member_id == preview.invite.sponsor_member_id
    )
    return HumanDecisionPayload(
        project_id=preview.invite.project_id,
        repository_id=_decision_repository_id(preview.invite.repository_id),
        actor=sponsor.actor,
        action=DecisionAction.APPROVE_EXTERNAL_WRITE,
        graph_version=preview.authority_after.sequence,
        parent_bundle_digest=preview.base_bundle_digest,
        subject=DecisionSubject(
            kind="join_approval",
            id="join_approval:" + preview.response_digest.removeprefix("sha256:"),
        ),
        subject_digest=(
            _digest(
                _canonical(
                    {
                        "approval_request_digest": approval_request_digest,
                        "preview": preview.model_dump(mode="json"),
                    }
                )
            )
            if approval_request_digest is not None
            else _digest(preview.canonical_bytes())
        ),
        result_digest=(approval_request_digest or preview.authority_after_digest),
        challenge="challenge:" + hashlib.sha256(challenge).hexdigest(),
        issued_at=now,
        expires_at=now + _DECISION_LIFETIME,
    )


class TeamEnrollmentService:
    """Pure ceremony with private operations delegated to device/root stores."""

    def __init__(
        self,
        *,
        device_store: DeviceKeyStore,
        root_store: TeamRootKeyStore | None = None,
        sponsor_certificate_id: str | None = None,
        webauthn_verifier: WebAuthnVerifier | None = None,
        expected_origin: str | None = None,
        expected_rp_id: str | None = None,
        github_identity_verifier: GitHubIdentityVerifier | None = None,
        identity_lookup: LiveGitHubIdentityLookup | None = None,
        replay_store: EnrollmentReplayStateStore | None = None,
        challenge_store: EnrollmentChallengeKeyStore | None = None,
        nonce_source: Callable[[], bytes] = lambda: os.urandom(32),
        challenge_private_key_source: Callable[[], bytes] = lambda: os.urandom(32),
    ) -> None:
        self._device_store = device_store
        self._root_store = root_store
        self._sponsor_certificate_id = sponsor_certificate_id
        self._webauthn_verifier = webauthn_verifier
        self._expected_origin = expected_origin
        self._expected_rp_id = expected_rp_id
        self._github_identity_verifier = github_identity_verifier
        self._identity_lookup = identity_lookup
        self._replay_store = (
            replay_store if replay_store is not None else InMemoryEnrollmentReplayStateStore()
        )
        self._challenge_store = (
            challenge_store
            if challenge_store is not None
            else InMemoryEnrollmentChallengeKeyStore()
        )
        self._nonce_source = nonce_source
        self._challenge_source = challenge_private_key_source
        self._device_material: dict[str, DevicePublicMaterial] = {}

    @staticmethod
    def _raise_public(
        error: BaseException,
        message: Literal["team enrollment unavailable", "team enrollment changed"],
    ) -> Never:
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        error.args = ()
        if not isinstance(error, Exception):
            raise error.with_traceback(None)
        raise TeamEnrollmentError(message) from None

    def _verify_invite_public(self, invite: TeamInviteV2) -> None:
        value = TeamInviteV2.model_validate(invite.model_dump(mode="python"))
        certificate = value.sponsor_certificate
        Ed25519PublicKey.from_public_bytes(_decode(value.root.root_public_key, 32)).verify(
            _decode(certificate.root_signature, 64),
            canonical_certificate_signing_preimage(certificate.claims),
        )
        Ed25519PublicKey.from_public_bytes(
            _decode(certificate.claims.signing_public_key, 32)
        ).verify(
            _decode(value.sponsor_signature, 64),
            _INVITE_DOMAIN + _canonical(_invite_unsigned(value, include_id=True)),
        )

    @staticmethod
    def _active_sponsor(
        authority: TeamAuthorityRegistryV2,
        certificate_id: str,
        now: datetime,
    ) -> tuple[MemberRecordV2, DeviceSignerCertificateV2]:
        certificate = next(
            item for item in authority.device_certificates if item.certificate_id == certificate_id
        )
        member = next(
            item for item in authority.members if item.member_id == certificate.claims.member_id
        )
        if (
            member.role != "sponsor"
            or member.status != "active"
            or certificate.certificate_id not in member.device_certificate_ids
            or any(
                revocation.certificate_id == certificate.certificate_id
                and revocation.revoked_at <= now
                for revocation in authority.revocations
            )
            or certificate.claims.issued_at > now
            or certificate.claims.expires_at < now
        ):
            raise ValueError("active sponsor certificate required")
        return member, certificate

    def _verify_webauthn_evidence(
        self,
        *,
        assertion: bytes,
        decision: VerifiedHumanDecision,
        pre_assertion_sign_count: int,
        now: datetime,
    ) -> VerifiedHumanDecision:
        verifier = self._webauthn_verifier
        if (
            verifier is None
            or not self._expected_origin
            or not self._expected_rp_id
            or not decision.payload.issued_at <= now < decision.payload.expires_at
        ):
            raise ValueError("trusted WebAuthn verifier unavailable")
        credential = decision.credential
        if (
            type(pre_assertion_sign_count) is not int
            or not 0 <= pre_assertion_sign_count <= 2**32 - 1
            or credential.sign_count < pre_assertion_sign_count
            or (credential.sign_count == pre_assertion_sign_count and credential.sign_count != 0)
        ):
            raise ValueError("WebAuthn counter verification failed")
        credential_before = credential.model_copy(update={"sign_count": pre_assertion_sign_count})
        request = AuthenticationRequest(
            challenge=hashlib.sha256(decision.payload.canonical_bytes()).digest(),
            rp_id=self._expected_rp_id,
            expected_origin=self._expected_origin,
            project_id=decision.payload.project_id,
            repository_id=decision.payload.repository_id,
            actor=decision.payload.actor,
            payload_bytes=decision.payload.canonical_bytes(),
            credentials=(credential_before,),
        )
        result = verifier.verify_authentication(assertion, request)
        if (
            type(result) is not VerifiedAuthentication
            or result.user_verified is not True
            or _b64(result.credential_id) != credential.credential_id
            or result.new_sign_count != credential.sign_count
            or result.new_sign_count < 0
        ):
            raise ValueError("WebAuthn assertion verification failed")
        return decision

    def _build_transition(
        self,
        *,
        invite: TeamInviteV2,
        current: VerifiedRemoteStateV2,
        identity: GitHubIdentity,
        material: DevicePublicMaterial,
        credential: CredentialRecord,
        issued_at: datetime,
    ) -> tuple[DeviceSignerCertificateV2, TeamAuthorityRegistryV2]:
        authority = current.authority
        if (
            len(authority.members) >= authority.policy.max_active_members
            or len(authority.device_certificates) >= authority.policy.max_active_devices
            or len(authority.active_recipient_key_ids()) >= 64
            or any(
                member.github_account_id == int(identity.account_id) for member in authority.members
            )
            or any(
                certificate.claims.device_id == material.device_id
                or certificate.claims.recipient_key_id == material.recipient_key_id
                or certificate.claims.signature_id == material.signature_id
                for certificate in authority.device_certificates
            )
        ):
            raise ValueError("duplicate or capacity exceeded")
        if self._root_store is None:
            raise ValueError("root store unavailable")
        member_id = derive_member_id(
            invite.project_id, invite.repository_id, int(identity.account_id)
        )
        claims = DeviceCertificateClaimsV2(
            project_id=invite.project_id,
            repository_id=invite.repository_id,
            authority_epoch=invite.root.authority_epoch,
            member_id=member_id,
            device_id=material.device_id,
            github_account_id=int(identity.account_id),
            github_login=identity.login,
            recipient_key_id=material.recipient_key_id,
            recipient_public_key=_b64(material.recipient_public_key),
            signature_id=material.signature_id,
            signing_public_key=_b64(material.signing_public_key),
            webauthn_credential_digest=_credential_digest(credential),
            serial=max(item.claims.serial for item in authority.device_certificates) + 1,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(days=366),
        )
        certificate = issue_device_certificate(claims, self._root_store)
        member = MemberRecordV2(
            member_id=member_id,
            actor=f"github:{identity.account_id}",
            github_account_id=int(identity.account_id),
            github_login=identity.login,
            role="member",
            status="active",
            device_certificate_ids=(certificate.certificate_id,),
            enrolled_at=issued_at,
        )
        before = authority_digest(authority)
        after = TeamAuthorityRegistryV2(
            project_id=authority.project_id,
            repository_id=authority.repository_id,
            authority_epoch=authority.authority_epoch,
            sequence=authority.sequence + 1,
            root=authority.root,
            policy=authority.policy,
            members=tuple(sorted((*authority.members, member), key=lambda item: item.member_id)),
            device_certificates=tuple(
                sorted(
                    (*authority.device_certificates, certificate),
                    key=lambda item: item.certificate_id,
                )
            ),
            revocations=authority.revocations,
            ci_recipient=authority.ci_recipient,
            previous_authority_digest=before,
        )
        return certificate, after

    def _device_public_material_unsafe(
        self, *, invite: TeamInviteV2, local_identity: GitHubIdentity
    ) -> DevicePublicMaterial:
        try:
            self._verify_invite_public(invite)
            if (
                local_identity.account_id != str(invite.intended_github_account_id)
                or local_identity.login != invite.intended_github_login
            ):
                raise ValueError("identity mismatch")
            binding = self._device_store.enrollment_binding()
            expected = DeviceEnrollmentBinding(
                project_id=invite.project_id,
                repository_id=invite.repository_id,
                actor=f"github:{invite.intended_github_account_id}",
                github_account_id=invite.intended_github_account_id,
                github_login=invite.intended_github_login,
                device_id=binding.device_id,
            )
            material = self._device_store.create(expected)
            self._device_material[invite.invite_id] = material
            return material
        except BaseException as error:  # noqa: BLE001
            self._raise_public(error, "team enrollment unavailable")

    def _create_invite_unsafe(
        self,
        *,
        state: VerifiedRemoteStateV2,
        intended_identity: GitHubIdentity,
        now: datetime,
    ) -> TeamInviteV2:
        try:
            state = VerifiedRemoteStateV2.model_validate(state.model_dump(mode="python"))
            identity = GitHubIdentity.model_validate(intended_identity.model_dump(mode="python"))
            now = _utc_second(now)
            if self._sponsor_certificate_id is None:
                raise ValueError("sponsor certificate unavailable")
            sponsor, certificate = self._active_sponsor(
                state.authority, self._sponsor_certificate_id, now
            )
            if any(
                item.github_account_id == int(identity.account_id)
                for item in state.authority.members
            ):
                raise ValueError("invalid sponsor or duplicate member")
            nonce = self._nonce_source()
            challenge_private = self._challenge_source()
            if (
                type(nonce) is not bytes
                or len(nonce) != 32
                or type(challenge_private) is not bytes
                or len(challenge_private) != 32
            ):
                raise ValueError("invalid enrollment randomness")
            challenge_public = (
                X25519PrivateKey.from_private_bytes(challenge_private)
                .public_key()
                .public_bytes_raw()
            )
            unsigned: dict[str, object] = {
                "schema_version": 2,
                "nonce": _b64(nonce),
                "challenge_public_key": _b64(challenge_public),
                "project_id": state.authority.project_id,
                "repository_id": state.authority.repository_id,
                "default_branch": state.default_branch,
                "default_branch_commit": state.default_branch_commit,
                "tooling_digest": state.tooling_digest,
                "state_branch": "intent-state",
                "intended_github_account_id": int(identity.account_id),
                "intended_github_login": identity.login,
                "intended_role": "member",
                "authority_digest": authority_digest(state.authority),
                "authority_sequence": state.authority.sequence,
                "next_certificate_serial": max(
                    item.claims.serial for item in state.authority.device_certificates
                )
                + 1,
                "root": state.authority.root.model_dump(mode="json"),
                "sponsor_member_id": sponsor.member_id,
                "sponsor_certificate": certificate.model_dump(mode="json"),
                "base_state_commit": state.state_commit,
                "base_bundle_digest": state.bundle_digest,
                "created_at": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + _INVITE_LIFETIME).isoformat().replace("+00:00", "Z"),
            }
            invite_id = "invite:" + _digest(_canonical(unsigned))
            if (
                self._replay_store.get_invite(invite_id) is not None
                or self._replay_store.is_consumed(invite_id)
                or self._challenge_store.has(invite_id)
            ):
                raise ValueError("duplicate invitation")
            with_id = {**unsigned, "invite_id": invite_id}
            signature = self._device_store.sign(
                certificate.claims.signature_id, _INVITE_DOMAIN + _canonical(with_id)
            )
            invite = TeamInviteV2(
                invite_id=invite_id,
                nonce=_b64(nonce),
                challenge_public_key=_b64(challenge_public),
                project_id=state.authority.project_id,
                repository_id=state.authority.repository_id,
                default_branch=state.default_branch,
                default_branch_commit=state.default_branch_commit,
                tooling_digest=state.tooling_digest,
                state_branch="intent-state",
                intended_github_account_id=int(identity.account_id),
                intended_github_login=identity.login,
                intended_role="member",
                authority_digest=authority_digest(state.authority),
                authority_sequence=state.authority.sequence,
                next_certificate_serial=max(
                    item.claims.serial for item in state.authority.device_certificates
                )
                + 1,
                root=state.authority.root,
                sponsor_member_id=sponsor.member_id,
                sponsor_certificate=certificate,
                base_state_commit=state.state_commit,
                base_bundle_digest=state.bundle_digest,
                created_at=now,
                expires_at=now + _INVITE_LIFETIME,
                sponsor_signature=_b64(signature),
            )
            export_team_invite(invite)
            if (
                self._challenge_store.create(invite.invite_id, challenge_private)
                != challenge_public
            ):
                raise ValueError("enrollment challenge changed")
            self._replay_store.put_invite(invite)
            return invite
        except BaseException as error:  # noqa: BLE001
            self._raise_public(error, "team enrollment unavailable")

    def _create_join_response_unsafe(
        self,
        *,
        invite: TeamInviteV2,
        local_identity: GitHubIdentity,
        decision: VerifiedHumanDecision,
        identity_proof: bytes,
        pre_assertion_sign_count: int,
        webauthn_assertion: bytes,
        now: datetime,
    ) -> JoinResponseV2:
        try:
            self._verify_invite_public(invite)
            now = _utc_second(now)
            identity = GitHubIdentity.model_validate(local_identity.model_dump(mode="python"))
            if (
                now < invite.created_at - _CLOCK_SKEW
                or now >= invite.expires_at
                or identity.account_id != str(invite.intended_github_account_id)
                or identity.login != invite.intended_github_login
                or type(decision) is not VerifiedHumanDecision
                or type(identity_proof) is not bytes
                or not identity_proof
                or len(identity_proof) > 16 * 1024
            ):
                raise ValueError("invalid invitation identity or time")
            material = self._device_material.get(invite.invite_id)
            if material is None:
                material = self.device_public_material(invite=invite, local_identity=identity)
            credential = decision.credential
            expected = build_join_decision_payload(
                invite=invite,
                identity=identity,
                material=material,
                credential=credential,
                identity_proof=identity_proof,
                pre_assertion_sign_count=pre_assertion_sign_count,
                challenge=b"placeholder-challenge",
                now=decision.payload.issued_at,
            ).model_copy(update={"challenge": decision.payload.challenge})
            if (
                decision.payload != expected
                or decision.verified_at < decision.payload.issued_at
                or decision.verified_at > decision.payload.expires_at
                or now != decision.payload.issued_at
                or now < decision.verified_at
                or now > decision.payload.expires_at
                or credential.local_only
                or credential.project_id != invite.project_id
                or credential.repository_id != _decision_repository_id(invite.repository_id)
                or credential.actor != f"github:{identity.account_id}"
                or credential.github_account_id != identity.account_id
                or credential.github_login != identity.login
            ):
                raise ValueError("invalid join decision")
            claims, member = _proposed_addition(
                invite=invite,
                identity=identity,
                material=material,
                credential=credential,
                issued_at=now,
            )
            invite_digest = _digest(export_team_invite(invite))
            commitment = _proposed_addition_commitment(
                invite_digest=invite_digest,
                authority_before_digest=invite.authority_digest,
                base_state_commit=invite.base_state_commit,
                base_bundle_digest=invite.base_bundle_digest,
                claims=claims,
                member=member,
            )
            subject = _join_subject(
                invite=invite,
                identity=identity,
                material=material,
                credential=credential,
                pre_assertion_sign_count=pre_assertion_sign_count,
                identity_proof_digest=_digest(identity_proof),
                proposed_certificate_claims=claims,
                proposed_member=member,
                transition_commitment=commitment,
                created_at=now,
                expires_at=min(invite.expires_at, now + _DECISION_LIFETIME),
            )
            signature = self._device_store.sign(material.signature_id, _RESPONSE_DOMAIN + subject)
            proof = self._device_store.prove_recipient_possession(
                material.recipient_key_id,
                _decode(invite.challenge_public_key, 32),
                subject,
            )
            aad = _IDENTITY_PROOF_AAD_DOMAIN + subject
            sealed = self._device_store.seal_identity_proof(
                material.recipient_key_id,
                _decode(invite.challenge_public_key, 32),
                identity_proof,
                aad,
            )
            response = JoinResponseV2(
                invite_id=invite.invite_id,
                invite_digest=invite_digest,
                project_id=invite.project_id,
                repository_id=invite.repository_id,
                authority_before_digest=invite.authority_digest,
                base_state_commit=invite.base_state_commit,
                base_bundle_digest=invite.base_bundle_digest,
                github_account_id=int(identity.account_id),
                github_login=identity.login,
                actor=f"github:{identity.account_id}",
                device_id=material.device_id,
                recipient_key_id=material.recipient_key_id,
                recipient_public_key=_b64(material.recipient_public_key),
                signature_id=material.signature_id,
                signing_public_key=_b64(material.signing_public_key),
                credential=credential,
                webauthn_pre_assertion_sign_count=pre_assertion_sign_count,
                github_identity_proof_digest=_digest(identity_proof),
                github_identity_proof_algorithm="x25519-hkdf-sha256-aes256gcm-v1",
                github_identity_proof_nonce=_b64(sealed.nonce),
                github_identity_proof_ciphertext=_b64(sealed.ciphertext),
                proposed_certificate_claims=claims,
                proposed_member=member,
                transition_commitment=commitment,
                created_at=now,
                expires_at=min(invite.expires_at, now + _DECISION_LIFETIME),
                device_possession_signature=_b64(signature),
                recipient_possession_proof=_b64(proof),
                webauthn_decision=decision,
                webauthn_assertion=_b64(webauthn_assertion),
            )
            export_join_response(response)
            return response
        except BaseException as error:  # noqa: BLE001
            self._raise_public(error, "team enrollment unavailable")
        finally:
            identity_proof = b""

    @staticmethod
    def _state_matches_invite(current: VerifiedRemoteStateV2, invite: TeamInviteV2) -> bool:
        return (
            current.authority.project_id == invite.project_id
            and current.authority.repository_id == invite.repository_id
            and current.authority.root == invite.root
            and authority_digest(current.authority) == invite.authority_digest
            and current.authority.sequence == invite.authority_sequence
            and current.state_commit == invite.base_state_commit
            and current.bundle_digest == invite.base_bundle_digest
            and current.default_branch == invite.default_branch
            and current.default_branch_commit == invite.default_branch_commit
            and current.tooling_digest == invite.tooling_digest
        )

    def _preview_approval_unsafe(
        self,
        *,
        invite: TeamInviteV2,
        response: JoinResponseV2,
        current: VerifiedRemoteStateV2,
        now: datetime,
    ) -> JoinApprovalPreviewV2:
        identity_proof = b""
        try:
            now = _utc_second(now)
            current = VerifiedRemoteStateV2.model_validate(current.model_dump(mode="python"))
            if self._replay_store.get_invite(invite.invite_id) != invite:
                raise TeamEnrollmentError("team enrollment changed")
            self._verify_invite_public(invite)
            if not self._state_matches_invite(current, invite):
                raise TeamEnrollmentError("team enrollment changed")
            if (
                self._replay_store.is_consumed(invite.invite_id)
                or now < invite.created_at - _CLOCK_SKEW
                or now >= invite.expires_at
                or now >= response.webauthn_decision.payload.expires_at
                or now >= response.expires_at
            ):
                raise ValueError("expired or replayed invitation")
            if (
                response.invite_id != invite.invite_id
                or response.invite_digest != _digest(export_team_invite(invite))
                or response.project_id != invite.project_id
                or response.repository_id != invite.repository_id
                or response.authority_before_digest != invite.authority_digest
                or response.base_state_commit != invite.base_state_commit
                or response.base_bundle_digest != invite.base_bundle_digest
                or response.github_account_id != invite.intended_github_account_id
                or response.github_login != invite.intended_github_login
                or response.actor != f"github:{response.github_account_id}"
            ):
                raise ValueError("join response binding changed")
            sponsor, sponsor_certificate = self._active_sponsor(
                current.authority, invite.sponsor_certificate.certificate_id, now
            )
            if sponsor_certificate != invite.sponsor_certificate:
                raise ValueError("sponsor certificate changed")
            material = DevicePublicMaterial(
                device_id=response.device_id,
                recipient_key_id=response.recipient_key_id,
                recipient_public_key=_decode(response.recipient_public_key, 32),
                signature_id=response.signature_id,
                signing_public_key=_decode(response.signing_public_key, 32),
            )
            identity = GitHubIdentity(
                account_id=str(response.github_account_id), login=response.github_login
            )
            credential = response.credential
            claims, member = _proposed_addition(
                invite=invite,
                identity=identity,
                material=material,
                credential=credential,
                issued_at=response.created_at,
            )
            commitment = _proposed_addition_commitment(
                invite_digest=response.invite_digest,
                authority_before_digest=response.authority_before_digest,
                base_state_commit=response.base_state_commit,
                base_bundle_digest=response.base_bundle_digest,
                claims=claims,
                member=member,
            )
            if (
                response.proposed_certificate_claims != claims
                or response.proposed_member != member
                or response.transition_commitment != commitment
                or invite.next_certificate_serial
                != max(item.claims.serial for item in current.authority.device_certificates) + 1
            ):
                raise ValueError("proposed join transition changed")
            subject = _join_subject(
                invite=invite,
                identity=identity,
                material=material,
                credential=credential,
                pre_assertion_sign_count=response.webauthn_pre_assertion_sign_count,
                identity_proof_digest=response.github_identity_proof_digest,
                proposed_certificate_claims=claims,
                proposed_member=member,
                transition_commitment=commitment,
                created_at=response.created_at,
                expires_at=response.expires_at,
            )
            Ed25519PublicKey.from_public_bytes(material.signing_public_key).verify(
                _decode(response.device_possession_signature, 64), _RESPONSE_DOMAIN + subject
            )
            if not self._challenge_store.verify(
                invite.invite_id,
                material.recipient_public_key,
                subject,
                _decode(response.recipient_possession_proof, 32),
            ):
                raise ValueError("recipient possession changed")
            identity_proof = self._challenge_store.decrypt_identity_proof(
                invite.invite_id,
                material.recipient_public_key,
                _decode(response.github_identity_proof_nonce, 12),
                _decode_variable(response.github_identity_proof_ciphertext, 16 * 1024 + 16),
                _IDENTITY_PROOF_AAD_DOMAIN + subject,
            )
            proof_digest = _digest(identity_proof)
            if self._github_identity_verifier is None:
                raise ValueError("GitHub identity verifier unavailable")
            verified_identity = self._github_identity_verifier.verify(identity_proof)
            expected_decision = build_join_decision_payload(
                invite=invite,
                identity=identity,
                material=material,
                credential=credential,
                identity_proof=identity_proof,
                pre_assertion_sign_count=response.webauthn_pre_assertion_sign_count,
                challenge=b"placeholder-challenge",
                now=response.webauthn_decision.payload.issued_at,
            ).model_copy(update={"challenge": response.webauthn_decision.payload.challenge})
            identity_proof = b""
            if (
                proof_digest != response.github_identity_proof_digest
                or verified_identity != identity
                or self._identity_lookup is None
                or self._identity_lookup.lookup(response.github_account_id) != identity
            ):
                raise ValueError("GitHub identity proof changed")
            decision = response.webauthn_decision
            if (
                decision.payload != expected_decision
                or decision.verified_at < decision.payload.issued_at
                or decision.verified_at > decision.payload.expires_at
                or response.created_at != decision.payload.issued_at
                or response.expires_at != decision.payload.expires_at
                or response.created_at < decision.verified_at
                or decision.credential != credential
            ):
                raise ValueError("join WebAuthn binding changed")
            verified = self._verify_webauthn_evidence(
                assertion=_decode_variable(response.webauthn_assertion, 16 * 1024),
                decision=decision,
                pre_assertion_sign_count=response.webauthn_pre_assertion_sign_count,
                now=now,
            )
            if verified != decision:
                raise ValueError("join WebAuthn evidence changed")
            if sponsor.member_id != invite.sponsor_member_id:
                raise ValueError("active sponsor required")
            if (
                len(current.authority.members) >= current.authority.policy.max_active_members
                or len(current.authority.device_certificates)
                >= current.authority.policy.max_active_devices
                or len(current.authority.active_recipient_key_ids()) >= 64
                or any(
                    member.github_account_id == response.github_account_id
                    for member in current.authority.members
                )
                or any(
                    certificate.claims.device_id == response.device_id
                    or certificate.claims.recipient_key_id == response.recipient_key_id
                    or certificate.claims.signature_id == response.signature_id
                    for certificate in current.authority.device_certificates
                )
            ):
                raise ValueError("duplicate or capacity exceeded")
            certificate, after = self._build_transition(
                invite=invite,
                current=current,
                identity=identity,
                material=material,
                credential=credential,
                issued_at=response.created_at,
            )
            if (
                certificate.claims != claims
                or next(item for item in after.members if item.member_id == member.member_id)
                != member
            ):
                raise ValueError("join transition changed")
            before = authority_digest(current.authority)
            return JoinApprovalPreviewV2(
                invite=invite,
                response=response,
                response_digest=_digest(export_join_response(response)),
                authority_before_digest=before,
                authority_after_digest=authority_digest(after),
                certificate=certificate,
                authority_after=after,
                base_state_commit=current.state_commit,
                base_bundle_digest=current.bundle_digest,
                default_branch_commit=current.default_branch_commit,
                tooling_digest=current.tooling_digest,
            )
        except BaseException as error:  # noqa: BLE001
            message: Literal["team enrollment unavailable", "team enrollment changed"] = (
                "team enrollment changed"
                if isinstance(error, TeamEnrollmentError)
                and str(error) == "team enrollment changed"
                else "team enrollment unavailable"
            )
            self._raise_public(error, message)
        finally:
            identity_proof = b""

    def _approve_unsafe(
        self,
        *,
        preview: JoinApprovalPreviewV2,
        sponsor_decision: VerifiedHumanDecision,
        sponsor_pre_assertion_sign_count: int,
        sponsor_assertion: bytes,
        current: VerifiedRemoteStateV2,
        now: datetime,
        persist_approval: Callable[[ApprovedAuthorityTransitionV2], None] | None = None,
        approval_request_digest: str | None = None,
    ) -> ApprovedAuthorityTransitionV2:
        try:
            now = _utc_second(now)
            if (
                now > preview.invite.expires_at
                or now > preview.response.expires_at
                or now >= preview.response.webauthn_decision.payload.expires_at
                or now >= sponsor_decision.payload.expires_at
            ):
                raise ValueError("approval expired")
            recomputed = self._preview_approval_unsafe(
                invite=preview.invite,
                response=preview.response,
                current=current,
                now=preview.certificate.claims.issued_at,
            )
            if (
                recomputed != preview
                or self._replay_store.is_consumed(preview.invite.invite_id)
                or not self._state_matches_invite(current, preview.invite)
                or preview.base_state_commit != current.state_commit
                or preview.base_bundle_digest != current.bundle_digest
                or preview.default_branch_commit != current.default_branch_commit
                or preview.tooling_digest != current.tooling_digest
                or preview.authority_before_digest != authority_digest(current.authority)
                or preview.authority_after_digest != authority_digest(preview.authority_after)
                or type(sponsor_decision) is not VerifiedHumanDecision
                or (persist_approval is not None and not callable(persist_approval))
            ):
                raise ValueError("approval changed")
            credential = sponsor_decision.credential
            expected = build_sponsor_decision_payload(
                preview=preview,
                credential=credential,
                challenge=b"placeholder-challenge",
                now=sponsor_decision.payload.issued_at,
                approval_request_digest=approval_request_digest,
            ).model_copy(update={"challenge": sponsor_decision.payload.challenge})
            sponsor = next(
                member
                for member in current.authority.members
                if member.member_id == preview.invite.sponsor_member_id
            )
            active_sponsor, active_certificate = self._active_sponsor(
                current.authority, preview.invite.sponsor_certificate.certificate_id, now
            )
            live_sponsor = (
                None
                if self._identity_lookup is None
                else self._identity_lookup.lookup(sponsor.github_account_id)
            )
            if (
                sponsor_decision.payload != expected
                or sponsor_decision.verified_at < sponsor_decision.payload.issued_at
                or sponsor_decision.verified_at > sponsor_decision.payload.expires_at
                or now < sponsor_decision.verified_at
                or now > sponsor_decision.payload.expires_at
                or credential.local_only
                or credential.project_id != preview.invite.project_id
                or credential.repository_id != _decision_repository_id(preview.invite.repository_id)
                or credential.actor != sponsor.actor
                or credential.github_account_id != str(sponsor.github_account_id)
                or credential.github_login != sponsor.github_login
                or live_sponsor is None
                or live_sponsor.account_id != str(sponsor.github_account_id)
                or live_sponsor.login != sponsor.github_login
                or active_sponsor != sponsor
                or active_certificate != preview.invite.sponsor_certificate
                or _credential_digest(credential)
                != active_certificate.claims.webauthn_credential_digest
                or sponsor.role != "sponsor"
                or sponsor.status != "active"
                or self._root_store is None
            ):
                raise ValueError("sponsor decision changed")
            self._verify_webauthn_evidence(
                assertion=sponsor_assertion,
                decision=sponsor_decision,
                pre_assertion_sign_count=sponsor_pre_assertion_sign_count,
                now=now,
            )
            decision_digest = _digest(sponsor_decision.payload.canonical_bytes())
            attestation = AuthorityAttestationV2(
                project_id=preview.invite.project_id,
                repository_id=preview.invite.repository_id,
                authority_epoch=preview.invite.root.authority_epoch,
                previous_authority_digest=preview.authority_before_digest,
                authority_digest=preview.authority_after_digest,
                parent_bundle_digest=preview.base_bundle_digest,
                operation="enroll",
                subject_digest=preview.response_digest,
                sponsor_member_id=sponsor.member_id,
                sponsor_device_certificate_id=preview.invite.sponsor_certificate.certificate_id,
                sponsor_decision_digest=decision_digest,
                decided_at=now,
                root_key_id=preview.invite.root.root_key_id,
                root_signature=_b64(b"\0" * 64),
            )
            assert self._root_store is not None
            signature = self._root_store.sign(
                preview.invite.root.root_key_id,
                canonical_authority_attestation_preimage(attestation),
            )
            attestation = attestation.model_copy(update={"root_signature": _b64(signature)})
            result = ApprovedAuthorityTransitionV2(
                approval=SponsorJoinApprovalV2(
                    invite_id=preview.invite.invite_id,
                    join_response_digest=preview.response_digest,
                    authority_before_digest=preview.authority_before_digest,
                    authority_after_digest=preview.authority_after_digest,
                    certificate_id=preview.certificate.certificate_id,
                    sponsor_member_id=sponsor.member_id,
                    sponsor_decision_digest=decision_digest,
                ),
                certificate=preview.certificate,
                authority=preview.authority_after,
                attestation=attestation,
            )
            if persist_approval is not None:
                persist_approval(result)
            self._replay_store.consume(preview.invite.invite_id)
            self._challenge_store.delete(preview.invite.invite_id)
            return result
        except BaseException as error:  # noqa: BLE001
            self._raise_public(error, "team enrollment unavailable")

    def plan_publication(
        self,
        *,
        snapshot: CanonicalStateSnapshot,
        parent: VerifiedReleaseV2,
        preview: JoinApprovalPreviewV2,
        now: datetime,
    ) -> EnrollmentPublicationPlanV2:
        """Create the exact unsigned encrypted artifacts shown before sponsor approval."""
        try:
            if (
                type(preview) is not JoinApprovalPreviewV2
                or preview.authority_before_digest != authority_digest(parent.authority)
                or preview.authority_after_digest != authority_digest(preview.authority_after)
                or preview.base_state_commit != parent.commit
                or preview.base_bundle_digest != parent.manifest.bundle_digest
            ):
                raise ValueError("enrollment publication plan changed")
            return self._publication_plan_unsafe(
                snapshot=snapshot,
                parent=parent,
                after=preview.authority_after,
                now=now,
            )
        except BaseException as error:  # noqa: BLE001 - fixed public crypto boundary
            failure = _prepared_failure(error, "team enrollment unavailable")
            del self, snapshot, parent, preview, now, error
            raise failure.with_traceback(None) from None

    def _publication_plan_unsafe(
        self,
        *,
        snapshot: CanonicalStateSnapshot,
        parent: VerifiedReleaseV2,
        after: TeamAuthorityRegistryV2,
        now: datetime,
    ) -> EnrollmentPublicationPlanV2:
        snapshot = CanonicalStateSnapshot.model_validate(snapshot.model_dump(mode="python"))
        parent = replace(parent)
        after = TeamAuthorityRegistryV2.model_validate(after.model_dump(mode="python"))
        now = _utc_second(now)
        before = parent.authority
        if (
            parent.snapshot is not None
            and parent.snapshot != snapshot
            or snapshot.project_id != before.project_id
            or snapshot.repository_id != before.repository_id
            or snapshot.graph_version < parent.manifest.graph_version
            or after.root != before.root
            or after.sequence != before.sequence + 1
            or after.previous_authority_digest != authority_digest(before)
            or after.ci_recipient != before.ci_recipient
        ):
            raise ValueError("enrollment publication plan changed")
        recipient_ids = after.active_recipient_key_ids()
        public_keys = {
            item.claims.recipient_key_id: _decode(item.claims.recipient_public_key, 32)
            for item in after.device_certificates
            if item.claims.recipient_key_id in recipient_ids
        }
        public_keys[after.ci_recipient.key_id] = _decode(after.ci_recipient.public_key, 32)
        public_keys = dict(sorted(public_keys.items()))
        if tuple(public_keys) != recipient_ids:
            raise ValueError("enrollment recipients changed")
        archive = build_archive_v2(snapshot, after)
        context = AuthenticatedBundleContextV2(
            project_id=snapshot.project_id,
            repository_id=snapshot.repository_id,
            graph_version=snapshot.graph_version,
            parent_bundle_digest=parent.manifest.bundle_digest,
            recipient_key_ids=recipient_ids,
            authority_digest=authority_digest(after),
            authority_epoch=after.authority_epoch,
            authority_sequence=after.sequence,
            root_key_id=after.root.root_key_id,
            created_at=now,
        )
        bundle = canonical_encrypted_bundle_bytes(
            _encrypt_bundle_for_public_keys(
                archive,
                public_keys,
                canonical_authenticated_context_bytes(context),
            )
        )
        manifest = TeamStateManifestV2(
            project_id=snapshot.project_id,
            repository_id=snapshot.repository_id,
            graph_version=snapshot.graph_version,
            parent_bundle_digest=parent.manifest.bundle_digest,
            bundle_digest=_digest(bundle),
            bundle_size=len(bundle),
            recipient_key_ids=recipient_ids,
            authority_digest=authority_digest(after),
            authority_epoch=after.authority_epoch,
            root_key_id=after.root.root_key_id,
            created_at=now,
        )
        _validate_v2_snapshot(snapshot, manifest)
        digest_hex = manifest.bundle_digest.removeprefix("sha256:")
        release = f"{manifest.graph_version}-{digest_hex}"
        return EnrollmentPublicationPlanV2(
            manifest=manifest,
            authority=after,
            bundle=_b64(bundle),
            branch=f"intent-publication/{digest_hex}",
            bundle_path=f"bundles/{release}.intent",
            signature_path=f"signatures/{release}.json",
            snapshot_digest=_digest(_canonical(snapshot.model_dump(mode="json"))),
            parent_manifest_digest=_digest(parent.manifest_bytes),
            parent_commit=parent.commit,
        )

    def prepare_publication(
        self,
        *,
        snapshot: CanonicalStateSnapshot,
        parent: VerifiedReleaseV2,
        transition: ApprovedAuthorityTransitionV2,
        now: datetime,
        plan: EnrollmentPublicationPlanV2 | None = None,
    ) -> PreparedEnrollmentPublicationV2:
        """Build the exact authority-changing release approved by the sponsor ceremony."""
        result: PreparedEnrollmentPublicationV2 | None = None
        failure: BaseException | None = None
        signature = b""
        try:
            snapshot = CanonicalStateSnapshot.model_validate(snapshot.model_dump(mode="python"))
            if (
                type(parent) is not VerifiedReleaseV2
                or type(transition) is not ApprovedAuthorityTransitionV2
            ):
                raise ValueError("enrollment publication changed")
            transition = ApprovedAuthorityTransitionV2.model_validate(
                transition.model_dump(mode="python")
            )
            now = _utc_second(now)
            before = parent.authority
            after = transition.authority
            attestation = transition.attestation
            sponsor_certificate = next(
                (
                    item
                    for item in before.device_certificates
                    if item.certificate_id == attestation.sponsor_device_certificate_id
                ),
                None,
            )
            if (
                parent.snapshot is not None
                and parent.snapshot != snapshot
                or snapshot.project_id != before.project_id
                or snapshot.repository_id != before.repository_id
                or snapshot.graph_version < parent.manifest.graph_version
                or after.root != before.root
                or after.sequence != before.sequence + 1
                or after.previous_authority_digest != authority_digest(before)
                or after.ci_recipient != before.ci_recipient
                or transition.approval.authority_before_digest != authority_digest(before)
                or transition.approval.authority_after_digest != authority_digest(after)
                or transition.approval.join_response_digest != attestation.subject_digest
                or transition.approval.sponsor_member_id != attestation.sponsor_member_id
                or transition.approval.sponsor_decision_digest
                != attestation.sponsor_decision_digest
                or transition.certificate not in after.device_certificates
                or sponsor_certificate is None
                or attestation.operation != "enroll"
                or attestation.parent_bundle_digest != parent.manifest.bundle_digest
                or attestation.decided_at != now
            ):
                raise ValueError("enrollment publication changed")
            if plan is None:
                plan = self._publication_plan_unsafe(
                    snapshot=snapshot,
                    parent=parent,
                    after=after,
                    now=now,
                )
            else:
                plan = EnrollmentPublicationPlanV2.model_validate(plan.model_dump(mode="python"))
                if (
                    plan.authority != after
                    or plan.snapshot_digest != _digest(_canonical(snapshot.model_dump(mode="json")))
                    or plan.parent_manifest_digest != _digest(parent.manifest_bytes)
                    or plan.parent_commit != parent.commit
                    or plan.manifest.parent_bundle_digest != parent.manifest.bundle_digest
                    or plan.manifest.created_at != now
                ):
                    raise ValueError("enrollment publication plan changed")
            manifest = plan.manifest
            bundle = plan.bundle_bytes()
            signature = self._device_store.sign(
                sponsor_certificate.claims.signature_id,
                canonical_state_signature_preimage(manifest),
            )
            envelope = StateSignatureEnvelopeV2(
                manifest_digest=_digest(manifest.canonical_bytes()),
                bundle_digest=manifest.bundle_digest,
                authority_digest=manifest.authority_digest,
                certificates=(sponsor_certificate,),
                signatures=(
                    CertifiedStateSignatureV2(
                        certificate_id=sponsor_certificate.certificate_id,
                        signature_id=sponsor_certificate.claims.signature_id,
                        signature=_b64(signature),
                    ),
                ),
                authority_attestation=attestation,
            )
            verify_v2_envelope(manifest, envelope, before.root, before, now)
            result = PreparedEnrollmentPublicationV2(
                repository_id=manifest.repository_id,
                branch=plan.branch,
                manifest=manifest,
                manifest_bytes=manifest.canonical_bytes(),
                bundle=bundle,
                envelope=envelope,
                signatures=envelope.canonical_bytes(),
                bundle_path=plan.bundle_path,
                signature_path=plan.signature_path,
                authority=after,
            )
        except BaseException as caught:  # noqa: BLE001 - public secret-bearing boundary
            caught_traceback = caught.__traceback__
            if caught_traceback is not None:
                traceback.clear_frames(caught_traceback)
            caught_traceback = None
            caught.args = ()
            caught.__dict__.clear()
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            failure = (
                caught
                if not isinstance(caught, Exception)
                else TeamEnrollmentError("team enrollment unavailable")
            )
        finally:
            signature = b""
            snapshot = None  # type: ignore[assignment]
            parent = None  # type: ignore[assignment]
            transition = None  # type: ignore[assignment]
            now = None  # type: ignore[assignment]
        if failure is not None:
            detached = failure
            failure = None
            raise detached.with_traceback(None) from None
        assert result is not None
        return result

    def transition_proof(
        self,
        *,
        preview: JoinApprovalPreviewV2,
        parent: VerifiedReleaseV2,
        publication: PreparedEnrollmentPublicationV2,
    ) -> EnrollmentTransitionProofV2:
        try:
            return self._transition_proof_unsafe(
                preview=preview,
                parent=parent,
                publication=publication,
            )
        except BaseException as error:  # noqa: BLE001 - preserve cancellation identity
            failure = _prepared_failure(error, "team enrollment unavailable")
            del self, preview, parent, publication, error
            raise failure.with_traceback(None) from None

    def _transition_proof_unsafe(
        self,
        *,
        preview: JoinApprovalPreviewV2,
        parent: VerifiedReleaseV2,
        publication: PreparedEnrollmentPublicationV2,
    ) -> EnrollmentTransitionProofV2:
        """Create the compact public proof B needs to authenticate the exact descendant."""
        signature = b""
        try:
            if (
                type(preview) is not JoinApprovalPreviewV2
                or type(parent) is not VerifiedReleaseV2
                or type(publication) is not PreparedEnrollmentPublicationV2
            ):
                raise ValueError("enrollment transition proof changed")
            before = parent.authority
            after = publication.authority
            attestation = publication.envelope.authority_attestation
            root_store = self._root_store
            if attestation is None or root_store is None:
                raise ValueError("enrollment transition proof unavailable")
            sponsor_member = next(
                member
                for member in before.members
                if member.member_id == preview.invite.sponsor_member_id
            )
            sponsor_certificate = next(
                certificate
                for certificate in before.device_certificates
                if certificate.certificate_id == preview.invite.sponsor_certificate.certificate_id
            )
            if (
                preview.authority_before_digest != authority_digest(before)
                or preview.authority_after_digest != authority_digest(after)
                or preview.authority_after != after
                or publication.manifest.parent_bundle_digest != parent.manifest.bundle_digest
            ):
                raise ValueError("enrollment transition proof changed")
            values = {
                "root": before.root,
                "authority_before_sequence": before.sequence,
                "authority_after_sequence": after.sequence,
                "authority_before_digest": authority_digest(before),
                "authority_after_digest": authority_digest(after),
                "parent_members_digest": _digest(
                    _canonical([item.model_dump(mode="json") for item in before.members])
                ),
                "parent_certificates_digest": _digest(
                    _canonical(
                        [item.model_dump(mode="json") for item in before.device_certificates]
                    )
                ),
                "parent_revocations_digest": _digest(
                    _canonical([item.model_dump(mode="json") for item in before.revocations])
                ),
                "parent_policy_digest": _digest(_canonical(before.policy.model_dump(mode="json"))),
                "parent_ci_recipient": before.ci_recipient,
                "sponsor_member": sponsor_member,
                "sponsor_certificate": sponsor_certificate,
                "invite_id": preview.invite.invite_id,
                "response_digest": preview.response_digest,
                "new_member": preview.response.proposed_member,
                "new_certificate": preview.certificate,
                "base_state_commit": preview.base_state_commit,
                "base_bundle_digest": preview.base_bundle_digest,
                "default_branch_commit": preview.default_branch_commit,
                "tooling_digest": preview.tooling_digest,
                "publication_manifest_digest": _digest(publication.manifest_bytes),
                "authority_attestation": attestation,
            }
            placeholder = EnrollmentTransitionProofV2.model_construct(
                **values,  # type: ignore[arg-type]
                root_signature=_b64(b"\0" * 64),
            )
            signature = root_store.sign(before.root.root_key_id, placeholder.signing_preimage())
            result = EnrollmentTransitionProofV2.model_validate(
                {**values, "root_signature": _b64(signature)}
            )
            Ed25519PublicKey.from_public_bytes(_decode(before.root.root_public_key, 32)).verify(
                signature, result.signing_preimage()
            )
            if len(result.canonical_bytes()) > 64 * 1024:
                raise ValueError("enrollment transition proof unavailable")
            return result
        except BaseException as error:  # noqa: BLE001 - public crypto boundary
            failure = _prepared_failure(error, "team enrollment unavailable")
            signature = b""
            del self, preview, parent, publication, error
            raise failure.with_traceback(None) from None

    def device_public_material(
        self, *, invite: TeamInviteV2, local_identity: GitHubIdentity
    ) -> DevicePublicMaterial:
        try:
            return self._device_public_material_unsafe(invite=invite, local_identity=local_identity)
        except BaseException as error:  # noqa: BLE001
            failure = _prepared_failure(error, "team enrollment unavailable")
            del self, invite, local_identity, error
            raise failure.with_traceback(None) from None

    def create_invite(
        self,
        *,
        state: VerifiedRemoteStateV2,
        intended_identity: GitHubIdentity,
        now: datetime,
    ) -> TeamInviteV2:
        try:
            return self._create_invite_unsafe(
                state=state, intended_identity=intended_identity, now=now
            )
        except BaseException as error:  # noqa: BLE001
            failure = _prepared_failure(error, "team enrollment unavailable")
            del self, state, intended_identity, now, error
            raise failure.with_traceback(None) from None

    def create_join_response(
        self,
        *,
        invite: TeamInviteV2,
        local_identity: GitHubIdentity,
        decision: VerifiedHumanDecision,
        identity_proof: bytes,
        pre_assertion_sign_count: int,
        webauthn_assertion: bytes,
        now: datetime,
    ) -> JoinResponseV2:
        try:
            return self._create_join_response_unsafe(
                invite=invite,
                local_identity=local_identity,
                decision=decision,
                identity_proof=identity_proof,
                pre_assertion_sign_count=pre_assertion_sign_count,
                webauthn_assertion=webauthn_assertion,
                now=now,
            )
        except BaseException as error:  # noqa: BLE001
            failure = _prepared_failure(error, "team enrollment unavailable")
            del self, invite, local_identity, decision, identity_proof
            del pre_assertion_sign_count, webauthn_assertion, now, error
            raise failure.with_traceback(None) from None

    def preview_approval(
        self,
        *,
        invite: TeamInviteV2,
        response: JoinResponseV2,
        current: VerifiedRemoteStateV2,
        now: datetime,
    ) -> JoinApprovalPreviewV2:
        try:
            return self._preview_approval_unsafe(
                invite=invite, response=response, current=current, now=now
            )
        except BaseException as error:  # noqa: BLE001
            changed = isinstance(error, TeamEnrollmentError) and str(error) == (
                "team enrollment changed"
            )
            failure = _prepared_failure(
                error,
                "team enrollment changed" if changed else "team enrollment unavailable",
            )
            del self, invite, response, current, now, error, changed
            raise failure.with_traceback(None) from None

    def approve(
        self,
        *,
        preview: JoinApprovalPreviewV2,
        sponsor_decision: VerifiedHumanDecision,
        sponsor_pre_assertion_sign_count: int,
        sponsor_assertion: bytes,
        current: VerifiedRemoteStateV2,
        now: datetime,
        persist_approval: Callable[[ApprovedAuthorityTransitionV2], None] | None = None,
        approval_request_digest: str | None = None,
    ) -> ApprovedAuthorityTransitionV2:
        try:
            return self._approve_unsafe(
                preview=preview,
                sponsor_decision=sponsor_decision,
                sponsor_pre_assertion_sign_count=sponsor_pre_assertion_sign_count,
                sponsor_assertion=sponsor_assertion,
                current=current,
                now=now,
                persist_approval=persist_approval,
                approval_request_digest=approval_request_digest,
            )
        except BaseException as error:  # noqa: BLE001
            failure = _prepared_failure(error, "team enrollment unavailable")
            del self, preview, sponsor_decision, sponsor_pre_assertion_sign_count
            del sponsor_assertion, current, now, persist_approval, approval_request_digest, error
            raise failure.with_traceback(None) from None


__all__ = [
    "ApprovedAuthorityTransitionV2",
    "EnrollmentChallengeKeyStore",
    "EnrollmentPublicationPlanV2",
    "EnrollmentReplayStateStore",
    "EnrollmentTransitionProofV2",
    "InMemoryEnrollmentChallengeKeyStore",
    "InMemoryEnrollmentReplayStateStore",
    "JoinApprovalPreviewV2",
    "JoinResponseV2",
    "PreparedEnrollmentPublicationV2",
    "SponsorJoinApprovalV2",
    "TeamEnrollmentError",
    "TeamEnrollmentService",
    "TeamInviteV2",
    "VerifiedRemoteStateV2",
    "authenticate_enrollment_publication",
    "build_join_decision_payload",
    "build_sponsor_decision_payload",
    "enrollment_transition_plan_digest",
    "export_join_response",
    "export_team_invite",
    "parse_join_response",
    "parse_team_invite",
]
