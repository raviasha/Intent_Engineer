"""Immutable project configuration and source checkpoint records."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import PurePosixPath
from types import MappingProxyType

from pydantic import ConfigDict, Field, field_serializer, field_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.models.source_roles import SourceRoleAssignment

_DEFAULT_CONTEXT_LIMITS = {
    "relevant_intent": 10,
    "relevant_requirements": 10,
    "decisions": 10,
    "constraints": 10,
    "acceptance_criteria": 10,
    "code_refs": 20,
    "test_refs": 20,
    "open_reconciliation_cases": 10,
    "evidence_refs": 20,
}

MAX_TEST_COMMANDS = 16
MAX_TEST_COMMAND_ARGUMENTS = 128
MAX_TEST_CONFIGURATION_ITEM_BYTES = 4096
MAX_TEST_COMMAND_BYTES = 16 * 1024
MAX_TEST_RESULT_PATHS = 16
_SHELL_METACHARACTERS = frozenset(";&|`$<>*?{}[]()!\\")
_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


def _safe_relative_path(value: str) -> PurePosixPath:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > MAX_TEST_CONFIGURATION_ITEM_BYTES
        or "\x00" in value
        or "\\" in value
    ):
        raise ValueError("invalid reviewed test path")
    path = PurePosixPath(value)
    if (
        value != path.as_posix()
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0].casefold() == "dev"
        or any(part.split(".", 1)[0].casefold() in _DEVICE_NAMES for part in path.parts)
    ):
        raise ValueError("invalid reviewed test path")
    return path


class SyncCheckpoint(StrictModel):
    """The durable source cursor and semantic-consumption boundary for one connector."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    connector_id: str
    cursor: str | None
    committed_at: datetime
    consumption_schema_version: int = Field(default=1, ge=1, le=1)
    consumed_evidence_ids: tuple[str, ...] = ()

    @field_validator("consumed_evidence_ids")
    @classmethod
    def validate_consumed_evidence_ids(cls, evidence_ids: tuple[str, ...]) -> tuple[str, ...]:
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("consumed evidence ids must be unique")
        return evidence_ids


class ProjectConfig(StrictModel):
    """Local project settings that affect deterministic application behavior."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    project_id: str
    graph_path: str = ".intent/graph.yaml"
    local_actor: str
    source_exclusions: tuple[str, ...] = ()
    auto_apply_metadata: bool = True
    auto_apply_semantic: bool = False
    source_roles: tuple[SourceRoleAssignment, ...] = ()
    context_limits: Mapping[str, int] = Field(default_factory=lambda: dict(_DEFAULT_CONTEXT_LIMITS))
    test_commands: tuple[tuple[str, ...], ...] = ()
    test_result_paths: tuple[str, ...] = ()

    @field_validator("source_roles")
    @classmethod
    def normalize_source_roles(
        cls, source_roles: tuple[SourceRoleAssignment, ...]
    ) -> tuple[SourceRoleAssignment, ...]:
        normalized = tuple(sorted(source_roles, key=lambda item: (item.connector_id, item.scope)))
        pairs = tuple((item.connector_id, item.scope) for item in normalized)
        if len(pairs) != len(set(pairs)):
            raise ValueError("duplicate source role assignment")
        return normalized

    @field_serializer("source_roles")
    def serialize_source_roles(
        self, source_roles: tuple[SourceRoleAssignment, ...]
    ) -> list[dict[str, object]]:
        return [assignment.model_dump(mode="json") for assignment in source_roles]

    @field_validator("context_limits")
    @classmethod
    def freeze_context_limits(cls, context_limits: Mapping[str, int]) -> Mapping[str, int]:
        return MappingProxyType(dict(context_limits))

    @field_serializer("context_limits")
    def serialize_context_limits(self, context_limits: Mapping[str, int]) -> dict[str, int]:
        return dict(context_limits)

    @field_validator("test_commands")
    @classmethod
    def validate_test_commands(
        cls, commands: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        if len(commands) > MAX_TEST_COMMANDS or len(commands) != len(set(commands)):
            raise ValueError("invalid reviewed test commands")
        total_bytes = 0
        for command in commands:
            if (
                type(command) is not tuple
                or not command
                or len(command) > MAX_TEST_COMMAND_ARGUMENTS
            ):
                raise ValueError("invalid reviewed test command")
            for argument in command:
                if (
                    type(argument) is not str
                    or not argument
                    or len(argument.encode("utf-8")) > MAX_TEST_CONFIGURATION_ITEM_BYTES
                    or any(character in argument for character in ("\x00", "\r", "\n"))
                    or any(character in argument for character in _SHELL_METACHARACTERS)
                ):
                    raise ValueError("invalid reviewed test command")
                total_bytes += len(argument.encode("utf-8")) + 1
            _safe_relative_path(command[0])
        if total_bytes > MAX_TEST_COMMAND_BYTES:
            raise ValueError("invalid reviewed test commands")
        return commands

    @field_validator("test_result_paths")
    @classmethod
    def validate_test_result_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        if len(paths) > MAX_TEST_RESULT_PATHS or len(paths) != len(set(paths)):
            raise ValueError("invalid reviewed test result paths")
        for path in paths:
            _safe_relative_path(path)
        return paths
