"""Fail-closed public contracts for local human authority."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

_MAX_IDENTIFIER_BYTES = 512
_MAX_CREDENTIAL_BYTES = 4096
_MAX_SELECTED_NODE_IDS = 256
_MAX_PAYLOAD_BYTES = 16 * 1024
_MAX_CHALLENGE_LIFETIME = timedelta(minutes=5)
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY_ID_PATTERN = re.compile(r"^repo:sha256:[0-9a-f]{64}$")
_CHALLENGE_ID_PATTERN = re.compile(r"^challenge:[0-9a-f]{64}$")
_SUBJECT_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class DecisionAction(StrEnum):
    """The bounded human actions that may carry a WebAuthn assertion."""

    CONFIRM_BASELINE = "confirm_baseline"
    ANSWER_CLARIFICATION = "answer_clarification"
    CONFIRM_PROPOSAL = "confirm_proposal"
    RESOLVE_CONFLICT = "resolve_conflict"
    PUBLISH_STATE = "publish_state"
    APPROVE_EXTERNAL_WRITE = "approve_external_write"


class DevStatus(StrEnum):
    """Readiness state returned by the local developer control plane."""

    READY = "ready"
    ONBOARDING_REQUIRED = "onboarding_required"
    HUMAN_ATTENTION_REQUIRED = "human_attention_required"
    OFFLINE_STALE = "offline_stale"
    SHARED_STATE_UNAVAILABLE = "shared_state_unavailable"
    SHARED_STATE_INVALID = "shared_state_invalid"
    UPGRADE_REQUIRED = "upgrade_required"
    LOCAL_ONLY = "local_only"


class AttentionRoute(StrEnum):
    """The bounded UI locations to which a readiness result may direct a user."""

    HOME = "home"
    ONBOARDING = "onboarding"
    INBOX = "inbox"
    PROPOSAL = "proposal"
    TEAM_STATE = "team_state"


class _ControlPlaneModel(BaseModel):
    """Frozen, strict base for all control-plane transport records."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def require_exact_schema_version(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("schema_version must be an exact integer")
        return value


def _exact_string(value: object, *, label: str, maximum: int = _MAX_IDENTIFIER_BYTES) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be an exact string")
    if not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds maximum size")
    return value


def _canonical_timestamp(value: datetime) -> str:
    utc_value = value.astimezone(UTC)
    suffix = utc_value.isoformat(timespec="microseconds" if utc_value.microsecond else "seconds")
    return f"{suffix.removesuffix('+00:00')}Z"


def _utc_timestamp(value: object) -> datetime:
    if type(value) is str:
        text = value
        if not _TIMESTAMP_PATTERN.fullmatch(text):
            raise ValueError("timestamp must use canonical UTC Z")
        try:
            parsed = datetime.fromisoformat(f"{text[:-1]}+00:00")
        except ValueError as error:
            raise ValueError("timestamp must use canonical UTC Z") from error
        if _canonical_timestamp(parsed) != text:
            raise ValueError("timestamp must use canonical UTC Z")
        return parsed
    if type(value) is not datetime:
        raise ValueError("timestamp must be an exact datetime")
    offset = value.utcoffset() if value.tzinfo is not None else None
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must use UTC")
    return value.astimezone(UTC)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


class DecisionSubject(_ControlPlaneModel):
    """One precise proposal, case, plan, answer, or equivalent decision target."""

    kind: Annotated[str, Field(max_length=64)]
    id: Annotated[str, Field(max_length=_MAX_IDENTIFIER_BYTES)]

    @field_validator("kind", "id", mode="before")
    @classmethod
    def require_exact_identifiers(cls, value: object, info: ValidationInfo) -> str:
        return _exact_string(value, label=str(info.field_name), maximum=_MAX_IDENTIFIER_BYTES)

    @model_validator(mode="after")
    def validate_subject(self) -> DecisionSubject:
        if not _SUBJECT_KIND_PATTERN.fullmatch(self.kind):
            raise ValueError("subject kind is invalid")
        if not self.id.startswith(f"{self.kind}:"):
            raise ValueError("subject identifier must match subject kind")
        return self


class HumanDecisionPayload(_ControlPlaneModel):
    """The only canonical byte representation that later code signs or verifies."""

    schema_version: Literal[1] = 1
    project_id: Annotated[str, Field(max_length=_MAX_IDENTIFIER_BYTES)]
    repository_id: Annotated[str, Field(max_length=76)]
    actor: Annotated[str, Field(max_length=_MAX_IDENTIFIER_BYTES)]
    action: DecisionAction
    graph_version: Annotated[int, Field(ge=0, le=2**63 - 1)]
    parent_bundle_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN.pattern)]
    subject: DecisionSubject
    subject_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN.pattern)]
    selected_node_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_SELECTED_NODE_IDS)] = ()
    result_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN.pattern)]
    challenge: Annotated[str, Field(pattern=_CHALLENGE_ID_PATTERN.pattern)]
    issued_at: datetime
    expires_at: datetime

    @field_validator(
        "project_id",
        "repository_id",
        "actor",
        "parent_bundle_digest",
        "subject_digest",
        "result_digest",
        "challenge",
        mode="before",
    )
    @classmethod
    def require_exact_strings(cls, value: object, info: ValidationInfo) -> str:
        return _exact_string(value, label=str(info.field_name))

    @field_validator("issued_at", "expires_at", mode="before")
    @classmethod
    def require_utc_timestamps(cls, value: object) -> datetime:
        return _utc_timestamp(value)

    @field_validator("selected_node_ids")
    @classmethod
    def require_canonical_selected_nodes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(type(item) is not str for item in value):
            raise ValueError("selected node identifiers must be exact strings")
        if any(not item or len(item.encode("utf-8")) > _MAX_IDENTIFIER_BYTES for item in value):
            raise ValueError("selected node identifier exceeds maximum size")
        if tuple(sorted(set(value))) != value:
            raise ValueError("selected node identifiers must be unique and sorted")
        return value

    @field_serializer("issued_at", "expires_at")
    def serialize_timestamps(self, value: datetime) -> str:
        return _canonical_timestamp(value)

    @model_validator(mode="after")
    def validate_signed_binding(self) -> HumanDecisionPayload:
        if not _REPOSITORY_ID_PATTERN.fullmatch(self.repository_id):
            raise ValueError("repository identifier must be canonical")
        if (
            self.expires_at < self.issued_at
            or self.expires_at - self.issued_at > _MAX_CHALLENGE_LIFETIME
        ):
            raise ValueError("decision expiry must be within five minutes")
        if self.action in {DecisionAction.CONFIRM_BASELINE, DecisionAction.CONFIRM_PROPOSAL}:
            if not self.selected_node_ids:
                raise ValueError("selected node identifiers are required for this action")
        elif self.selected_node_ids:
            raise ValueError("selected node identifiers are not allowed for this action")
        if len(self.canonical_bytes()) > _MAX_PAYLOAD_BYTES:
            raise ValueError("decision payload exceeds maximum size")
        return self

    def canonical_bytes(self) -> bytes:
        """Return the exact UTF-8 JSON bytes that an authenticator binds."""
        return _canonical_json_bytes(self.model_dump(mode="json"))


class CredentialRecord(_ControlPlaneModel):
    """A non-secret registered WebAuthn credential and its repository binding."""

    schema_version: Literal[1] = 1
    id: Annotated[str, Field(max_length=_MAX_IDENTIFIER_BYTES)]
    project_id: Annotated[str, Field(max_length=_MAX_IDENTIFIER_BYTES)]
    repository_id: Annotated[str, Field(max_length=76)]
    actor: Annotated[str, Field(max_length=_MAX_IDENTIFIER_BYTES)]
    credential_id: Annotated[str, Field(max_length=_MAX_CREDENTIAL_BYTES)]
    public_key: Annotated[str, Field(max_length=_MAX_CREDENTIAL_BYTES)]
    sign_count: Annotated[int, Field(ge=0, le=2**32 - 1)]
    created_at: datetime
    local_only: bool = True
    github_account_id: Annotated[str, Field(max_length=_MAX_IDENTIFIER_BYTES)] | None = None
    github_login: Annotated[str, Field(max_length=_MAX_IDENTIFIER_BYTES)] | None = None

    @field_validator(
        "id",
        "project_id",
        "repository_id",
        "actor",
        "credential_id",
        "public_key",
        "github_account_id",
        "github_login",
        mode="before",
    )
    @classmethod
    def require_exact_optional_strings(cls, value: object, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _exact_string(
            value,
            label=str(info.field_name),
            maximum=(
                _MAX_CREDENTIAL_BYTES
                if info.field_name in {"credential_id", "public_key"}
                else _MAX_IDENTIFIER_BYTES
            ),
        )

    @field_validator("created_at", mode="before")
    @classmethod
    def require_created_at_utc(cls, value: object) -> datetime:
        return _utc_timestamp(value)

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        return _canonical_timestamp(value)

    @model_validator(mode="after")
    def validate_credential(self) -> CredentialRecord:
        if not _REPOSITORY_ID_PATTERN.fullmatch(self.repository_id):
            raise ValueError("repository identifier must be canonical")
        if not self.id.startswith("credential:"):
            raise ValueError("credential identifier must be canonical")
        if not _BASE64URL_PATTERN.fullmatch(self.credential_id) or not _BASE64URL_PATTERN.fullmatch(
            self.public_key
        ):
            raise ValueError("credential material must be base64url")
        if self.local_only != (self.github_account_id is None and self.github_login is None):
            raise ValueError("credential enrollment identity is inconsistent")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))


class ChallengeRecord(_ControlPlaneModel):
    """A short-lived, repository-bound registration or authentication challenge."""

    schema_version: Literal[1] = 1
    id: Annotated[str, Field(max_length=76)]
    project_id: Annotated[str, Field(max_length=_MAX_IDENTIFIER_BYTES)]
    repository_id: Annotated[str, Field(max_length=76)]
    actor: Annotated[str, Field(max_length=_MAX_IDENTIFIER_BYTES)]
    ceremony: Literal["registration", "authentication"]
    challenge: Annotated[str, Field(max_length=_MAX_CREDENTIAL_BYTES)]
    payload_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN.pattern)] | None = None
    issued_at: datetime
    expires_at: datetime

    @field_validator(
        "id", "project_id", "repository_id", "actor", "challenge", "payload_digest", mode="before"
    )
    @classmethod
    def require_exact_challenge_strings(cls, value: object, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _exact_string(
            value,
            label=str(info.field_name),
            maximum=(
                _MAX_CREDENTIAL_BYTES if info.field_name == "challenge" else _MAX_IDENTIFIER_BYTES
            ),
        )

    @field_validator("issued_at", "expires_at", mode="before")
    @classmethod
    def require_challenge_timestamps(cls, value: object) -> datetime:
        return _utc_timestamp(value)

    @field_serializer("issued_at", "expires_at")
    def serialize_challenge_timestamps(self, value: datetime) -> str:
        return _canonical_timestamp(value)

    @model_validator(mode="after")
    def validate_challenge(self) -> ChallengeRecord:
        if not _CHALLENGE_ID_PATTERN.fullmatch(self.id):
            raise ValueError("challenge identifier must be canonical")
        if not _REPOSITORY_ID_PATTERN.fullmatch(self.repository_id):
            raise ValueError("repository identifier must be canonical")
        if not _BASE64URL_PATTERN.fullmatch(self.challenge):
            raise ValueError("challenge must be base64url")
        if self.ceremony == "registration" and self.payload_digest is not None:
            raise ValueError("registration challenge cannot bind a decision payload")
        if self.ceremony == "authentication" and self.payload_digest is None:
            raise ValueError("authentication challenge must bind a decision payload")
        if (
            self.expires_at < self.issued_at
            or self.expires_at - self.issued_at > _MAX_CHALLENGE_LIFETIME
        ):
            raise ValueError("challenge expiry must be within five minutes")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))
