"""Strict immutable records for previewed and approved external mutations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from math import isfinite
from types import MappingProxyType
from typing import Literal, cast

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from intent_engineering.capture.mcp.profile_models import (
    ProviderBinding,
    ProviderProfile,
)
from intent_engineering.core.models import JsonValue
from intent_engineering.core.models._base import StrictModel

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HASH = r"^sha256:[0-9a-f]{64}$"
_PLAN_ID = r"^write-plan:sha256:[0-9a-f]{64}$"
_APPROVAL_ID = r"^approval:sha256:[0-9a-f]{64}$"
_MAX_APPROVAL_WINDOW = timedelta(minutes=15)


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 2048 or _CONTROL.search(value):
        raise ValueError(f"invalid {label}")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return value.astimezone(UTC)


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        frozen: dict[str, object] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError("invalid mutation JSON")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if type(value) is list:
        return tuple(_freeze_json(item) for item in cast(list[object], value))
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and isfinite(value):
        return value
    raise ValueError("invalid mutation JSON")


def _thaw_json(value: object) -> JsonValue:
    if type(value) is dict:
        thawed: dict[str, JsonValue] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError("invalid mutation JSON")
            thawed[key] = _thaw_json(item)
        return thawed
    if type(value) is MappingProxyType:
        return {
            key: _thaw_json(item)
            for key, item in cast(Mapping[str, object], value).items()
        }
    if type(value) is list:
        return [_thaw_json(item) for item in cast(list[object], value)]
    if type(value) is tuple:
        return [_thaw_json(item) for item in cast(tuple[object, ...], value)]
    if value is None or type(value) in {str, bool, int}:
        return cast(JsonValue, value)
    if type(value) is float and isfinite(value):
        return value
    raise ValueError("invalid mutation JSON")


def _freeze_object(value: object) -> Mapping[str, JsonValue]:
    if type(value) is not dict:
        raise ValueError("mutation JSON object must be exact")
    frozen = _freeze_json(value)
    if type(frozen) is not MappingProxyType:
        raise ValueError("invalid mutation JSON object")
    return cast(Mapping[str, JsonValue], frozen)


def _require_exact_object(value: object) -> object:
    if type(value) is not dict:
        raise ValueError("mutation JSON object must be exact")
    return value


def _canonical_hash(payload: Mapping[str, JsonValue]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class _MutationModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def exact_schema_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema version must be an integer")
        return value


class RemoteObject(_MutationModel):
    """Exact current provider representation used to construct a preview."""

    schema_version: Literal[1] = 1
    connector_id: str
    profile_id: str
    profile_version: str
    object_type: str
    id: str
    version: str
    content: Mapping[str, JsonValue]

    @field_validator(
        "connector_id", "profile_id", "profile_version", "object_type", "id", "version"
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _text(value, "remote object field")

    @field_validator("content", mode="before")
    @classmethod
    def require_exact_content(cls, value: object) -> object:
        return _require_exact_object(value)

    @field_validator("content")
    @classmethod
    def freeze_content(cls, value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return _freeze_object(dict(value))

    @field_serializer("content")
    def serialize_content(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _thaw_json(value)


class WritePlan(_MutationModel):
    """Hash-bound exact preview of one guarded provider mutation."""

    schema_version: Literal[1] = 1
    id: str = Field(pattern=_PLAN_ID)
    case_id: str
    connector_id: str
    profile_id: str
    profile_version: str
    object_type: str
    binding_hash: str = Field(pattern=_HASH)
    write_contract_hash: str = Field(pattern=_HASH)
    operation: str
    provider_operation: str
    target_id: str
    before_version: str
    before: Mapping[str, JsonValue]
    after: Mapping[str, JsonValue]
    arguments: Mapping[str, JsonValue]
    evidence_refs: tuple[str, ...]
    conflicting_authors: tuple[str, ...]
    created_by: str
    created_by_aliases: tuple[str, ...]
    created_at: datetime
    expires_at: datetime

    @field_validator(
        "case_id",
        "connector_id",
        "profile_id",
        "profile_version",
        "object_type",
        "operation",
        "provider_operation",
        "target_id",
        "before_version",
        "created_by",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _text(value, "write plan field")

    @field_validator("before", "after", "arguments", mode="before")
    @classmethod
    def require_exact_objects(cls, value: object) -> object:
        return _require_exact_object(value)

    @field_validator("before", "after", "arguments")
    @classmethod
    def freeze_objects(cls, value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return _freeze_object(dict(value))

    @field_serializer("before", "after", "arguments")
    def serialize_objects(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _thaw_json(value)

    @field_validator("evidence_refs", "conflicting_authors", "created_by_aliases")
    @classmethod
    def validate_ordered_identity(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or tuple(sorted(set(values))) != values:
            raise ValueError("mutation provenance must be nonempty, unique, and sorted")
        return tuple(_text(value, "mutation provenance") for value in values)

    @field_validator("created_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @property
    def canonical_hash(self) -> str:
        material = cast(
            Mapping[str, JsonValue],
            self.model_dump(mode="json", exclude={"id"}),
        )
        return _canonical_hash(material)

    @model_validator(mode="after")
    def validate_plan(self) -> WritePlan:
        if self.id != f"write-plan:{self.canonical_hash}":
            raise ValueError("write plan identifier does not match canonical hash")
        if self.expires_at <= self.created_at:
            raise ValueError("write plan expiry must follow creation")
        if self.created_by not in self.created_by_aliases:
            raise ValueError("write plan aliases omit creator")
        if not self.before or not self.after or not self.arguments or self.before == self.after:
            raise ValueError("write plan must describe one exact change")
        return self


class ApprovalRecord(_MutationModel):
    """Independent interactive authorization bound to one exact plan and target version."""

    schema_version: Literal[1] = 1
    id: str = Field(pattern=_APPROVAL_ID)
    plan_id: str = Field(pattern=_PLAN_ID)
    plan_hash: str = Field(pattern=_HASH)
    target_version: str
    actor: str
    actor_aliases: tuple[str, ...]
    plan_created_at: datetime
    plan_expires_at: datetime
    approved_at: datetime
    expires_at: datetime
    confirmation_method: Literal["interactive"] = "interactive"

    @field_validator("target_version", "actor")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _text(value, "approval field")

    @field_validator("actor_aliases")
    @classmethod
    def validate_actor_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or tuple(sorted(set(values))) != values:
            raise ValueError("approval aliases must be nonempty, unique, and sorted")
        return tuple(_text(value, "approval alias") for value in values)

    @field_validator("plan_created_at", "plan_expires_at", "approved_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @property
    def canonical_hash(self) -> str:
        return _canonical_hash(
            cast(Mapping[str, JsonValue], self.model_dump(mode="json", exclude={"id"}))
        )

    @model_validator(mode="after")
    def validate_approval(self) -> ApprovalRecord:
        if self.id != f"approval:{self.canonical_hash}":
            raise ValueError("approval identifier does not match canonical hash")
        if self.actor not in self.actor_aliases:
            raise ValueError("approval aliases omit actor")
        if self.plan_id != f"write-plan:{self.plan_hash}":
            raise ValueError("approval plan identity does not match plan hash")
        if self.plan_expires_at <= self.plan_created_at:
            raise ValueError("approval plan window is invalid")
        if self.approved_at < self.plan_created_at or self.approved_at >= self.plan_expires_at:
            raise ValueError("approval time is outside plan window")
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must follow approval")
        if self.expires_at - self.approved_at > _MAX_APPROVAL_WINDOW:
            raise ValueError("approval window exceeds maximum")
        if self.expires_at > self.plan_expires_at:
            raise ValueError("approval outlives plan")
        return self


class ExecutionReceipt(_MutationModel):
    """Immutable result of one attempted guarded mutation; persisted in Task 6."""

    schema_version: Literal[1] = 1
    id: str
    plan_id: str = Field(pattern=_PLAN_ID)
    approval_id: str = Field(pattern=_APPROVAL_ID)
    status: Literal["succeeded", "rejected", "failed"]
    attempted_at: datetime
    completed_at: datetime
    resulting_version: str | None = None
    evidence_ref: str
    redacted_error: str | None = None

    @field_validator("id", "evidence_ref")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _text(value, "receipt field")

    @field_validator("attempted_at", "completed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_receipt(self) -> ExecutionReceipt:
        if self.completed_at < self.attempted_at:
            raise ValueError("receipt completion precedes attempt")
        if self.status == "succeeded" and self.resulting_version is None:
            raise ValueError("successful receipt requires resulting version")
        if self.status != "succeeded" and self.resulting_version is not None:
            raise ValueError("unsuccessful receipt cannot contain resulting version")
        return self


class WriteResult(_MutationModel):
    """Provider-neutral successful write result consumed by Task 6."""

    schema_version: Literal[1] = 1
    resulting_version: str
    redacted_result: Mapping[str, JsonValue]

    @field_validator("resulting_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _text(value, "resulting version")

    @field_validator("redacted_result", mode="before")
    @classmethod
    def require_exact_result(cls, value: object) -> object:
        return _require_exact_object(value)

    @field_validator("redacted_result")
    @classmethod
    def freeze_result(cls, value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return _freeze_object(dict(value))

    @field_serializer("redacted_result")
    def serialize_result(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _thaw_json(value)


def write_plan_id(material: Mapping[str, JsonValue]) -> str:
    """Return the validated ID for write-plan fields excluding ID."""
    return f"write-plan:{_canonical_hash(material)}"


def approval_id(material: Mapping[str, JsonValue]) -> str:
    """Return the validated ID for approval fields excluding ID."""
    return f"approval:{_canonical_hash(material)}"


def provider_binding_hash(binding: ProviderBinding) -> str:
    """Hash the complete validated local provider mapping and identity aliases."""
    validated = ProviderBinding.model_validate_json(binding.model_dump_json())
    return _canonical_hash(
        cast(Mapping[str, JsonValue], validated.model_dump(mode="json"))
    )


def provider_write_contract_hash(
    profile: ProviderProfile,
    binding: ProviderBinding,
    operation_name: str,
) -> str:
    """Hash one exact semantic write contract and its provider capability name."""
    validated_profile = ProviderProfile.model_validate_json(profile.model_dump_json())
    validated_binding = ProviderBinding.model_validate_json(binding.model_dump_json())
    validated_binding.validate_against(validated_profile)
    operation = validated_profile.writes[operation_name]
    material: dict[str, JsonValue] = {
        "profile_id": validated_profile.id,
        "profile_version": validated_profile.version,
        "semantic_name": operation_name,
        "provider_operation": validated_binding.tools[operation_name],
        "operation": cast(JsonValue, operation.model_dump(mode="json")),
    }
    return _canonical_hash(material)


def identity_aliases_for(
    identity_aliases: object,
    actor: str,
    provider_principals: frozenset[str],
) -> tuple[str, ...]:
    """Resolve one person across local, provider, repository, and communication identities."""
    if type(identity_aliases) not in {dict, MappingProxyType}:
        raise ValueError("invalid identity aliases")
    aliases_by_actor = cast(Mapping[object, object], identity_aliases)
    aliases = aliases_by_actor.get(actor)
    if type(aliases) is not frozenset:
        raise ValueError("invalid identity aliases")
    resolved = cast(frozenset[object], aliases)
    if any(type(alias) is not str or not alias.strip() for alias in resolved):
        raise ValueError("invalid identity aliases")
    normalized = frozenset(cast(str, alias) for alias in resolved)
    if actor not in normalized or not provider_principals.issubset(normalized):
        raise ValueError("identity aliases omit authenticated identities")
    return tuple(sorted(normalized))
