"""Immutable reconciliation observations, cases, and audit events."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from intent_engineering.core.models.enums import (
    ReconciliationCaseType,
    ReconciliationStatus,
    ResolutionAction,
    SourceMode,
)
from intent_engineering.core.models.graph import Confidence

ALLOWED_CASE_TRANSITIONS: dict[ReconciliationStatus, frozenset[ReconciliationStatus]] = {
    ReconciliationStatus.OPEN: frozenset(
        {
            ReconciliationStatus.PROPOSED,
            ReconciliationStatus.DEFERRED,
            ReconciliationStatus.FALSE_POSITIVE,
        }
    ),
    ReconciliationStatus.PROPOSED: frozenset({ReconciliationStatus.NEEDS_HUMAN}),
    ReconciliationStatus.NEEDS_HUMAN: frozenset({ReconciliationStatus.RESOLVED}),
    ReconciliationStatus.RESOLVED: frozenset(),
    ReconciliationStatus.DEFERRED: frozenset(),
    ReconciliationStatus.FALSE_POSITIVE: frozenset(),
}


class EvidenceSide(BaseModel):
    """One independently attributable position used to classify drift."""

    model_config = ConfigDict(frozen=True)

    label: str
    claim: str
    evidence_refs: tuple[str, ...]
    observed_at: datetime
    authors: tuple[str, ...]
    confidence: Confidence
    source_mode: SourceMode = SourceMode.EXPLICIT
    current: bool = True

    @field_validator("evidence_refs")
    @classmethod
    def require_evidence_refs(cls, evidence_refs: tuple[str, ...]) -> tuple[str, ...]:
        if not evidence_refs or any(not reference.strip() for reference in evidence_refs):
            raise ValueError("evidence_refs must not be empty")
        return evidence_refs

    @field_validator("authors")
    @classmethod
    def require_authors(cls, authors: tuple[str, ...]) -> tuple[str, ...]:
        if not authors or any(not author.strip() for author in authors):
            raise ValueError("authors must not be empty")
        return authors


ReconciliationEvidenceSide = EvidenceSide


class DriftObservation(BaseModel):
    """A deterministic detector result before durable case creation."""

    model_config = ConfigDict(frozen=True)

    subject_ref: str
    case_type: ReconciliationCaseType
    affected_refs: tuple[str, ...]
    evidence_sides: tuple[EvidenceSide, ...]
    detector_id: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    requires_human: bool = True

    @model_validator(mode="after")
    def require_evidence(self) -> DriftObservation:
        if not any(side.evidence_refs for side in self.evidence_sides):
            raise ValueError("drift observation requires evidence")
        return self


class ClassificationEvent(BaseModel):
    """An immutable lifecycle event for a reconciliation case."""

    model_config = ConfigDict(frozen=True)

    actor: str = Field(min_length=1)
    at: datetime
    prior: ReconciliationStatus
    new: ReconciliationStatus


class ReconciliationCase(BaseModel):
    """Durable case packet for a divergence requiring reconciliation."""

    model_config = ConfigDict(frozen=True)

    id: str
    subject_ref: str
    case_type: ReconciliationCaseType
    affected_refs: tuple[str, ...]
    evidence_sides: tuple[EvidenceSide, ...]
    detector_id: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    status: ReconciliationStatus = ReconciliationStatus.OPEN
    requires_human: bool = True
    alternatives: tuple[ReconciliationCaseType, ...] = ()
    impact: str = ""
    resolution: ResolutionAction | None = None
    resolved_by_changeset: str | None = None
    history: tuple[ClassificationEvent, ...] = ()

    @property
    def all_evidence_refs(self) -> tuple[str, ...]:
        """Return every evidence reference exactly once in stable order."""
        return tuple(sorted({ref for side in self.evidence_sides for ref in side.evidence_refs}))

    @model_validator(mode="after")
    def require_resolution_for_resolved_case(self) -> ReconciliationCase:
        if not self.all_evidence_refs:
            raise ValueError("reconciliation case requires evidence")
        expected_status = ReconciliationStatus.OPEN
        for event in self.history:
            if event.prior is not expected_status:
                raise ValueError("case history must be contiguous")
            if event.new not in ALLOWED_CASE_TRANSITIONS[event.prior]:
                raise ValueError(f"invalid reconciliation transition: {event.prior} -> {event.new}")
            expected_status = event.new
        if self.status is not expected_status:
            raise ValueError("case status must match lifecycle history")
        if self.status is ReconciliationStatus.RESOLVED and (
            self.resolution is None or self.resolved_by_changeset is None
        ):
            raise ValueError("resolved case requires resolution and changeset")
        if self.status is not ReconciliationStatus.RESOLVED and (
            self.resolution is not None or self.resolved_by_changeset is not None
        ):
            raise ValueError("only resolved case can include resolution evidence")
        return self
