"""Immutable, provider-neutral MCP profile and local binding contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Literal, cast

from jsonschema.validators import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import (
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from intent_engineering.core.models import JsonValue
from intent_engineering.core.models._base import StrictModel

_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_SELECTOR_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_DECIMAL_INDEX = re.compile(r"^(?:0|[1-9][0-9]*)$")
_MAX_SELECTOR_LENGTH = 256
_MAX_SELECTOR_DEPTH = 32
_TRANSFORM_INPUTS: dict[str, frozenset[str]] = {
    "string": frozenset({"string", "integer", "number"}),
    "integer": frozenset({"string", "integer"}),
    "iso_datetime": frozenset({"string"}),
    "string_list": frozenset({"list"}),
    "canonical_json": frozenset({"any"}),
    "sha256": frozenset({"any"}),
}
_TRANSFORM_OUTPUTS = {
    "string": "string",
    "integer": "integer",
    "iso_datetime": "string",
    "string_list": "list",
    "canonical_json": "string",
    "sha256": "string",
}

type ProfileText = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        pattern=r"^[^\x00-\x1f\x7f]*\S[^\x00-\x1f\x7f]*$",
    ),
]
type SelectorPath = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_MAX_SELECTOR_LENGTH,
        pattern=r"^\$(?:\.(?:[A-Za-z_][A-Za-z0-9_-]*|0|[1-9][0-9]*)){0,32}$",
        json_schema_extra={"allOf": [{"not": {"pattern": r"(?:^|\.)__"}}]},
    ),
]
type TransformName = Literal[
    "string", "integer", "iso_datetime", "string_list", "canonical_json", "sha256"
]


class BindingValidationError(ValueError):
    """A concise public failure for invalid profile-to-server bindings."""


def _require_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or _CONTROL_CHARACTER.search(value):
        raise ValueError(f"invalid {label}")
    return value


def _freeze_json(value: object) -> object:
    """Detach and recursively freeze one exact finite JSON value."""
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if type(value) is list or type(value) is tuple:
        return tuple(_freeze_json(item) for item in cast(list[object] | tuple[object, ...], value))
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float and isfinite(value):
        return value
    raise ValueError("profile values must be finite JSON values")


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return cast(JsonValue, value)


def _freeze_mapping(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        frozen[_require_text(key, label)] = item
    return MappingProxyType(frozen)


def validate_selector_path(path: object) -> str:
    """Validate the small, bounded selector language shared by every public model."""
    path = _require_text(path, "selector path")
    if len(path) > _MAX_SELECTOR_LENGTH or path == "$":
        if path == "$":
            return path
        raise ValueError("invalid selector path")
    if not path.startswith("$."):
        raise ValueError("invalid selector path")
    segments = path[2:].split(".")
    if len(segments) > _MAX_SELECTOR_DEPTH or any(not segment for segment in segments):
        raise ValueError("invalid selector path")
    for segment in segments:
        if _DECIMAL_INDEX.fullmatch(segment):
            continue
        if not _SELECTOR_KEY.fullmatch(segment) or segment.startswith("__"):
            raise ValueError("invalid selector path")
    return path


def validate_transform_chain(transforms: tuple[str, ...]) -> tuple[str, ...]:
    """Reject unknown transforms and impossible type reinterpretation chains."""
    current = "any"
    for transform in transforms:
        transform = _require_text(transform, "selector transform")
        accepted = _TRANSFORM_INPUTS.get(transform)
        if accepted is None:
            raise ValueError("invalid selector transform")
        if current != "any" and current not in accepted and "any" not in accepted:
            raise ValueError("incompatible selector transforms")
        current = _TRANSFORM_OUTPUTS[transform]
    return transforms


class McpProfileModel(StrictModel):
    """Strict frozen base for public MCP profile records."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class Selector(McpProfileModel):
    """One pure, bounded extraction path and optional normalization chain."""

    path: SelectorPath
    transforms: Annotated[tuple[TransformName, ...], Field(max_length=8)] = ()
    required: bool = True

    @field_validator("path", mode="before")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_selector_path(value)

    @field_validator("transforms")
    @classmethod
    def validate_transforms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_transform_chain(value)


class ArgumentBinding(McpProfileModel):
    """One explicitly enumerated argument source for a semantic operation."""

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"source": {"const": "constant"}}},
                    "then": {
                        "required": ["value"],
                        "not": {"required": ["field"]},
                    },
                },
                {
                    "if": {"properties": {"source": {"const": "field"}}},
                    "then": {
                        "required": ["field"],
                        "not": {"required": ["value"]},
                    },
                },
                {
                    "if": {
                        "properties": {
                            "source": {
                                "enum": [
                                    "scope",
                                    "cursor",
                                    "object_id",
                                    "object_version",
                                    "target_id",
                                    "before_version",
                                ]
                            }
                        }
                    },
                    "then": {
                        "not": {
                            "anyOf": [
                                {"required": ["value"]},
                                {"required": ["field"]},
                            ]
                        }
                    },
                },
            ]
        }
    )

    source: Literal[
        "constant",
        "scope",
        "cursor",
        "object_id",
        "object_version",
        "target_id",
        "before_version",
        "field",
    ]
    value: JsonValue | None = None
    field: ProfileText | None = None

    @field_validator("value", mode="after")
    @classmethod
    def freeze_value(cls, value: JsonValue | None) -> JsonValue | None:
        return cast(JsonValue | None, _freeze_json(value))

    @field_serializer("value")
    def serialize_value(self, value: JsonValue | None) -> JsonValue | None:
        return _thaw_json(value)

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str | None) -> str | None:
        return None if value is None else _require_text(value, "field")

    @model_validator(mode="after")
    def validate_source_shape(self) -> ArgumentBinding:
        if self.source == "field" and self.field is None:
            raise ValueError("field source requires field")
        if self.source != "field" and "field" in self.model_fields_set:
            raise ValueError("field is only valid for field source")
        if self.source == "constant" and "value" not in self.model_fields_set:
            raise ValueError("constant source requires value")
        if self.source != "constant" and "value" in self.model_fields_set:
            raise ValueError("value is only valid for constant source")
        return self

    @model_serializer(mode="wrap")
    def serialize_source_shape(self, handler: Callable[[object], object]) -> object:
        serialized = handler(self)
        assert isinstance(serialized, dict)
        if self.source != "constant":
            serialized.pop("value", None)
        if self.source != "field":
            serialized.pop("field", None)
        return serialized


class ReadOperation(McpProfileModel):
    """A discover or fetch capability backed by one tool or resource."""

    semantic_name: ProfileText
    kind: Literal["tool", "resource"]
    pagination: Literal["none", "cursor", "page"]
    arguments: dict[ProfileText, ArgumentBinding] = Field(default_factory=dict)
    item_selector: Selector
    next_cursor_selector: Selector | None = None

    @field_validator("semantic_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _require_text(value, "semantic operation name")

    @field_validator("arguments", mode="after")
    @classmethod
    def freeze_arguments(cls, value: dict[str, ArgumentBinding]) -> dict[str, ArgumentBinding]:
        return cast(dict[str, ArgumentBinding], _freeze_mapping(value, "argument name"))

    @field_serializer("arguments")
    def serialize_arguments(self, value: dict[str, ArgumentBinding]) -> dict[str, ArgumentBinding]:
        return dict(value)

    @model_validator(mode="after")
    def validate_pagination(self) -> ReadOperation:
        if self.pagination == "none" and self.next_cursor_selector is not None:
            raise ValueError("non-paginated operation forbids next cursor selector")
        if self.pagination != "none" and self.next_cursor_selector is None:
            raise ValueError("paginated operation requires next cursor selector")
        return self


class ObjectProfile(McpProfileModel):
    """Provider fields needed to normalize one externally versioned object."""

    discover_operation: ProfileText
    fetch_operation: ProfileText
    external_id: Selector
    external_version: Selector
    author: Selector
    observed_at: Selector
    locator: Selector
    parent_ref: Selector | None = None
    acl: Selector | None = None
    content: Annotated[dict[ProfileText, Selector], Field(min_length=1)]

    @field_validator("discover_operation", "fetch_operation")
    @classmethod
    def validate_operation_reference(cls, value: str) -> str:
        return _require_text(value, "semantic operation name")

    @field_validator("content", mode="after")
    @classmethod
    def freeze_content(cls, value: dict[str, Selector]) -> dict[str, Selector]:
        if not value:
            raise ValueError("content requires at least one field")
        return cast(dict[str, Selector], _freeze_mapping(value, "field name"))

    @field_serializer("content")
    def serialize_content(self, value: dict[str, Selector]) -> dict[str, Selector]:
        return dict(value)


class WriteOperationProfile(McpProfileModel):
    """One allowlisted, version-preconditioned external mutation contract."""

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "properties": {
                        "target_id": {
                            "not": {
                                "properties": {"required": {"const": False}},
                                "required": ["required"],
                            }
                        }
                    }
                },
                {
                    "properties": {
                        "before_version": {
                            "not": {
                                "properties": {"required": {"const": False}},
                                "required": ["required"],
                            }
                        }
                    }
                },
                {
                    "properties": {
                        "result_version": {
                            "not": {
                                "properties": {"required": {"const": False}},
                                "required": ["required"],
                            }
                        }
                    }
                },
            ]
        }
    )

    semantic_name: ProfileText
    target_object: ProfileText
    target_id: Selector
    before_version: Selector
    allowed_fields: Annotated[frozenset[ProfileText], Field(min_length=1)]
    arguments: Annotated[dict[ProfileText, ArgumentBinding], Field(min_length=1)]
    input_schema: dict[str, JsonValue]
    result_version: Selector

    @field_validator("semantic_name", "target_object")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _require_text(value, "semantic operation name")

    @field_validator("allowed_fields", mode="after")
    @classmethod
    def validate_fields(cls, value: frozenset[str]) -> frozenset[str]:
        if not value:
            raise ValueError("write operation requires allowed fields")
        return frozenset(_require_text(field, "field name") for field in value)

    @field_validator("arguments", mode="after")
    @classmethod
    def freeze_arguments(cls, value: dict[str, ArgumentBinding]) -> dict[str, ArgumentBinding]:
        return cast(dict[str, ArgumentBinding], _freeze_mapping(value, "argument name"))

    @field_serializer("arguments")
    def serialize_arguments(self, value: dict[str, ArgumentBinding]) -> dict[str, ArgumentBinding]:
        return dict(value)

    @field_validator("input_schema", mode="after")
    @classmethod
    def freeze_input_schema(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        frozen = _freeze_json(value)
        assert isinstance(frozen, Mapping)
        return cast(dict[str, JsonValue], frozen)

    @field_serializer("input_schema")
    def serialize_input_schema(self, value: dict[str, JsonValue]) -> JsonValue:
        return _thaw_json(value)

    @model_validator(mode="after")
    def validate_write_contract(self) -> WriteOperationProfile:
        if (
            not self.target_id.required
            or not self.before_version.required
            or not self.result_version.required
        ):
            raise ValueError("write selectors must be required")
        schema = _thaw_json(self.input_schema)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("write input schema must be a strict object schema")
        if schema.get("additionalProperties") is not False:
            raise ValueError("write input schema must forbid additional properties")
        properties = schema.get("properties")
        if not isinstance(properties, dict) or set(properties) != set(self.allowed_fields):
            raise ValueError("write input schema fields must match allowed fields")
        required = schema.get("required", [])
        if not isinstance(required, list) or not set(required).issubset(self.allowed_fields):
            raise ValueError("write input schema has invalid required fields")
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as error:
            raise ValueError("invalid write input schema") from error
        for binding in self.arguments.values():
            if binding.source == "field" and binding.field not in self.allowed_fields:
                raise ValueError("write argument field is not allowed")
        return self


class ProviderProfile(McpProfileModel):
    """A versioned, transport-independent semantic provider profile."""

    id: ProfileText
    version: ProfileText
    display_name: ProfileText
    operations: Annotated[dict[ProfileText, ReadOperation], Field(min_length=1)]
    objects: Annotated[dict[ProfileText, ObjectProfile], Field(min_length=1)]
    writes: dict[ProfileText, WriteOperationProfile]
    redacted_paths: frozenset[SelectorPath] = frozenset()

    @field_validator("id", "version")
    @classmethod
    def validate_id_and_version(cls, value: str) -> str:
        return _require_text(value, "profile identifier")

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return _require_text(value, "display name")

    @field_validator("operations", mode="after")
    @classmethod
    def freeze_operations(cls, value: dict[str, ReadOperation]) -> dict[str, ReadOperation]:
        if not value:
            raise ValueError("profile requires operations")
        return cast(dict[str, ReadOperation], _freeze_mapping(value, "semantic operation name"))

    @field_serializer("operations")
    def serialize_operations(self, value: dict[str, ReadOperation]) -> dict[str, ReadOperation]:
        return dict(value)

    @field_validator("objects", mode="after")
    @classmethod
    def freeze_objects(cls, value: dict[str, ObjectProfile]) -> dict[str, ObjectProfile]:
        if not value:
            raise ValueError("profile requires objects")
        return cast(dict[str, ObjectProfile], _freeze_mapping(value, "object name"))

    @field_serializer("objects")
    def serialize_objects(self, value: dict[str, ObjectProfile]) -> dict[str, ObjectProfile]:
        return dict(value)

    @field_validator("writes", mode="after")
    @classmethod
    def freeze_writes(
        cls, value: dict[str, WriteOperationProfile]
    ) -> dict[str, WriteOperationProfile]:
        return cast(
            dict[str, WriteOperationProfile], _freeze_mapping(value, "semantic operation name")
        )

    @field_serializer("writes")
    def serialize_writes(
        self, value: dict[str, WriteOperationProfile]
    ) -> dict[str, WriteOperationProfile]:
        return dict(value)

    @field_validator("redacted_paths", mode="after")
    @classmethod
    def validate_redacted_paths(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(validate_selector_path(path) for path in value)

    @model_validator(mode="after")
    def validate_relationships(self) -> ProviderProfile:
        if any(key != operation.semantic_name for key, operation in self.operations.items()):
            raise ValueError("operation mapping key must match semantic name")
        if any(key != write.semantic_name for key, write in self.writes.items()):
            raise ValueError("write mapping key must match semantic name")
        for object_profile in self.objects.values():
            if object_profile.discover_operation not in self.operations:
                raise ValueError("object references unknown discover operation")
            if object_profile.fetch_operation not in self.operations:
                raise ValueError("object references unknown fetch operation")
        return self


class ProviderBinding(McpProfileModel):
    """A local server's exact names for every profile semantic capability."""

    profile_id: ProfileText
    profile_version: ProfileText
    tools: dict[ProfileText, ProfileText]
    resources: dict[ProfileText, ProfileText]
    actor_principals: dict[ProfileText, frozenset[ProfileText]]

    @field_validator("profile_id", "profile_version")
    @classmethod
    def validate_id_and_version(cls, value: str) -> str:
        return _require_text(value, "binding profile identifier")

    @field_validator("tools", "resources", mode="after")
    @classmethod
    def freeze_names(cls, value: dict[str, str]) -> dict[str, str]:
        return cast(
            dict[str, str],
            MappingProxyType(
                {
                    _require_text(semantic_name, "semantic operation name"): _require_text(
                        provider_name, "provider capability name"
                    )
                    for semantic_name, provider_name in value.items()
                }
            ),
        )

    @field_serializer("tools", "resources")
    def serialize_names(self, value: dict[str, str]) -> dict[str, str]:
        return dict(value)

    @field_validator("actor_principals", mode="after")
    @classmethod
    def freeze_principals(cls, value: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
        frozen: dict[str, frozenset[str]] = {}
        for actor, principals in value.items():
            frozen[_require_text(actor, "binding actor identifier")] = frozenset(
                _require_text(principal, "binding principal identifier") for principal in principals
            )
        return cast(dict[str, frozenset[str]], MappingProxyType(frozen))

    @field_serializer("actor_principals")
    def serialize_principals(self, value: dict[str, frozenset[str]]) -> dict[str, list[str]]:
        return {actor: sorted(principals) for actor, principals in value.items()}

    def validate_against(self, profile: ProviderProfile) -> None:
        """Require one exact local mapping for each declared semantic capability."""
        if self.profile_id != profile.id:
            raise BindingValidationError("profile identifier mismatch")
        if self.profile_version != profile.version:
            raise BindingValidationError("profile version mismatch")
        expected_tools = {
            name for name, operation in profile.operations.items() if operation.kind == "tool"
        } | set(profile.writes)
        expected_resources = {
            name for name, operation in profile.operations.items() if operation.kind == "resource"
        }
        provided = set(self.tools) | set(self.resources)
        expected = expected_tools | expected_resources
        if provided - expected:
            raise BindingValidationError("unknown operation mapping")
        if set(self.tools) & expected_resources or set(self.resources) & expected_tools:
            raise BindingValidationError("operation mapping has wrong capability kind")
        if expected - provided:
            raise BindingValidationError("missing operation mapping")
        read_tool_names = {
            self.tools[name]
            for name, operation in profile.operations.items()
            if operation.kind == "tool"
        }
        write_tool_names = {self.tools[name] for name in profile.writes}
        if not read_tool_names.isdisjoint(write_tool_names):
            raise BindingValidationError("read and write capabilities overlap")

    def assert_capabilities(
        self,
        tools: frozenset[str],
        resources: frozenset[str],
        resource_templates: frozenset[str],
    ) -> None:
        """Ensure a discovered server still exposes every locally bound capability."""
        if any(name not in tools for name in self.tools.values()):
            raise BindingValidationError("missing bound tool")
        for name in self.resources.values():
            available = resource_templates if "{" in name or "}" in name else resources
            if name not in available:
                raise BindingValidationError("missing bound resource")


def profile_schema_bytes() -> bytes:
    """Return canonical bytes for the checked-in provider profile schema."""
    return (
        json.dumps(
            ProviderProfile.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    ).encode("utf-8")
