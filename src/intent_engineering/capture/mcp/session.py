"""Strict local configuration and the provider-neutral asynchronous MCP port."""

from __future__ import annotations

import re
from collections.abc import Mapping
from math import isfinite
from pathlib import PurePath
from types import MappingProxyType
from typing import Annotated, Literal, Protocol, cast
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from intent_engineering.capture.mcp.profile_models import ProviderBinding
from intent_engineering.core.models import JsonValue
from intent_engineering.core.models._base import StrictModel

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ENV_REFERENCE = re.compile(r"^env:[A-Z_][A-Z0-9_]{0,127}$")
_MAX_NAME = 256
_MAX_ARGUMENTS = 128
_MAX_HEADERS = 64
_MAX_SCOPE_KEYS = 128
_SHELL_COMMANDS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "cmd", "cmd.exe", "powershell", "pwsh"}
)
_SHELL_ARGUMENTS = frozenset(
    {"-c", "/c", "-command", "/command", "--command", "-encodedcommand", "--encodedcommand"}
)
_COMBINED_SHELL_SWITCH = re.compile(r"^-[lc]{2,8}$", re.IGNORECASE)

type PublicName = Annotated[str, Field(min_length=1, max_length=_MAX_NAME)]


def _public_name(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > _MAX_NAME or _CONTROL.search(value):
        raise ValueError(f"invalid {label}")
    return value


def _environment_reference(value: object) -> str:
    if type(value) is not str or _ENV_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid environment reference")
    return value


def _freeze_json(value: object) -> JsonValue:
    """Detach exactly built-in, finite JSON; never invoke arbitrary mapping protocols."""
    if type(value) is dict:
        frozen: dict[str, JsonValue] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError("invalid MCP JSON")
            frozen[key] = _freeze_json(item)
        return cast(JsonValue, MappingProxyType(frozen))
    if type(value) is list:
        return cast(JsonValue, tuple(_freeze_json(item) for item in cast(list[object], value)))
    if value is None or type(value) in {str, bool, int}:
        return cast(JsonValue, value)
    if type(value) is float and isfinite(value):
        return cast(JsonValue, value)
    raise ValueError("invalid MCP JSON")


def thaw_json(value: object) -> JsonValue:
    """Return a fresh built-in JSON value or reject a non-exact boundary value."""
    if type(value) is MappingProxyType:
        return {key: thaw_json(item) for key, item in cast(Mapping[str, object], value).items()}
    if type(value) is tuple:
        return [thaw_json(item) for item in cast(tuple[object, ...], value)]
    if type(value) is dict:
        return {key: thaw_json(item) for key, item in cast(dict[str, object], value).items()}
    if type(value) is list:
        return [thaw_json(item) for item in cast(list[object], value)]
    if value is None or type(value) in {str, bool, int}:
        return cast(JsonValue, value)
    if type(value) is float and isfinite(value):
        return cast(JsonValue, value)
    raise ValueError("invalid MCP JSON")


def detached_json(value: object) -> JsonValue:
    """Validate and copy one public JSON value without retaining caller aliases."""
    return thaw_json(_freeze_json(value))


class McpSession(Protocol):
    """The small testable port shared by local and HTTP MCP transports."""

    async def list_tools(self) -> frozenset[str]: ...

    async def list_resources(self) -> frozenset[str]: ...

    async def call_tool(self, name: str, arguments: dict[str, JsonValue]) -> JsonValue: ...

    async def read_resource(self, uri: str) -> JsonValue: ...

    async def close(self) -> None: ...


class _StrictConfig(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class McpServerConfig(_StrictConfig):
    """One local stdio or streamable-HTTP MCP endpoint, without resolved secrets."""

    id: PublicName
    transport: Literal["stdio", "streamable_http"]
    command: str | None = None
    args: tuple[str, ...] = Field(default=(), validate_default=False)
    url: str | None = None
    environment_refs: dict[PublicName, str] = Field(default_factory=dict)
    headers: dict[PublicName, str] = Field(default_factory=dict)
    timeout_seconds: float = 30.0

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _public_name(value, "MCP server identifier")

    @field_validator("command", mode="before")
    @classmethod
    def validate_command(cls, value: object) -> str | None:
        if value is None:
            return None
        command = _public_name(value, "MCP command")
        if any(char.isspace() for char in command) or PurePath(command).name.casefold() in _SHELL_COMMANDS:
            raise ValueError("invalid MCP command")
        return command

    @field_validator("args", mode="before")
    @classmethod
    def validate_args(cls, value: object) -> tuple[str, ...]:
        if type(value) is not list or len(cast(list[object], value)) > _MAX_ARGUMENTS:
            raise ValueError("invalid MCP argv")
        args = tuple(_public_name(item, "MCP argv") for item in cast(list[object], value))
        if any(
            item.casefold() in _SHELL_ARGUMENTS or _COMBINED_SHELL_SWITCH.fullmatch(item) for item in args
        ):
            raise ValueError("invalid MCP argv")
        return args

    @field_validator("url", mode="before")
    @classmethod
    def validate_url(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or _CONTROL.search(value) or len(value) > 2048:
            raise ValueError("invalid MCP URL")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("invalid MCP URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.query:
            raise ValueError("invalid MCP URL")
        return value

    @field_validator("environment_refs", "headers", mode="before")
    @classmethod
    def reject_nonexact_references(cls, value: object) -> object:
        if type(value) is not dict:
            raise ValueError("invalid MCP environment references")
        return value

    @field_validator("environment_refs", "headers", mode="after")
    @classmethod
    def freeze_references(cls, value: dict[str, str]) -> dict[str, str]:
        if type(value) is not dict or len(value) > _MAX_HEADERS:
            raise ValueError("invalid MCP environment references")
        frozen: dict[str, str] = {}
        seen_lower: set[str] = set()
        for key, item in value.items():
            public_key = _public_name(key, "MCP header or environment name")
            if public_key.lower() in seen_lower:
                raise ValueError("duplicate MCP header or environment name")
            seen_lower.add(public_key.lower())
            frozen[public_key] = _environment_reference(item)
        return cast(dict[str, str], MappingProxyType(frozen))

    @field_serializer("environment_refs", "headers")
    def serialize_references(self, value: dict[str, str]) -> dict[str, str]:
        return dict(value)

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def validate_timeout(cls, value: object) -> float:
        if type(value) not in {int, float} or type(value) is bool:
            raise ValueError("invalid MCP timeout")
        timeout = float(cast(int | float, value))
        if not isfinite(timeout) or timeout <= 0 or timeout > 300:
            raise ValueError("invalid MCP timeout")
        return timeout

    @model_validator(mode="after")
    def validate_transport_shape(self) -> McpServerConfig:
        if self.transport == "stdio":
            if self.command is None or self.url is not None or self.headers:
                raise ValueError("invalid stdio MCP configuration")
        elif self.url is None or self.command is not None or self.args or self.environment_refs:
            raise ValueError("invalid streamable HTTP MCP configuration")
        return self


class McpConnectorConfig(_StrictConfig):
    """A local profile/binding selection plus bounded public connector scope."""

    id: PublicName
    profile_path: str
    server: McpServerConfig
    binding: ProviderBinding
    scope: dict[PublicName, JsonValue] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _public_name(value, "MCP connector identifier")

    @field_validator("profile_path", mode="before")
    @classmethod
    def validate_profile_path(cls, value: object) -> str:
        path = _public_name(value, "MCP profile path")
        if PurePath(path).is_absolute() or ".." in PurePath(path).parts:
            raise ValueError("invalid MCP profile path")
        return path

    @field_validator("scope", mode="after")
    @classmethod
    def freeze_scope(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if type(value) is not dict or len(value) > _MAX_SCOPE_KEYS:
            raise ValueError("invalid MCP scope")
        return cast(
            dict[str, JsonValue],
            MappingProxyType({_public_name(key, "MCP scope key"): _freeze_json(item) for key, item in value.items()}),
        )

    @field_validator("scope", mode="before")
    @classmethod
    def reject_nonexact_scope(cls, value: object) -> object:
        if type(value) is not dict or len(cast(dict[object, object], value)) > _MAX_SCOPE_KEYS:
            raise ValueError("invalid MCP scope")
        for key, item in cast(dict[object, object], value).items():
            _public_name(key, "MCP scope key")
            _freeze_json(item)
        return value

    @field_serializer("scope")
    def serialize_scope(self, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {key: thaw_json(item) for key, item in value.items()}
