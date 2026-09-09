"""Bounded public exchange for sponsor-approved version-two team enrollment."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Never, Protocol

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
from intent_engineering.team_state.authority import (
    authority_digest,
    canonical_authority_attestation_preimage,
    canonical_certificate_signing_preimage,
    derive_certificate_id,
    derive_member_id,
    issue_device_certificate,
)
from intent_engineering.team_state.crypto import verify_recipient_possession_proof
from intent_engineering.team_state.keys import (
    DeviceEnrollmentBinding,
    DeviceKeyStore,
    DevicePublicMaterial,
    GitHubIdentity,
    GitHubIdentityVerifier,
)
from intent_engineering.team_state.models import (
    AuthorityAttestationV2,
    DeviceCertificateClaimsV2,
    DeviceSignerCertificateV2,
    MemberRecordV2,
    TeamAuthorityRegistryV2,
    TeamRootTrustV2,
)
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
        self, message: Literal["team enrollment unavailable", "team enrollment changed"]
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
    message: Literal["team enrollment unavailable", "team enrollment changed"],
) -> BaseException:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    error.args = ()
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


class ApprovedAuthorityTransitionV2(_EnrollmentModel):
    approval: SponsorJoinApprovalV2
    certificate: DeviceSignerCertificateV2
    authority: TeamAuthorityRegistryV2
    attestation: AuthorityAttestationV2


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
) -> HumanDecisionPayload:
    now = _utc_second(now)
    if type(challenge) is not bytes or len(challenge) < 16:
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
        subject_digest=_digest(preview.canonical_bytes()),
        result_digest=preview.authority_after_digest,
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
            ):
                raise ValueError("approval changed")
            credential = sponsor_decision.credential
            expected = build_sponsor_decision_payload(
                preview=preview,
                credential=credential,
                challenge=b"placeholder-challenge",
                now=sponsor_decision.payload.issued_at,
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
            self._replay_store.consume(preview.invite.invite_id)
            self._challenge_store.delete(preview.invite.invite_id)
            return result
        except BaseException as error:  # noqa: BLE001
            self._raise_public(error, "team enrollment unavailable")

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
    ) -> ApprovedAuthorityTransitionV2:
        try:
            return self._approve_unsafe(
                preview=preview,
                sponsor_decision=sponsor_decision,
                sponsor_pre_assertion_sign_count=sponsor_pre_assertion_sign_count,
                sponsor_assertion=sponsor_assertion,
                current=current,
                now=now,
            )
        except BaseException as error:  # noqa: BLE001
            failure = _prepared_failure(error, "team enrollment unavailable")
            del self, preview, sponsor_decision, sponsor_pre_assertion_sign_count
            del sponsor_assertion, current, now, error
            raise failure.with_traceback(None) from None


__all__ = [
    "ApprovedAuthorityTransitionV2",
    "EnrollmentChallengeKeyStore",
    "EnrollmentReplayStateStore",
    "InMemoryEnrollmentChallengeKeyStore",
    "InMemoryEnrollmentReplayStateStore",
    "JoinApprovalPreviewV2",
    "JoinResponseV2",
    "SponsorJoinApprovalV2",
    "TeamEnrollmentError",
    "TeamEnrollmentService",
    "TeamInviteV2",
    "VerifiedRemoteStateV2",
    "build_join_decision_payload",
    "build_sponsor_decision_payload",
    "export_join_response",
    "export_team_invite",
    "parse_join_response",
    "parse_team_invite",
]
