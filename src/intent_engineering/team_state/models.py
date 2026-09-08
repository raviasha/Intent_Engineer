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


def canonical_manifest_bytes(manifest: TeamStateManifest) -> bytes:
    """Return the sole canonical UTF-8 JSON encoding used for signing and AAD."""
    if type(manifest) is not TeamStateManifest:
        raise TypeError("manifest must be a TeamStateManifest")
    validated = TeamStateManifest.model_validate(manifest.model_dump(mode="python"))
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
    "MAX_BUNDLE_BYTES",
    "MAX_FILE_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_SIGNATURE_BYTES",
    "MAX_STATE_BYTES",
    "MAX_STATE_FILES",
    "STATE_REF",
    "BundleInventory",
    "BundleInventoryEntry",
    "CanonicalStateFile",
    "CanonicalStateSnapshot",
    "PreparedPublication",
    "PublicationLineage",
    "RecipientRecord",
    "RemoteStateSnapshot",
    "SharedStateManifest",
    "TeamStateManifest",
    "canonical_manifest_bytes",
]
