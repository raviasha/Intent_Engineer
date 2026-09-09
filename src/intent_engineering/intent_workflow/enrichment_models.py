"""Strict detached records for resumable graph-enrichment sessions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from intent_engineering.core.models._base import StrictModel

_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[^\x00-\x20\x7f]{1,256}$"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PROPOSAL_PREIMAGE_NAMES = (
    "acl_policy",
    "cases",
    "config",
    "evidence",
    "graph",
    "history",
    "intent_proposals",
)


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("enrichment timestamp must use UTC")
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("enrichment timestamp must use UTC")
    return value.astimezone(UTC)


class _EnrichmentModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


class EnrichmentSession(_EnrichmentModel):
    """Current durable state of one bounded progressive-enrichment session."""

    schema_version: Literal[1] = 1
    id: Annotated[str, Field(pattern=r"^refine:[^\x00-\x20\x7f]{1,249}$")]
    status: Literal["open", "paused", "complete", "cancelled"]
    focus_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)] | None = None
    budget_minutes: Literal[5, 15, 30] | None = None
    remaining_budget_seconds: Annotated[int, Field(ge=0, le=1800)]
    snapshot_digest: Annotated[str, Field(pattern=_HASH_PATTERN)]
    current_gap_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)] | None = None
    answered_gap_ids: Annotated[tuple[str, ...], Field(max_length=10_000)] = ()
    skipped_gap_ids: Annotated[tuple[str, ...], Field(max_length=10_000)] = ()
    answer_evidence_refs: Annotated[tuple[str, ...], Field(max_length=10_000)] = ()
    started_at: datetime
    updated_at: datetime

    @field_validator("started_at", "updated_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("answered_gap_ids", "skipped_gap_ids")
    @classmethod
    def require_canonical_unique_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 256 or _CONTROL.search(value) for value in values):
            raise ValueError("invalid enrichment identifier")
        if len(values) != len(set(values)):
            raise ValueError("duplicate enrichment identifier")
        return values

    @field_validator("answer_evidence_refs")
    @classmethod
    def require_canonical_evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value.startswith("evidence:") or len(value) > 256 or _CONTROL.search(value)
            for value in values
        ):
            raise ValueError("invalid enrichment evidence reference")
        if len(values) != len(set(values)):
            raise ValueError("duplicate enrichment evidence reference")
        return values

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.focus_id is None and self.budget_minutes is None:
            raise ValueError("enrichment session requires focus or budget")
        maximum = 0 if self.budget_minutes is None else self.budget_minutes * 60
        if self.remaining_budget_seconds > maximum:
            raise ValueError("invalid remaining enrichment budget")
        if self.updated_at < self.started_at:
            raise ValueError("invalid enrichment time order")
        if set(self.answered_gap_ids) & set(self.skipped_gap_ids):
            raise ValueError("enrichment gap cannot be answered and skipped")
        if len(self.answered_gap_ids) != len(self.answer_evidence_refs):
            raise ValueError("enrichment answers require evidence references")
        if self.current_gap_id in {*self.answered_gap_ids, *self.skipped_gap_ids}:
            raise ValueError("resolved enrichment gap cannot remain current")
        if self.status in {"complete", "cancelled"} and self.current_gap_id is not None:
            raise ValueError("terminal enrichment session cannot retain a current gap")
        return self


class EnrichmentEvent(_EnrichmentModel):
    """One immutable, hash-linked enrichment lifecycle event."""

    schema_version: Literal[1] = 1
    sequence: Annotated[int, Field(ge=0, le=1_000_000)]
    predecessor_event_digest: Annotated[str, Field(pattern=_HASH_PATTERN)] | None = None
    event_type: Literal[
        "opened",
        "answered",
        "skipped",
        "paused",
        "resumed",
        "reassessed",
        "completed",
        "cancelled",
    ]
    session: EnrichmentSession
    gap_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)] | None = None
    answer_evidence_ref: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)] | None = None
    reassessment_predecessor_digest: Annotated[str, Field(pattern=_HASH_PATTERN)] | None = None
    operation_id: Annotated[str, Field(pattern=_HASH_PATTERN)] | None = None
    at: datetime

    @field_validator("at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.at != self.session.updated_at:
            raise ValueError("event time must match session update")
        if self.event_type == "opened":
            if self.sequence != 0 or self.predecessor_event_digest is not None:
                raise ValueError("invalid opening event")
        elif self.sequence == 0 or self.predecessor_event_digest is None:
            raise ValueError("enrichment event requires predecessor")
        if self.event_type == "reassessed":
            if (
                self.reassessment_predecessor_digest is None
                or self.gap_id is not None
                or self.answer_evidence_ref is not None
            ):
                raise ValueError("reassessment event requires only its snapshot predecessor")
        elif self.reassessment_predecessor_digest is not None:
            raise ValueError("only reassessment may bind a snapshot predecessor")
        elif self.event_type == "answered":
            if self.gap_id is None or self.answer_evidence_ref is None:
                raise ValueError("answer event requires gap and evidence")
            if not self.answer_evidence_ref.startswith("evidence:"):
                raise ValueError("answer event requires evidence reference")
        elif self.event_type == "skipped":
            if self.gap_id is None or self.answer_evidence_ref is not None:
                raise ValueError("skip event requires only a gap")
        elif self.gap_id is not None or self.answer_evidence_ref is not None:
            raise ValueError("lifecycle event cannot include answer fields")
        return self

    @property
    def digest(self) -> str:
        material = self.model_dump(mode="json")
        if self.reassessment_predecessor_digest is None:
            material.pop("reassessment_predecessor_digest")
        if self.operation_id is None:
            material.pop("operation_id")
        payload = json.dumps(
            material,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class EnrichmentProposalBinding(_EnrichmentModel):
    """Exact enrichment precondition consumed by the governed proposal transaction."""

    schema_version: Literal[1] = 1
    session_id: Annotated[str, Field(pattern=r"^refine:[^\x00-\x20\x7f]{1,249}$")]
    latest_event_digest: Annotated[str, Field(pattern=_HASH_PATTERN)]
    snapshot_digest: Annotated[str, Field(pattern=_HASH_PATTERN)]
    actor: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
    answered_gap_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=256)]
    answer_evidence_refs: Annotated[tuple[str, ...], Field(min_length=1, max_length=256)]
    assessment_preimage_digests: Annotated[
        tuple[tuple[str, Annotated[str, Field(pattern=_HASH_PATTERN)]], ...],
        Field(min_length=7, max_length=7),
    ]

    @field_validator("answered_gap_ids")
    @classmethod
    def require_gap_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 256 or _CONTROL.search(value) for value in values):
            raise ValueError("invalid enrichment proposal gap")
        if len(values) != len(set(values)):
            raise ValueError("duplicate enrichment proposal gap")
        return values

    @field_validator("answer_evidence_refs")
    @classmethod
    def require_evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value.startswith("evidence:") or len(value) > 256 or _CONTROL.search(value)
            for value in values
        ):
            raise ValueError("invalid enrichment proposal evidence")
        if len(values) != len(set(values)):
            raise ValueError("duplicate enrichment proposal evidence")
        return values

    @model_validator(mode="after")
    def align_answers(self) -> Self:
        if len(self.answered_gap_ids) != len(self.answer_evidence_refs):
            raise ValueError("enrichment proposal answers do not align")
        if tuple(name for name, _digest in self.assessment_preimage_digests) != (
            _PROPOSAL_PREIMAGE_NAMES
        ):
            raise ValueError("invalid enrichment proposal preimages")
        return self


__all__ = ["EnrichmentEvent", "EnrichmentProposalBinding", "EnrichmentSession"]
