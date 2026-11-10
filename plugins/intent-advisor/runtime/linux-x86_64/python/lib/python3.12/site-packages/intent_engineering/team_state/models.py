"""Provider-neutral, canonical contracts for shared approved intent state."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, ClassVar, Final, Literal, Self

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage.jsonl.strict import loads_strict_object

ENCRYPTION_ALGORITHM: Final = "x25519-hkdf-sha256-aes256gcm-v1"
DECISION_ALGORITHM: Final = "webauthn-decision-v1"
STATE_REF: Final = "refs/remotes/origin/intent-state"
MAX_MANIFEST_BYTES: Final = 64 * 1024
MAX_SIGNATURE_BYTES: Final = 64 * 1024
MAX_BUNDLE_BYTES: Final = 24 * 1024 * 1024
MAX_STATE_BYTES: Final = 16 * 1024 * 1024
MAX_FILE_BYTES: Final = 8 * 1024 * 1024
MAX_STATE_FILES: Final = 32
MAX_AUTHORITY_BYTES: Final = 256 * 1024
MAX_AUTHORITY_MEMBERS: Final = 32
MAX_AUTHORITY_DEVICES: Final = 63
MAX_AUTHORITY_REVOCATIONS: Final = 256
MAX_RELEASE_SIGNATURES: Final = 8

CANONICAL_STATE_PATHS: Final = (
    "approvals/approvals.jsonl",
    "approvals/plans.jsonl",
    "approvals/policy.yaml",
    "approvals/receipts.jsonl",
    "config.yaml",
    "evidence/evidence.jsonl",
    "graph.yaml",
    "history/changesets.jsonl",
    "history/intent-proposals.jsonl",
    "reconciliation/cases.jsonl",
)

_REQUIRED_PATHS = frozenset(CANONICAL_STATE_PATHS)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPOSITORY_ID = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_PUBLICATION_BRANCH = re.compile(r"^intent-publication/[0-9a-f]{64}$")
_GITHUB_ACCOUNT_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_GITHUB_LOGIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_ROOT_KEY_ID = re.compile(r"^root:sha256:[0-9a-f]{64}$")
_MEMBER_ID = re.compile(r"^member:sha256:[0-9a-f]{64}$")
_RECIPIENT_KEY_ID = re.compile(r"^recipient:sha256:[0-9a-f]{64}$")
_CI_RECIPIENT_KEY_ID = re.compile(r"^recipient:ci:sha256:[0-9a-f]{64}$")
_SIGNATURE_ID = re.compile(r"^signer:sha256:[0-9a-f]{64}$")
_CERTIFICATE_ID = re.compile(r"^certificate:sha256:[0-9a-f]{64}$")
_DEVICE_ID = re.compile(r"^device:[0-9a-f]{32}$")
_MAX_WEBAUTHN_CREDENTIAL_ID = 1024
_MAX_WEBAUTHN_PUBLIC_KEY = 4096


class _TeamStateModel(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        strict=True,
        validate_default=True,
    )

    @field_validator("repository_id", check_fields=False)
    @classmethod
    def require_canonical_repository_id(cls, value: str) -> str:
        if _REPOSITORY_ID.fullmatch(value) is None:
            raise ValueError("invalid team-state repository identity")
        _host, owner, repository = value.split("/")
        if owner in {".", ".."} or repository in {".", ".."} or repository.endswith(".git"):
            raise ValueError("invalid team-state repository identity")
        return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _json_tuple(value: object, info: ValidationInfo) -> object:
    if info.mode == "json" and type(value) is list:
        return tuple(value)
    if info.mode == "python" and type(value) is not tuple:
        raise ValueError("team-state collections must be tuples")
    return value


def _require_sorted_unique_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError("team-state identifiers must be nonempty, sorted, and unique")
    if any(_KEY_ID.fullmatch(value) is None for value in values):
        raise ValueError("invalid team-state identifier")
    return values


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("team-state time must be UTC")
    return value.astimezone(UTC)


def _require_utc_second(value: datetime) -> datetime:
    value = _require_utc(value)
    if value.microsecond != 0:
        raise ValueError("team-state time must use whole seconds")
    return value


def _decode_base64url(
    value: str,
    *,
    label: str,
    maximum_encoded: int,
    exact_decoded: int | None = None,
    minimum_decoded: int = 1,
) -> bytes:
    if not value or len(value) > maximum_encoded or _BASE64URL.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"invalid {label}") from error
    encoded = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if (
        encoded != value
        or len(decoded) < minimum_decoded
        or (exact_decoded is not None and len(decoded) != exact_decoded)
    ):
        raise ValueError(f"invalid {label}")
    return decoded


def _scoped_digest(domain: str, *parts: bytes) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + b"\0".join(parts)).hexdigest()


def _derived_root_key_id(project_id: str, repository_id: str, public_key: str) -> str:
    raw = _decode_base64url(
        public_key, label="root public key", maximum_encoded=64, exact_decoded=32
    )
    return "root:sha256:" + _scoped_digest(
        "intent.team-root.v2", project_id.encode(), repository_id.encode(), raw
    )


def _derived_member_id(project_id: str, repository_id: str, github_account_id: int) -> str:
    return "member:sha256:" + _scoped_digest(
        "intent.team-member.v2",
        project_id.encode(),
        repository_id.encode(),
        str(github_account_id).encode("ascii"),
    )


def _derived_recipient_key_id(project_id: str, repository_id: str, public_key: str) -> str:
    raw = _decode_base64url(
        public_key, label="recipient public key", maximum_encoded=64, exact_decoded=32
    )
    return "recipient:sha256:" + _scoped_digest(
        "intent.team-device-recipient.v2", project_id.encode(), repository_id.encode(), raw
    )


def _derived_signature_id(project_id: str, repository_id: str, public_key: str) -> str:
    raw = _decode_base64url(
        public_key, label="signing public key", maximum_encoded=64, exact_decoded=32
    )
    return "signer:sha256:" + _scoped_digest(
        "intent.team-device-signing.v2", project_id.encode(), repository_id.encode(), raw
    )


class _CanonicalWireModel(_TeamStateModel):
    _wire_maximum: ClassVar[int]
    _wire_label: ClassVar[str] = "team-state object"

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Any | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        if isinstance(json_data, str):
            content = json_data.encode("utf-8")
        elif isinstance(json_data, (bytes, bytearray)):
            content = bytes(json_data)
        else:
            raise TypeError(f"{cls._wire_label} JSON must be bytes or text")
        if not content or len(content) > cls._wire_maximum:
            raise ValueError(f"invalid {cls._wire_label}")
        try:
            loads_strict_object(content.decode("utf-8"))
        except (TypeError, UnicodeError, ValueError) as error:
            raise ValueError(f"invalid {cls._wire_label}") from error
        parsed = super().model_validate_json(
            content,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )
        if _canonical_json(parsed.model_dump(mode="json")) != content:
            raise ValueError(f"noncanonical {cls._wire_label}")
        return parsed

    def canonical_bytes(self) -> bytes:
        validated = type(self).model_validate(self.model_dump(mode="python"))
        content = _canonical_json(validated.model_dump(mode="json"))
        if not content or len(content) > type(self)._wire_maximum:
            raise ValueError(f"{type(self)._wire_label} is oversized")
        return content


class TeamStateManifest(_TeamStateModel):
    """The bounded, readable metadata stored on the protected state ref.

    This is the schema-version-1 wire type historically named
    ``SharedStateManifest`` by the restore implementation.
    """

    schema_version: Literal[1] = 1
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    graph_version: Annotated[int, Field(ge=1)]
    parent_bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)] | None = None
    bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    bundle_size: Annotated[int, Field(gt=0, le=MAX_BUNDLE_BYTES)]
    encryption_algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"] = ENCRYPTION_ALGORITHM
    recipient_key_ids: Annotated[tuple[str, ...], Field(max_length=64)]
    required_signature_ids: Annotated[tuple[str, ...], Field(max_length=64)]
    created_at: datetime

    _wire_maximum: ClassVar[int] = MAX_MANIFEST_BYTES

    @field_validator("schema_version", "graph_version", "bundle_size", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid team-state integer")
        return value

    @field_validator("recipient_key_ids", "required_signature_ids", mode="before")
    @classmethod
    def require_json_lists(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @field_validator("recipient_key_ids", "required_signature_ids")
    @classmethod
    def require_sorted_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_sorted_unique_ids(values)

    @field_validator("created_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def reject_self_parent(self) -> TeamStateManifest:
        if self.parent_bundle_digest == self.bundle_digest:
            raise ValueError("team-state manifest cannot be its own parent")
        return self

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Any | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Accept exactly one canonical JSON byte form and no duplicate keys."""
        if isinstance(json_data, str):
            content = json_data.encode("utf-8")
        elif isinstance(json_data, (bytes, bytearray)):
            content = bytes(json_data)
        else:
            raise TypeError("manifest JSON must be bytes or text")
        if not content or len(content) > cls._wire_maximum:
            raise ValueError("invalid team-state manifest")
        try:
            loads_strict_object(content.decode("utf-8"))
        except (TypeError, UnicodeError, ValueError) as error:
            raise ValueError("invalid team-state manifest") from error
        parsed = super().model_validate_json(
            content,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )
        if canonical_manifest_bytes(parsed) != content:
            raise ValueError("noncanonical team-state manifest")
        return parsed


class V1MigrationBinding(_TeamStateModel):
    """Exact legacy manifest and signer policy bridged by the first v2 release."""

    prior_manifest_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    legacy_signature_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=64)]

    @field_validator("legacy_signature_ids", mode="before")
    @classmethod
    def require_signature_tuple(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @field_validator("legacy_signature_ids")
    @classmethod
    def require_sorted_signatures(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_sorted_unique_ids(values)


class TeamStateManifestV2(_CanonicalWireModel):
    """Stable-root manifest for a version-two encrypted authority archive."""

    schema_version: Literal[2] = 2
    archive_version: Literal[2] = 2
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    graph_version: Annotated[int, Field(ge=1)]
    parent_bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)] | None = None
    bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    bundle_size: Annotated[int, Field(gt=0, le=MAX_BUNDLE_BYTES)]
    encryption_algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"] = ENCRYPTION_ALGORITHM
    recipient_key_ids: Annotated[tuple[str, ...], Field(min_length=2, max_length=64)]
    signing_policy: Literal["root-certified-device-threshold-v1"] = (
        "root-certified-device-threshold-v1"
    )
    authority_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    authority_epoch: Annotated[int, Field(ge=1, le=2**31 - 1)]
    root_key_id: Annotated[str, Field(pattern=_ROOT_KEY_ID.pattern)]
    created_at: datetime
    migration: V1MigrationBinding | None = None

    _wire_maximum: ClassVar[int] = MAX_MANIFEST_BYTES
    _wire_label: ClassVar[str] = "team-state manifest"

    @field_validator(
        "schema_version",
        "archive_version",
        "graph_version",
        "bundle_size",
        "authority_epoch",
        mode="before",
    )
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid team-state integer")
        return value

    @field_validator("recipient_key_ids", mode="before")
    @classmethod
    def require_recipient_tuple(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @field_validator("recipient_key_ids")
    @classmethod
    def require_sorted_recipients(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        values = _require_sorted_unique_ids(values)
        ci_ids = tuple(value for value in values if _CI_RECIPIENT_KEY_ID.fullmatch(value))
        human_ids = tuple(value for value in values if _RECIPIENT_KEY_ID.fullmatch(value))
        if len(ci_ids) != 1 or not human_ids or len(ci_ids) + len(human_ids) != len(values):
            raise ValueError("invalid version-two recipient policy")
        return values

    @field_validator("created_at")
    @classmethod
    def require_utc_second(cls, value: datetime) -> datetime:
        return _require_utc_second(value)

    @model_validator(mode="after")
    def require_lineage(self) -> TeamStateManifestV2:
        if self.parent_bundle_digest == self.bundle_digest:
            raise ValueError("team-state manifest cannot be its own parent")
        if self.migration is not None and self.parent_bundle_digest is None:
            raise ValueError("team-state migration requires a legacy parent")
        return self


class StateSignature(_TeamStateModel):
    """One schema-version-1 state signature, retained without wire changes."""

    signature_id: Annotated[str, Field(pattern=_KEY_ID.pattern)]
    algorithm: Literal["ed25519-v1"] = "ed25519-v1"
    signature: str

    @field_validator("signature")
    @classmethod
    def require_signature_bytes(cls, value: str) -> str:
        _decode_base64url(value, label="state signature", maximum_encoded=96, exact_decoded=64)
        return value


class StateSignatureEnvelope(_CanonicalWireModel):
    """Schema-version-1 signature envelope, retained without wire changes."""

    schema_version: Literal[1] = 1
    manifest_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    signatures: Annotated[tuple[StateSignature, ...], Field(min_length=1, max_length=64)]

    _wire_maximum: ClassVar[int] = MAX_SIGNATURE_BYTES
    _wire_label: ClassVar[str] = "team-state signature envelope"

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid team-state integer")
        return value

    @field_validator("signatures", mode="before")
    @classmethod
    def require_signature_tuple(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @model_validator(mode="after")
    def require_sorted_signatures(self) -> StateSignatureEnvelope:
        _require_sorted_unique_ids(tuple(item.signature_id for item in self.signatures))
        return self


class TeamRootTrustV2(_TeamStateModel):
    schema_version: Literal[2] = 2
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    authority_epoch: Annotated[int, Field(ge=1, le=2**31 - 1)]
    root_key_id: Annotated[str, Field(pattern=_ROOT_KEY_ID.pattern)]
    root_public_key: str
    created_at: datetime
    predecessor_root_key_id: Annotated[str, Field(pattern=_ROOT_KEY_ID.pattern)] | None = None

    @field_validator("schema_version", "authority_epoch", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid authority integer")
        return value

    @field_validator("created_at")
    @classmethod
    def require_utc_second(cls, value: datetime) -> datetime:
        return _require_utc_second(value)

    @model_validator(mode="after")
    def require_derived_root_id(self) -> TeamRootTrustV2:
        if self.root_key_id != _derived_root_key_id(
            self.project_id, self.repository_id, self.root_public_key
        ):
            raise ValueError("root key binding changed")
        if self.predecessor_root_key_id == self.root_key_id:
            raise ValueError("root cannot be its own predecessor")
        if (self.authority_epoch == 1) != (self.predecessor_root_key_id is None):
            raise ValueError("root epoch and predecessor disagree")
        return self


class MemberRecordV2(_TeamStateModel):
    member_id: Annotated[str, Field(pattern=_MEMBER_ID.pattern)]
    actor: Annotated[str, Field(pattern=_KEY_ID.pattern)]
    github_account_id: Annotated[int, Field(gt=0)]
    github_login: Annotated[str, Field(pattern=_GITHUB_LOGIN.pattern)]
    role: Literal["sponsor", "member"]
    status: Literal["active", "revoked"]
    device_certificate_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=8)]
    enrolled_at: datetime
    revoked_at: datetime | None = None

    @field_validator("github_account_id", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid GitHub account identity")
        return value

    @field_validator("device_certificate_ids", mode="before")
    @classmethod
    def require_certificate_tuple(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @field_validator("device_certificate_ids")
    @classmethod
    def require_sorted_certificates(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            values != tuple(sorted(values))
            or len(values) != len(set(values))
            or any(_CERTIFICATE_ID.fullmatch(value) is None for value in values)
        ):
            raise ValueError("invalid device certificate identifiers")
        return values

    @field_validator("github_login")
    @classmethod
    def require_login(cls, value: str) -> str:
        if "--" in value:
            raise ValueError("invalid canonical GitHub login")
        return value

    @field_validator("enrolled_at", "revoked_at")
    @classmethod
    def require_utc_second(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc_second(value)

    @model_validator(mode="after")
    def require_status_time(self) -> MemberRecordV2:
        if self.actor != f"github:{self.github_account_id}":
            raise ValueError("member actor binding changed")
        if (self.status == "revoked") != (self.revoked_at is not None):
            raise ValueError("member status and revocation time disagree")
        if self.revoked_at is not None and self.revoked_at < self.enrolled_at:
            raise ValueError("member revoked before enrollment")
        return self


class DeviceCertificateClaimsV2(_TeamStateModel):
    schema_version: Literal[2] = 2
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    authority_epoch: Annotated[int, Field(ge=1, le=2**31 - 1)]
    member_id: Annotated[str, Field(pattern=_MEMBER_ID.pattern)]
    device_id: Annotated[str, Field(pattern=_DEVICE_ID.pattern)]
    github_account_id: Annotated[int, Field(gt=0)]
    github_login: Annotated[str, Field(pattern=_GITHUB_LOGIN.pattern)]
    recipient_key_id: Annotated[str, Field(pattern=_RECIPIENT_KEY_ID.pattern)]
    recipient_public_key: str
    signature_id: Annotated[str, Field(pattern=_SIGNATURE_ID.pattern)]
    signing_public_key: str
    webauthn_credential_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    serial: Annotated[int, Field(gt=0)]
    issued_at: datetime
    expires_at: datetime

    @field_validator(
        "schema_version", "authority_epoch", "github_account_id", "serial", mode="before"
    )
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid certificate integer")
        return value

    @field_validator("github_login")
    @classmethod
    def require_login(cls, value: str) -> str:
        if "--" in value:
            raise ValueError("invalid canonical GitHub login")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_utc_second(cls, value: datetime) -> datetime:
        return _require_utc_second(value)

    @model_validator(mode="after")
    def require_key_and_identity_bindings(self) -> DeviceCertificateClaimsV2:
        if self.member_id != _derived_member_id(
            self.project_id, self.repository_id, self.github_account_id
        ):
            raise ValueError("member identity binding changed")
        if self.recipient_key_id != _derived_recipient_key_id(
            self.project_id, self.repository_id, self.recipient_public_key
        ):
            raise ValueError("recipient key binding changed")
        if self.signature_id != _derived_signature_id(
            self.project_id, self.repository_id, self.signing_public_key
        ):
            raise ValueError("signing key binding changed")
        if self.expires_at <= self.issued_at or self.expires_at > self.issued_at + timedelta(
            days=366
        ):
            raise ValueError("invalid certificate lifetime")
        return self


class DeviceSignerCertificateV2(_TeamStateModel):
    claims: DeviceCertificateClaimsV2
    certificate_id: Annotated[str, Field(pattern=_CERTIFICATE_ID.pattern)]
    algorithm: Literal["ed25519-root-certificate-v1"] = "ed25519-root-certificate-v1"
    root_key_id: Annotated[str, Field(pattern=_ROOT_KEY_ID.pattern)]
    root_signature: str

    @field_validator("root_signature")
    @classmethod
    def require_root_signature(cls, value: str) -> str:
        _decode_base64url(
            value, label="root certificate signature", maximum_encoded=96, exact_decoded=64
        )
        return value

    @model_validator(mode="after")
    def require_certificate_id(self) -> DeviceSignerCertificateV2:
        claims = DeviceCertificateClaimsV2.model_validate(self.claims.model_dump(mode="python"))
        expected = (
            "certificate:sha256:"
            + hashlib.sha256(
                b"intent.team-device-certificate.v2\0"
                + _canonical_json(claims.model_dump(mode="json"))
            ).hexdigest()
        )
        if self.certificate_id != expected:
            raise ValueError("device certificate binding changed")
        return self


class DeviceRevocationV2(_TeamStateModel):
    certificate_id: Annotated[str, Field(pattern=_CERTIFICATE_ID.pattern)]
    revoked_at: datetime
    reason: Literal["replaced", "lost", "compromised", "member-removed"]
    sponsor_member_id: Annotated[str, Field(pattern=_MEMBER_ID.pattern)]

    @field_validator("revoked_at")
    @classmethod
    def require_utc_second(cls, value: datetime) -> datetime:
        return _require_utc_second(value)


class TeamAuthorityPolicyV2(_TeamStateModel):
    ordinary_signature_threshold: Literal[1] = 1
    authority_change_sponsor_threshold: Literal[1] = 1
    max_active_members: Literal[32] = 32
    max_active_devices: Literal[63] = 63


class TeamAuthorityRegistryV2(_CanonicalWireModel):
    schema_version: Literal[2] = 2
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    authority_epoch: Annotated[int, Field(ge=1, le=2**31 - 1)]
    sequence: Annotated[int, Field(ge=1, le=2**63 - 1)]
    root: TeamRootTrustV2
    policy: TeamAuthorityPolicyV2
    members: Annotated[
        tuple[MemberRecordV2, ...], Field(min_length=1, max_length=MAX_AUTHORITY_MEMBERS)
    ]
    device_certificates: Annotated[
        tuple[DeviceSignerCertificateV2, ...],
        Field(min_length=1, max_length=MAX_AUTHORITY_DEVICES),
    ]
    revocations: Annotated[
        tuple[DeviceRevocationV2, ...], Field(max_length=MAX_AUTHORITY_REVOCATIONS)
    ]
    ci_recipient: CiRecipientRecord
    previous_authority_digest: Annotated[str, Field(pattern=_SHA256.pattern)] | None

    _wire_maximum: ClassVar[int] = MAX_AUTHORITY_BYTES
    _wire_label: ClassVar[str] = "team authority registry"

    @field_validator("schema_version", "authority_epoch", "sequence", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid authority integer")
        return value

    @field_validator("members", "device_certificates", "revocations", mode="before")
    @classmethod
    def require_tuples(cls, value: object, info: ValidationInfo) -> object:
        converted = _json_tuple(value, info)
        if info.mode != "json" or type(converted) is not tuple:
            return converted
        model: type[_TeamStateModel]
        if info.field_name == "members":
            model = MemberRecordV2
        elif info.field_name == "device_certificates":
            model = DeviceSignerCertificateV2
        else:
            model = DeviceRevocationV2
        return tuple(
            model.model_validate_json(_canonical_json(item)) if type(item) is dict else item
            for item in converted
        )

    @model_validator(mode="after")
    def require_consistent_graph(self) -> TeamAuthorityRegistryV2:
        root = TeamRootTrustV2.model_validate(self.root.model_dump(mode="python"))
        ci = CiRecipientRecord.model_validate(self.ci_recipient.model_dump(mode="python"))
        members = tuple(
            MemberRecordV2.model_validate(item.model_dump(mode="python")) for item in self.members
        )
        certificates = tuple(
            DeviceSignerCertificateV2.model_validate(item.model_dump(mode="python"))
            for item in self.device_certificates
        )
        revocations = tuple(
            DeviceRevocationV2.model_validate(item.model_dump(mode="python"))
            for item in self.revocations
        )
        if (
            root.project_id != self.project_id
            or root.repository_id != self.repository_id
            or root.authority_epoch != self.authority_epoch
            or ci.project_id != self.project_id
            or ci.repository_id != self.repository_id
        ):
            raise ValueError("authority scope changed")
        member_ids = tuple(item.member_id for item in members)
        certificate_ids = tuple(item.certificate_id for item in certificates)
        revocation_ids = tuple(item.certificate_id for item in revocations)
        if (
            member_ids != tuple(sorted(member_ids))
            or len(member_ids) != len(set(member_ids))
            or certificate_ids != tuple(sorted(certificate_ids))
            or len(certificate_ids) != len(set(certificate_ids))
            or revocation_ids != tuple(sorted(revocation_ids))
            or len(revocation_ids) != len(set(revocation_ids))
        ):
            raise ValueError("authority collections must be sorted and unique")
        members_by_id = {item.member_id: item for item in members}
        certificates_by_id = {item.certificate_id: item for item in certificates}
        if len({item.github_account_id for item in members}) != len(members):
            raise ValueError("duplicate team member identity")
        if len({item.actor for item in members}) != len(members):
            raise ValueError("duplicate team actor")
        if len({item.claims.serial for item in certificates}) != len(certificates):
            raise ValueError("duplicate certificate serial")
        if len({item.claims.device_id for item in certificates}) != len(certificates):
            raise ValueError("duplicate team device")
        if len({item.claims.recipient_key_id for item in certificates}) != len(certificates):
            raise ValueError("duplicate device recipient")
        if len({item.claims.signature_id for item in certificates}) != len(certificates):
            raise ValueError("duplicate device signer")
        revoked_ids = set(revocation_ids)
        for certificate in certificates:
            claim = certificate.claims
            member = members_by_id.get(claim.member_id)
            if (
                member is None
                or claim.project_id != self.project_id
                or claim.repository_id != self.repository_id
                or claim.authority_epoch != self.authority_epoch
                or certificate.root_key_id != root.root_key_id
                or member.github_account_id != claim.github_account_id
                or member.github_login != claim.github_login
                or certificate.certificate_id not in member.device_certificate_ids
            ):
                raise ValueError("device certificate is outside authority")
        for member in members:
            if member.member_id != _derived_member_id(
                self.project_id, self.repository_id, member.github_account_id
            ):
                raise ValueError("member identity binding changed")
            owned = tuple(
                certificate.certificate_id
                for certificate in certificates
                if certificate.claims.member_id == member.member_id
            )
            if owned != member.device_certificate_ids:
                raise ValueError("member certificate inventory changed")
            active = any(item not in revoked_ids for item in owned)
            if (member.status == "active") != active:
                raise ValueError("member and device status disagree")
        for revocation in revocations:
            revoked_certificate = certificates_by_id.get(revocation.certificate_id)
            sponsor = members_by_id.get(revocation.sponsor_member_id)
            if (
                revoked_certificate is None
                or sponsor is None
                or sponsor.role != "sponsor"
                or sponsor.status != "active"
            ):
                raise ValueError("invalid authority revocation")
            if revocation.revoked_at < revoked_certificate.claims.issued_at:
                raise ValueError("certificate revoked before issue")
        active_members = sum(item.status == "active" for item in members)
        active_devices = len(certificates) - len(revoked_ids)
        if (
            not any(item.status == "active" and item.role == "sponsor" for item in members)
            or active_members > self.policy.max_active_members
            or active_devices > self.policy.max_active_devices
            or active_devices + 1 > 64
            or ci.key_id in {item.claims.recipient_key_id for item in certificates}
        ):
            raise ValueError("authority recipient capacity exceeded")
        if (self.sequence == 1) != (self.previous_authority_digest is None):
            raise ValueError("authority sequence and predecessor disagree")
        return self

    def active_recipient_key_ids(self) -> tuple[str, ...]:
        """Derive the only recipient set an ordinary v2 publication may use."""
        active_members = {item.member_id for item in self.members if item.status == "active"}
        revoked = {item.certificate_id for item in self.revocations}
        human = (
            item.claims.recipient_key_id
            for item in self.device_certificates
            if item.claims.member_id in active_members and item.certificate_id not in revoked
        )
        return tuple(sorted((*human, self.ci_recipient.key_id)))


class CertifiedStateSignatureV2(_TeamStateModel):
    certificate_id: Annotated[str, Field(pattern=_CERTIFICATE_ID.pattern)]
    signature_id: Annotated[str, Field(pattern=_SIGNATURE_ID.pattern)]
    algorithm: Literal["ed25519-v1"] = "ed25519-v1"
    signature: str

    @field_validator("signature")
    @classmethod
    def require_signature_bytes(cls, value: str) -> str:
        _decode_base64url(value, label="state signature", maximum_encoded=96, exact_decoded=64)
        return value


class AuthorityAttestationV2(_TeamStateModel):
    schema_version: Literal[2] = 2
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    authority_epoch: Annotated[int, Field(ge=1, le=2**31 - 1)]
    previous_authority_digest: Annotated[str, Field(pattern=_SHA256.pattern)] | None
    authority_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    parent_bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)] | None
    operation: Literal["v1-migration", "enroll", "revoke", "promote", "root-rotate"]
    subject_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    sponsor_member_id: Annotated[str, Field(pattern=_MEMBER_ID.pattern)]
    sponsor_device_certificate_id: Annotated[str, Field(pattern=_CERTIFICATE_ID.pattern)]
    sponsor_decision_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    decided_at: datetime
    root_key_id: Annotated[str, Field(pattern=_ROOT_KEY_ID.pattern)]
    root_signature: str

    @field_validator("schema_version", "authority_epoch", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid authority attestation integer")
        return value

    @field_validator("decided_at")
    @classmethod
    def require_utc_second(cls, value: datetime) -> datetime:
        return _require_utc_second(value)

    @field_validator("root_signature")
    @classmethod
    def require_root_signature(cls, value: str) -> str:
        _decode_base64url(
            value, label="authority root signature", maximum_encoded=96, exact_decoded=64
        )
        return value

    @model_validator(mode="after")
    def require_transition_digests(self) -> AuthorityAttestationV2:
        migration = self.operation == "v1-migration"
        if migration != (self.previous_authority_digest is None):
            raise ValueError("authority operation and predecessor disagree")
        if self.previous_authority_digest == self.authority_digest:
            raise ValueError("authority transition did not change")
        return self


class V1MigrationProof(_TeamStateModel):
    prior_manifest_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    legacy_signatures: Annotated[tuple[StateSignature, ...], Field(min_length=1, max_length=64)]

    @field_validator("legacy_signatures", mode="before")
    @classmethod
    def require_signature_tuple(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @model_validator(mode="after")
    def require_sorted_signatures(self) -> V1MigrationProof:
        _require_sorted_unique_ids(tuple(item.signature_id for item in self.legacy_signatures))
        return self


class StateSignatureEnvelopeV2(_CanonicalWireModel):
    schema_version: Literal[2] = 2
    manifest_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    authority_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    certificates: Annotated[
        tuple[DeviceSignerCertificateV2, ...],
        Field(min_length=1, max_length=MAX_RELEASE_SIGNATURES),
    ]
    signatures: Annotated[
        tuple[CertifiedStateSignatureV2, ...],
        Field(min_length=1, max_length=MAX_RELEASE_SIGNATURES),
    ]
    authority_attestation: AuthorityAttestationV2 | None = None
    migration_proof: V1MigrationProof | None = None

    _wire_maximum: ClassVar[int] = MAX_SIGNATURE_BYTES
    _wire_label: ClassVar[str] = "team-state signature envelope"

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid signature envelope integer")
        return value

    @field_validator("certificates", "signatures", mode="before")
    @classmethod
    def require_tuples(cls, value: object, info: ValidationInfo) -> object:
        converted = _json_tuple(value, info)
        if info.mode != "json" or type(converted) is not tuple:
            return converted
        model: type[_TeamStateModel] = (
            DeviceSignerCertificateV2
            if info.field_name == "certificates"
            else CertifiedStateSignatureV2
        )
        return tuple(
            model.model_validate_json(_canonical_json(item)) if type(item) is dict else item
            for item in converted
        )

    @model_validator(mode="after")
    def require_bound_signatures(self) -> StateSignatureEnvelopeV2:
        certificates = tuple(item.certificate_id for item in self.certificates)
        signature_certificates = tuple(item.certificate_id for item in self.signatures)
        signature_ids = tuple(item.signature_id for item in self.signatures)
        if (
            certificates != tuple(sorted(certificates))
            or len(certificates) != len(set(certificates))
            or signature_certificates != tuple(sorted(signature_certificates))
            or len(signature_certificates) != len(set(signature_certificates))
            or len(signature_ids) != len(set(signature_ids))
            or certificates != signature_certificates
        ):
            raise ValueError("signature certificate set changed")
        certificate_map = {item.certificate_id: item for item in self.certificates}
        certificate_scope = {
            (
                item.claims.project_id,
                item.claims.repository_id,
                item.claims.authority_epoch,
                item.root_key_id,
            )
            for item in self.certificates
        }
        if len(certificate_scope) != 1:
            raise ValueError("certificate authority scopes disagree")
        for signature in self.signatures:
            if (
                certificate_map[signature.certificate_id].claims.signature_id
                != signature.signature_id
            ):
                raise ValueError("signature key does not match certificate")
        if (
            self.authority_attestation is not None
            and self.authority_attestation.authority_digest != self.authority_digest
        ):
            raise ValueError("authority attestation digest changed")
        if self.authority_attestation is not None:
            attestation = self.authority_attestation
            project_id, repository_id, authority_epoch, root_key_id = next(iter(certificate_scope))
            sponsor_certificate = certificate_map.get(attestation.sponsor_device_certificate_id)
            if (
                attestation.project_id != project_id
                or attestation.repository_id != repository_id
                or attestation.authority_epoch != authority_epoch
                or attestation.root_key_id != root_key_id
                or sponsor_certificate is None
                or sponsor_certificate.claims.member_id != attestation.sponsor_member_id
            ):
                raise ValueError("authority attestation scope changed")
        migration = self.authority_attestation is not None and (
            self.authority_attestation.operation == "v1-migration"
        )
        if migration != (self.migration_proof is not None):
            raise ValueError("migration proof and authority attestation disagree")
        return self


TeamManifest = TeamStateManifest | TeamStateManifestV2
SignatureEnvelope = StateSignatureEnvelope | StateSignatureEnvelopeV2


def canonical_manifest_bytes(manifest: TeamManifest) -> bytes:
    """Return the sole canonical UTF-8 JSON encoding used for signing and AAD."""
    if type(manifest) is TeamStateManifest:
        validated: TeamManifest = TeamStateManifest.model_validate(
            manifest.model_dump(mode="python")
        )
    elif type(manifest) is TeamStateManifestV2:
        validated = TeamStateManifestV2.model_validate(manifest.model_dump(mode="python"))
    else:
        raise TypeError("manifest must be a supported TeamStateManifest")
    content = _canonical_json(validated.model_dump(mode="json"))
    if not content or len(content) > MAX_MANIFEST_BYTES:
        raise ValueError("team-state manifest is oversized")
    return content


class CanonicalStateFile(_TeamStateModel):
    """One immutable canonical-state path and its exact plaintext bytes."""

    path: str
    content: Annotated[bytes, Field(max_length=MAX_FILE_BYTES)]

    @field_validator("path")
    @classmethod
    def require_canonical_path(cls, value: str) -> str:
        if value not in _REQUIRED_PATHS:
            raise ValueError("invalid canonical state path")
        return value


class BundleInventoryEntry(_TeamStateModel):
    """Digest-only inventory record for one canonical state file."""

    path: str
    size: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)]
    sha256: Annotated[str, Field(pattern=_SHA256.pattern)]

    @field_validator("size", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid inventory size")
        return value

    @field_validator("path")
    @classmethod
    def require_canonical_path(cls, value: str) -> str:
        if value not in _REQUIRED_PATHS:
            raise ValueError("invalid inventory path")
        return value


class BundleInventory(_TeamStateModel):
    """Complete bounded digest inventory for a canonical snapshot."""

    entries: Annotated[
        tuple[BundleInventoryEntry, ...],
        Field(min_length=1, max_length=MAX_STATE_FILES),
    ]
    total_size: Annotated[int, Field(ge=0, le=MAX_STATE_BYTES)]

    @field_validator("entries", mode="before")
    @classmethod
    def require_entry_tuple(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @field_validator("total_size", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid inventory total")
        return value

    @model_validator(mode="after")
    def require_complete_consistent_inventory(self) -> BundleInventory:
        paths = tuple(entry.path for entry in self.entries)
        if paths != CANONICAL_STATE_PATHS or len(paths) != len(set(paths)):
            raise ValueError("inventory must contain each canonical state path exactly once")
        if sum(entry.size for entry in self.entries) != self.total_size:
            raise ValueError("inventory total does not match entries")
        return self


class CanonicalStateSnapshot(_TeamStateModel):
    """One immutable, complete plaintext state captured before archive creation."""

    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    graph_version: Annotated[int, Field(ge=1)]
    files: Annotated[
        tuple[CanonicalStateFile, ...],
        Field(min_length=1, max_length=MAX_STATE_FILES),
    ]

    @field_validator("graph_version", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid graph version")
        return value

    @field_validator("files", mode="before")
    @classmethod
    def require_file_tuple(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @model_validator(mode="after")
    def require_complete_bounded_snapshot(self) -> CanonicalStateSnapshot:
        paths = tuple(item.path for item in self.files)
        if paths != CANONICAL_STATE_PATHS or len(paths) != len(set(paths)):
            raise ValueError("snapshot must contain each canonical state path exactly once")
        if sum(len(item.content) for item in self.files) > MAX_STATE_BYTES:
            raise ValueError("canonical state snapshot is oversized")
        return self

    def inventory(self) -> BundleInventory:
        validated = CanonicalStateSnapshot.model_validate(self.model_dump(mode="python"))
        entries = tuple(
            BundleInventoryEntry(
                path=item.path,
                size=len(item.content),
                sha256=_digest(item.content),
            )
            for item in validated.files
        )
        return BundleInventory(entries=entries, total_size=sum(item.size for item in entries))


class RestoredSnapshotV2(_TeamStateModel):
    """A canonical state snapshot paired with its encrypted v2 authority."""

    snapshot: CanonicalStateSnapshot
    authority: TeamAuthorityRegistryV2

    @model_validator(mode="after")
    def require_shared_scope(self) -> RestoredSnapshotV2:
        snapshot = CanonicalStateSnapshot.model_validate(self.snapshot.model_dump(mode="python"))
        authority = TeamAuthorityRegistryV2.model_validate(self.authority.model_dump(mode="python"))
        if (
            snapshot.project_id != authority.project_id
            or snapshot.repository_id != authority.repository_id
        ):
            raise ValueError("restored state and authority scope changed")
        return self


class RecipientRecord(_TeamStateModel):
    """Reviewed public recipient material; private key bytes are never represented."""

    schema_version: Literal[1] = 1
    key_id: Annotated[str, Field(pattern=_KEY_ID.pattern)]
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    actor: Annotated[str, Field(pattern=_KEY_ID.pattern)]
    github_account_id: Annotated[str, Field(pattern=_GITHUB_ACCOUNT_ID.pattern)]
    github_login: Annotated[str, Field(pattern=_GITHUB_LOGIN.pattern)]
    public_key: str
    webauthn_credential_id: str
    webauthn_credential_public_key: str
    encryption_algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"] = ENCRYPTION_ALGORITHM
    decision_algorithm: Literal["webauthn-decision-v1"] = DECISION_ALGORITHM
    enrolled_at: datetime

    @field_validator("public_key")
    @classmethod
    def require_x25519_public_key(cls, value: str) -> str:
        _decode_base64url(
            value,
            label="recipient public key",
            maximum_encoded=64,
            exact_decoded=32,
        )
        return value

    @field_validator("webauthn_credential_id")
    @classmethod
    def require_webauthn_credential_id(cls, value: str) -> str:
        _decode_base64url(
            value,
            label="WebAuthn credential ID",
            maximum_encoded=_MAX_WEBAUTHN_CREDENTIAL_ID,
        )
        return value

    @field_validator("webauthn_credential_public_key")
    @classmethod
    def require_webauthn_public_key(cls, value: str) -> str:
        _decode_base64url(
            value,
            label="WebAuthn credential public key",
            maximum_encoded=_MAX_WEBAUTHN_PUBLIC_KEY,
            minimum_decoded=16,
        )
        return value

    @field_validator("github_login")
    @classmethod
    def require_canonical_github_login(cls, value: str) -> str:
        if "--" in value:
            raise ValueError("invalid canonical GitHub login")
        return value

    @field_validator("enrolled_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class CiRecipientRecord(_TeamStateModel):
    """Dedicated runner encryption identity; it conveys no human decision authority."""

    schema_version: Literal[1] = 1
    kind: Literal["ci"] = "ci"
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    runner_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9._-]{0,63}$")]
    public_key: str
    key_id: str = ""
    encryption_algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"] = ENCRYPTION_ALGORITHM

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid CI recipient version")
        return value

    @model_validator(mode="after")
    def require_bound_key(self) -> CiRecipientRecord:
        _decode_base64url(
            self.public_key, label="CI public key", maximum_encoded=64, exact_decoded=32
        )
        identity = _canonical_json(
            {
                "project_id": self.project_id,
                "repository_id": self.repository_id,
                "runner_id": self.runner_id,
                "public_key": self.public_key,
                "schema": "intent.ci-recipient.v1",
            }
        )
        expected = f"recipient:ci:sha256:{hashlib.sha256(identity).hexdigest()}"
        if self.key_id and self.key_id != expected:
            raise ValueError("CI recipient binding changed")
        object.__setattr__(self, "key_id", expected)
        return self


EncryptionRecipient = RecipientRecord | CiRecipientRecord


def validate_encryption_recipient(value: object) -> EncryptionRecipient:
    """Revalidate exact public records, including instances made without validation."""
    if type(value) is RecipientRecord:
        return RecipientRecord.model_validate(value.model_dump(mode="python"))
    if type(value) is CiRecipientRecord:
        return CiRecipientRecord.model_validate(value.model_dump(mode="python"))
    raise ValueError("invalid encryption recipient")


class PublicationLineage(_TeamStateModel):
    """A release digest plus its nearest-first authenticated ancestry."""

    bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    parent_bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)] | None = None
    ancestor_bundle_digests: Annotated[tuple[str, ...], Field(max_length=64)] = ()

    @field_validator("ancestor_bundle_digests", mode="before")
    @classmethod
    def require_ancestor_tuple(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @model_validator(mode="after")
    def require_genesis_or_exact_parent_chain(self) -> PublicationLineage:
        ancestors = self.ancestor_bundle_digests
        if len(ancestors) != len(set(ancestors)) or any(
            _SHA256.fullmatch(item) is None for item in ancestors
        ):
            raise ValueError("invalid publication ancestry")
        if self.parent_bundle_digest is None:
            if ancestors:
                raise ValueError("genesis publication cannot have ancestors")
        elif not ancestors or ancestors[0] != self.parent_bundle_digest:
            raise ValueError("publication parent must be the first ancestor")
        if self.bundle_digest in ancestors:
            raise ValueError("publication cannot contain itself in its ancestry")
        return self


class RemoteStateSnapshot(_TeamStateModel):
    """Manifest bytes read from one immutable remote-tracking commit."""

    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    ref: Literal["refs/remotes/origin/intent-state"] = STATE_REF
    commit: Annotated[str, Field(pattern=_GIT_COMMIT.pattern)]
    manifest: TeamStateManifest
    manifest_bytes: Annotated[bytes, Field(min_length=1, max_length=MAX_MANIFEST_BYTES)]

    @model_validator(mode="after")
    def require_exact_manifest_binding(self) -> RemoteStateSnapshot:
        if self.manifest.repository_id != self.repository_id:
            raise ValueError("remote repository does not match manifest")
        if self.manifest_bytes != canonical_manifest_bytes(self.manifest):
            raise ValueError("remote manifest bytes do not match typed manifest")
        return self


class PreparedPublication(_TeamStateModel):
    """Exact offline artifacts ready for a publication-branch push and PR."""

    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    branch: Annotated[str, Field(pattern=_PUBLICATION_BRANCH.pattern)]
    manifest: TeamStateManifest
    manifest_bytes: Annotated[bytes, Field(min_length=1, max_length=MAX_MANIFEST_BYTES)]
    bundle: Annotated[bytes, Field(min_length=1, max_length=MAX_BUNDLE_BYTES)]
    signatures: Annotated[bytes, Field(min_length=1, max_length=MAX_SIGNATURE_BYTES)]
    bundle_path: str
    signature_path: str
    decision_algorithm: Literal["webauthn-decision-v1"] = DECISION_ALGORITHM

    @model_validator(mode="after")
    def require_exact_artifact_bindings(self) -> PreparedPublication:
        manifest = self.manifest
        digest_hex = manifest.bundle_digest.removeprefix("sha256:")
        expected_name = f"{manifest.graph_version}-{digest_hex}"
        if (
            manifest.repository_id != self.repository_id
            or self.manifest_bytes != canonical_manifest_bytes(manifest)
            or len(self.bundle) != manifest.bundle_size
            or _digest(self.bundle) != manifest.bundle_digest
            or self.branch != f"intent-publication/{digest_hex}"
            or self.bundle_path != f"bundles/{expected_name}.intent"
            or self.signature_path != f"signatures/{expected_name}.json"
        ):
            raise ValueError("prepared publication artifacts are not exactly bound")
        return self


# Compatibility name for the hardened restore module's schema-version-1 wire API.
SharedStateManifest = TeamStateManifest

__all__ = [
    "CANONICAL_STATE_PATHS",
    "DECISION_ALGORITHM",
    "ENCRYPTION_ALGORITHM",
    "MAX_AUTHORITY_BYTES",
    "MAX_AUTHORITY_DEVICES",
    "MAX_AUTHORITY_MEMBERS",
    "MAX_AUTHORITY_REVOCATIONS",
    "MAX_BUNDLE_BYTES",
    "MAX_FILE_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_RELEASE_SIGNATURES",
    "MAX_SIGNATURE_BYTES",
    "MAX_STATE_BYTES",
    "MAX_STATE_FILES",
    "STATE_REF",
    "AuthorityAttestationV2",
    "BundleInventory",
    "BundleInventoryEntry",
    "CanonicalStateFile",
    "CanonicalStateSnapshot",
    "CertifiedStateSignatureV2",
    "DeviceCertificateClaimsV2",
    "DeviceRevocationV2",
    "DeviceSignerCertificateV2",
    "MemberRecordV2",
    "PreparedPublication",
    "PublicationLineage",
    "RecipientRecord",
    "RemoteStateSnapshot",
    "RestoredSnapshotV2",
    "SharedStateManifest",
    "SignatureEnvelope",
    "StateSignature",
    "StateSignatureEnvelope",
    "StateSignatureEnvelopeV2",
    "TeamAuthorityPolicyV2",
    "TeamAuthorityRegistryV2",
    "TeamManifest",
    "TeamRootTrustV2",
    "TeamStateManifest",
    "TeamStateManifestV2",
    "V1MigrationBinding",
    "V1MigrationProof",
    "canonical_manifest_bytes",
]
