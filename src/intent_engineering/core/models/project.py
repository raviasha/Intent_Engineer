"""Immutable project configuration and source checkpoint records."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

from pydantic import ConfigDict, Field, field_serializer, field_validator

from intent_engineering.core.models._base import StrictModel

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
    context_limits: Mapping[str, int] = Field(default_factory=lambda: dict(_DEFAULT_CONTEXT_LIMITS))

    @field_validator("context_limits")
    @classmethod
    def freeze_context_limits(cls, context_limits: Mapping[str, int]) -> Mapping[str, int]:
        return MappingProxyType(dict(context_limits))

    @field_serializer("context_limits")
    def serialize_context_limits(self, context_limits: Mapping[str, int]) -> dict[str, int]:
        return dict(context_limits)
