"""Immutable, provider-neutral task-context records."""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from intent_engineering.core.models.graph import Confidence


class ContextItem(BaseModel):
    """A concise, display-safe projection of a semantic graph item."""

    model_config = ConfigDict(frozen=True)

    id: str
    type: str
    label: str
    confidence: Confidence | None
    evidence_refs: Sequence[str]

    @field_validator("evidence_refs")
    @classmethod
    def freeze_evidence_refs(cls, evidence_refs: Sequence[str]) -> Sequence[str]:
        return tuple(evidence_refs)


class ContextPack(BaseModel):
    """Deterministic context selected for a task or symbol query."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = "1"
    task: str
    relevant_intent: Sequence[ContextItem]
    relevant_requirements: Sequence[ContextItem]
    decisions: Sequence[ContextItem]
    constraints: Sequence[ContextItem]
    acceptance_criteria: Sequence[ContextItem]
    code_refs: Sequence[ContextItem]
    test_refs: Sequence[ContextItem]
    open_reconciliation_cases: Sequence[ContextItem]
    evidence_refs: Sequence[str]
    warnings: Sequence[str]

    @field_validator(
        "relevant_intent",
        "relevant_requirements",
        "decisions",
        "constraints",
        "acceptance_criteria",
        "code_refs",
        "test_refs",
        "open_reconciliation_cases",
        "evidence_refs",
        "warnings",
    )
    @classmethod
    def freeze_sequences(cls, values: Sequence[object]) -> Sequence[object]:
        return tuple(values)
