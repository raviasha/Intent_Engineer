"""Immutable project configuration and source checkpoint records."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

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


class SyncCheckpoint(BaseModel):
    """The durable cursor reached by one connector."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    connector_id: str
    cursor: str | None
    committed_at: datetime


class ProjectConfig(BaseModel):
    """Local project settings that affect deterministic application behavior."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    project_id: str
    graph_path: str = ".intent/graph.yaml"
    local_actor: str
    source_exclusions: tuple[str, ...] = ()
    auto_apply_metadata: bool = True
    auto_apply_semantic: bool = False
    context_limits: Mapping[str, int] = Field(default_factory=lambda: dict(_DEFAULT_CONTEXT_LIMITS))

    @field_validator("context_limits")
    @classmethod
    def freeze_context_limits(cls, context_limits: Mapping[str, int]) -> Mapping[str, int]:
        return MappingProxyType(dict(context_limits))

    @field_serializer("context_limits")
    def serialize_context_limits(self, context_limits: Mapping[str, int]) -> dict[str, int]:
        return dict(context_limits)
