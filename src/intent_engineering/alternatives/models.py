"""Strict, detached contracts for grounded requirement alternatives."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator
from pydantic_core import to_jsonable_python

from intent_engineering.assessment import AssessmentDimension, NodeScorecard
from intent_engineering.core.models._base import StrictModel

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CANDIDATE_BYTE_LIMIT = 16 * 1024
_REQUEST_BYTE_LIMIT = 64 * 1024
_MAX_EFFECTS = 32
_MAX_ALTERNATIVES = 8
_MAX_VISITED_CONTEXT_NODES = 128


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        to_jsonable_python(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _exact_text(value: object, *, label: str, maximum: int = 8 * 1024) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be an exact non-empty string")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds its UTF-8 byte limit")
    return value


def _optional_text(value: object, *, label: str, maximum: int = 8 * 1024) -> str | None:
    if value is None:
        return None
    return _exact_text(value, label=label, maximum=maximum)


def _canonical_refs(value: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    for item in value:
        _exact_text(item, label=label, maximum=512)
    return tuple(sorted(set(value)))


def _canonical_texts(value: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    for item in value:
        _exact_text(item, label=label, maximum=2 * 1024)
    return tuple(sorted(set(value)))


class _AlternativeModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def require_exact_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an exact integer")
        return value


class AlternativeMode(StrEnum):
    """The user-requested shape of an alternative."""

    SIMPLIFY = "simplify"
    CHANGE_APPROACH = "change_approach"
    NARROW_SCOPE = "narrow_scope"
    REUSE_CAPABILITY = "reuse_capability"


class AlternativeStatus(StrEnum):
    """Stable result states at the optional reasoning boundary."""

    AVAILABLE = "available"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_PROVIDER_OUTPUT = "invalid_provider_output"
    SUBJECT_UNAVAILABLE = "subject_unavailable"
    STALE_REQUEST = "stale_request"
    NO_VALID_ALTERNATIVES = "no_valid_alternatives"


class AlternativeUnavailable(StrEnum):
    """Fixed public unavailability reasons used without hidden-state disclosure."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_PROVIDER_OUTPUT = "invalid_provider_output"
    SUBJECT_UNAVAILABLE = "subject_unavailable"
    STALE_REQUEST = "stale_request"
    NO_VALID_ALTERNATIVES = "no_valid_alternatives"


class ComplexityLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConstraintEffectKind(StrEnum):
    ADD = "add"
    CHANGE = "change"
    REMOVE = "remove"


class GraphEffectKind(StrEnum):
    ADD_NODE = "add_node"
    UPDATE_NODE = "update_node"
    REMOVE_NODE = "remove_node"
    ADD_EDGE = "add_edge"
    REMOVE_EDGE = "remove_edge"


class AlternativeEvidenceSummary(_AlternativeModel):
    schema_version: Literal[1] = 1
    evidence_ref: str
    summary: str

    @field_validator("evidence_ref", mode="before")
    @classmethod
    def validate_reference(cls, value: object) -> str:
        return _exact_text(value, label="evidence_ref", maximum=512)

    @field_validator("summary", mode="before")
    @classmethod
    def validate_summary(cls, value: object) -> str:
        return _exact_text(value, label="summary", maximum=2 * 1024)


class AlternativeContextNode(_AlternativeModel):
    """Bounded visible semantic context supplied to an optional reasoner."""

    schema_version: Literal[1] = 1
    node_ref: str
    node_type: str
    label: str

    @field_validator("node_ref", "node_type", "label", mode="before")
    @classmethod
    def validate_text(cls, value: object, info: ValidationInfo) -> str:
        maximum = 4 * 1024 if info.field_name == "label" else 512
        return _exact_text(value, label=str(info.field_name), maximum=maximum)


class ComplexityEstimate(_AlternativeModel):
    schema_version: Literal[1] = 1
    implementation: ComplexityLevel
    operations: ComplexityLevel


class OutcomeEffect(_AlternativeModel):
    """An explicitly weakened desired outcome and why it changes."""

    schema_version: Literal[1] = 1
    outcome_ref: str
    rationale: str

    @field_validator("outcome_ref", mode="before")
    @classmethod
    def validate_reference(cls, value: object) -> str:
        return _exact_text(value, label="outcome_ref", maximum=512)

    @field_validator("rationale", mode="before")
    @classmethod
    def validate_rationale(cls, value: object) -> str:
        return _exact_text(value, label="rationale", maximum=2 * 1024)


class ConstraintEffect(_AlternativeModel):
    """One explicit addition, change, or removal of a constraint."""

    schema_version: Literal[1] = 1
    constraint_ref: str
    effect: ConstraintEffectKind
    proposed_text: str | None = None
    rationale: str

    @field_validator("constraint_ref", mode="before")
    @classmethod
    def validate_reference(cls, value: object) -> str:
        return _exact_text(value, label="constraint_ref", maximum=512)

    @field_validator("proposed_text", mode="before")
    @classmethod
    def validate_proposed_text(cls, value: object) -> str | None:
        return _optional_text(value, label="proposed_text", maximum=2 * 1024)

    @field_validator("rationale", mode="before")
    @classmethod
    def validate_rationale(cls, value: object) -> str:
        return _exact_text(value, label="rationale", maximum=2 * 1024)

    @model_validator(mode="after")
    def validate_effect_shape(self) -> Self:
        if self.effect is ConstraintEffectKind.REMOVE and self.proposed_text is not None:
            raise ValueError("removed constraints cannot have proposed text")
        if self.effect is not ConstraintEffectKind.REMOVE and self.proposed_text is None:
            raise ValueError("added or changed constraints require proposed text")
        return self


class GraphEffect(_AlternativeModel):
    """A bounded declarative graph effect; never an applied mutation."""

    schema_version: Literal[1] = 1
    effect: GraphEffectKind
    target_ref: str
    field: str | None = None
    before: str | None = None
    after: str | None = None

    @field_validator("target_ref", mode="before")
    @classmethod
    def validate_target(cls, value: object) -> str:
        return _exact_text(value, label="target_ref", maximum=512)

    @field_validator("field", "before", "after", mode="before")
    @classmethod
    def validate_optional_text(cls, value: object, info: ValidationInfo) -> str | None:
        return _optional_text(value, label=str(info.field_name), maximum=4 * 1024)

    @model_validator(mode="after")
    def validate_effect_shape(self) -> Self:
        if self.effect is GraphEffectKind.UPDATE_NODE:
            if self.field is None or self.before is None or self.after is None:
                raise ValueError("node updates require field, before, and after")
            if self.before == self.after:
                raise ValueError("node updates must change a value")
        elif self.effect in {GraphEffectKind.ADD_NODE, GraphEffectKind.ADD_EDGE}:
            if self.after is None:
                raise ValueError("add effects require an after value")
        elif self.after is not None:
            raise ValueError("remove effects cannot have an after value")
        return self

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class AlternativeRequest(_AlternativeModel):
    """A bounded, ACL-projected grounding packet for optional reasoning."""

    schema_version: Literal[1] = 1
    id: str
    digest: str = Field(pattern=_DIGEST_RE)
    subject_ref: str
    subject_text: str
    requested_modes: tuple[AlternativeMode, ...]
    intent_refs: tuple[str, ...]
    desired_outcome_refs: tuple[str, ...]
    constraint_refs: tuple[str, ...]
    context_nodes: tuple[AlternativeContextNode, ...]
    evidence_summaries: tuple[AlternativeEvidenceSummary, ...]
    current_scorecard: NodeScorecard
    snapshot_digest: str = Field(pattern=_DIGEST_RE)
    policy_digest: str = Field(pattern=_DIGEST_RE)
    principal_projection_digest: str = Field(pattern=_DIGEST_RE)

    @field_validator("id", "subject_ref", "subject_text", mode="before")
    @classmethod
    def validate_text(cls, value: object, info: ValidationInfo) -> str:
        limit = 8 * 1024 if info.field_name == "subject_text" else 512
        return _exact_text(value, label=str(info.field_name), maximum=limit)

    @field_validator("requested_modes")
    @classmethod
    def canonicalize_modes(cls, value: tuple[AlternativeMode, ...]) -> tuple[AlternativeMode, ...]:
        if not value:
            raise ValueError("requested_modes cannot be empty")
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("intent_refs", "desired_outcome_refs", "constraint_refs")
    @classmethod
    def canonicalize_references(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        return _canonical_refs(value, label=str(info.field_name))

    @field_validator("evidence_summaries")
    @classmethod
    def canonicalize_evidence(
        cls, value: tuple[AlternativeEvidenceSummary, ...]
    ) -> tuple[AlternativeEvidenceSummary, ...]:
        if len(value) > 64:
            raise ValueError("evidence_summaries exceeds item limit")
        if len({item.evidence_ref for item in value}) != len(value):
            raise ValueError("evidence summaries must have unique references")
        return tuple(sorted(value, key=lambda item: item.evidence_ref))

    @field_validator("context_nodes")
    @classmethod
    def canonicalize_context(
        cls, value: tuple[AlternativeContextNode, ...]
    ) -> tuple[AlternativeContextNode, ...]:
        if len(value) > _MAX_VISITED_CONTEXT_NODES:
            raise ValueError("context_nodes exceeds item limit")
        if len({item.node_ref for item in value}) != len(value):
            raise ValueError("context nodes must have unique references")
        return tuple(sorted(value, key=lambda item: item.node_ref))

    @model_validator(mode="after")
    def validate_identity_and_bounds(self) -> Self:
        material = self._identity_material()
        expected_digest = _digest(material)
        if self.digest != expected_digest or self.id != f"alternative-request:{expected_digest}":
            raise ValueError("alternative request identity does not match canonical content")
        if self.current_scorecard.node_id != self.subject_ref:
            raise ValueError("current scorecard must describe the request subject")
        expected_context = (
            set(self.intent_refs) | set(self.desired_outcome_refs) | set(self.constraint_refs)
        )
        if {item.node_ref for item in self.context_nodes} != expected_context:
            raise ValueError("context nodes must exactly describe semantic references")
        if len(_canonical_json(material)) > _REQUEST_BYTE_LIMIT:
            raise ValueError("alternative request exceeds its UTF-8 byte limit")
        return self

    def _identity_material(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key not in {"id", "digest"}
        }

    @classmethod
    def create(cls, **values: object) -> AlternativeRequest:
        material = {"schema_version": 1, **values}
        digest = _digest(material)
        return cls.model_validate(
            {**material, "id": f"alternative-request:{digest}", "digest": digest}
        )


class RequirementAlternativeCandidate(_AlternativeModel):
    """Untrusted provider or host output before local grounding validation."""

    schema_version: Literal[1] = 1
    subject_ref: str
    proposed_text: str
    mode: AlternativeMode
    preserved_intent_refs: tuple[str, ...]
    preserved_outcome_refs: tuple[str, ...]
    weakened_outcomes: tuple[OutcomeEffect, ...]
    constraint_effects: tuple[ConstraintEffect, ...]
    supporting_evidence_refs: tuple[str, ...]
    complexity: ComplexityEstimate
    complexity_rationale: str
    risks: tuple[str, ...]
    tradeoffs: tuple[str, ...]
    assumptions: tuple[str, ...]
    graph_effects: tuple[GraphEffect, ...]

    @field_validator("subject_ref", "proposed_text", "complexity_rationale", mode="before")
    @classmethod
    def validate_text(cls, value: object, info: ValidationInfo) -> str:
        limit = 8 * 1024 if info.field_name == "proposed_text" else 2 * 1024
        return _exact_text(value, label=str(info.field_name), maximum=limit)

    @field_validator("preserved_intent_refs", "preserved_outcome_refs", "supporting_evidence_refs")
    @classmethod
    def canonicalize_references(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        return _canonical_refs(value, label=str(info.field_name))

    @field_validator("risks", "tradeoffs", "assumptions")
    @classmethod
    def canonicalize_text_collections(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        if len(value) > 32:
            raise ValueError(f"{info.field_name} exceeds item limit")
        return _canonical_texts(value, label=str(info.field_name))

    @field_validator("weakened_outcomes")
    @classmethod
    def canonicalize_outcomes(cls, value: tuple[OutcomeEffect, ...]) -> tuple[OutcomeEffect, ...]:
        if len(value) > _MAX_EFFECTS:
            raise ValueError("weakened_outcomes exceeds item limit")
        if len({item.outcome_ref for item in value}) != len(value):
            raise ValueError("weakened outcomes must have unique references")
        return tuple(sorted(value, key=lambda item: item.outcome_ref))

    @field_validator("constraint_effects")
    @classmethod
    def canonicalize_constraints(
        cls, value: tuple[ConstraintEffect, ...]
    ) -> tuple[ConstraintEffect, ...]:
        if len(value) > _MAX_EFFECTS:
            raise ValueError("constraint_effects exceeds item limit")
        if len({item.constraint_ref for item in value}) != len(value):
            raise ValueError("constraint effects must have unique references")
        return tuple(sorted(value, key=lambda item: item.constraint_ref))

    @field_validator("graph_effects")
    @classmethod
    def canonicalize_graph_effects(cls, value: tuple[GraphEffect, ...]) -> tuple[GraphEffect, ...]:
        if len(value) > _MAX_EFFECTS:
            raise ValueError("graph_effects exceeds item limit")
        if len({item.digest for item in value}) != len(value):
            raise ValueError("candidate must contain unique graph effects")
        return tuple(sorted(value, key=lambda item: item.digest))

    @model_validator(mode="after")
    def validate_grounding_and_bounds(self) -> Self:
        if not self.preserved_outcome_refs:
            raise ValueError("candidate must preserve at least one desired outcome")
        if not self.supporting_evidence_refs:
            raise ValueError("candidate must cite supporting evidence")
        if not self.risks or not self.tradeoffs or not self.assumptions:
            raise ValueError("candidate must state risks, tradeoffs, and assumptions")
        if not self.graph_effects:
            raise ValueError("candidate must declare graph effects")
        if len(_canonical_json(self.model_dump(mode="json"))) > _CANDIDATE_BYTE_LIMIT:
            raise ValueError("candidate exceeds its UTF-8 byte limit")
        return self

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))

    @property
    def id(self) -> str:
        return f"alternative-candidate:{self.digest}"

    @property
    def graph_effect_digest(self) -> str:
        return _digest([item.model_dump(mode="json") for item in self.graph_effects])

    @property
    def activation_effect_digest(self) -> str:
        """Digest the complete locally reviewable semantic activation material."""
        return _digest(
            {
                "graph_effects": [item.model_dump(mode="json") for item in self.graph_effects],
                "supporting_evidence_refs": self.supporting_evidence_refs,
            }
        )


class RequirementAlternative(_AlternativeModel):
    """A locally validated candidate bound to one request and detached effect."""

    schema_version: Literal[1] = 1
    candidate: RequirementAlternativeCandidate
    request_digest: str = Field(pattern=_DIGEST_RE)
    graph_effect_digest: str = Field(pattern=_DIGEST_RE)

    @model_validator(mode="after")
    def validate_effect_digest(self) -> Self:
        if self.graph_effect_digest != self.candidate.activation_effect_digest:
            raise ValueError("graph effect digest does not match candidate")
        return self

    @classmethod
    def from_candidate(
        cls,
        candidate: RequirementAlternativeCandidate,
        *,
        request_digest: str,
        graph_effect_digest: str,
    ) -> RequirementAlternative:
        return cls(
            candidate=candidate,
            request_digest=request_digest,
            graph_effect_digest=graph_effect_digest,
        )

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))

    @property
    def id(self) -> str:
        return f"requirement-alternative:{self.digest}"


class DimensionScoreDelta(_AlternativeModel):
    schema_version: Literal[1] = 1
    dimension: AssessmentDimension
    current: int | None = Field(default=None, ge=0, le=100)
    projected: int | None = Field(default=None, ge=0, le=100)
    delta: int | None = Field(default=None, ge=-100, le=100)

    @field_validator("current", "projected", "delta", mode="before")
    @classmethod
    def validate_exact_integers(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return None
        if type(value) is not int:
            raise ValueError(f"{info.field_name} must be an exact integer")
        return value

    @model_validator(mode="after")
    def validate_delta(self) -> Self:
        expected = (
            None
            if self.current is None or self.projected is None
            else self.projected - self.current
        )
        if self.delta != expected:
            raise ValueError("dimension delta does not match scores")
        return self


class AlternativeComparison(_AlternativeModel):
    """Approved-versus-projected comparison for one validated alternative."""

    schema_version: Literal[1] = 1
    current_state: Literal["approved"] = "approved"
    projected_state: Literal["projected"] = "projected"
    request_digest: str = Field(pattern=_DIGEST_RE)
    snapshot_digest: str = Field(pattern=_DIGEST_RE)
    policy_digest: str = Field(pattern=_DIGEST_RE)
    principal_projection_digest: str = Field(pattern=_DIGEST_RE)
    alternative: RequirementAlternative
    current_scorecard: NodeScorecard
    projected_scorecard: NodeScorecard
    dimension_deltas: tuple[DimensionScoreDelta, ...]

    @field_validator("dimension_deltas")
    @classmethod
    def canonicalize_deltas(
        cls, value: tuple[DimensionScoreDelta, ...]
    ) -> tuple[DimensionScoreDelta, ...]:
        if len({item.dimension for item in value}) != len(value):
            raise ValueError("dimension deltas must have unique dimensions")
        return tuple(sorted(value, key=lambda item: item.dimension.value))

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.alternative.request_digest != self.request_digest:
            raise ValueError("comparison request digest does not match alternative")
        if self.current_scorecard.node_id != self.projected_scorecard.node_id:
            raise ValueError("comparison scorecards must describe the same node")
        return self

    @classmethod
    def create(
        cls,
        *,
        request: AlternativeRequest,
        alternative: RequirementAlternative,
        current_scorecard: NodeScorecard,
        projected_scorecard: NodeScorecard,
    ) -> AlternativeComparison:
        current = {item.dimension: item.score for item in current_scorecard.dimensions}
        projected = {item.dimension: item.score for item in projected_scorecard.dimensions}
        dimensions = tuple(sorted(set(current) | set(projected), key=lambda item: item.value))
        deltas = tuple(
            DimensionScoreDelta(
                dimension=dimension,
                current=current.get(dimension),
                projected=projected.get(dimension),
                delta=(
                    None
                    if current.get(dimension) is None or projected.get(dimension) is None
                    else projected[dimension] - current[dimension]  # type: ignore[operator]
                ),
            )
            for dimension in dimensions
        )
        return cls(
            request_digest=request.digest,
            snapshot_digest=request.snapshot_digest,
            policy_digest=request.policy_digest,
            principal_projection_digest=request.principal_projection_digest,
            alternative=alternative,
            current_scorecard=current_scorecard,
            projected_scorecard=projected_scorecard,
            dimension_deltas=deltas,
        )

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))

    @property
    def id(self) -> str:
        return f"alternative-comparison:{self.digest}"


class AlternativeResult(_AlternativeModel):
    """Stable output of one explicit alternative request."""

    schema_version: Literal[1] = 1
    request_digest: str = Field(pattern=_DIGEST_RE)
    status: AlternativeStatus
    alternatives: tuple[AlternativeComparison, ...] = ()

    @field_validator("alternatives")
    @classmethod
    def canonicalize_alternatives(
        cls, value: tuple[AlternativeComparison, ...]
    ) -> tuple[AlternativeComparison, ...]:
        if len(value) > _MAX_ALTERNATIVES:
            raise ValueError("alternatives exceeds item limit")
        if len({item.digest for item in value}) != len(value):
            raise ValueError("alternatives must be semantically unique")
        return tuple(sorted(value, key=lambda item: item.digest))

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status is AlternativeStatus.AVAILABLE and not self.alternatives:
            raise ValueError("available results require alternatives")
        if self.status is not AlternativeStatus.AVAILABLE and self.alternatives:
            raise ValueError("unavailable results cannot contain alternatives")
        if any(item.request_digest != self.request_digest for item in self.alternatives):
            raise ValueError("all alternatives must match the result request")
        return self

    @classmethod
    def create(
        cls,
        *,
        request: AlternativeRequest,
        status: AlternativeStatus,
        alternatives: tuple[AlternativeComparison, ...],
    ) -> AlternativeResult:
        return cls(request_digest=request.digest, status=status, alternatives=alternatives)

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))

    @property
    def id(self) -> str:
        return f"alternative-result:{self.digest}"


__all__ = [
    "AlternativeComparison",
    "AlternativeContextNode",
    "AlternativeEvidenceSummary",
    "AlternativeMode",
    "AlternativeRequest",
    "AlternativeResult",
    "AlternativeStatus",
    "AlternativeUnavailable",
    "ComplexityEstimate",
    "ComplexityLevel",
    "ConstraintEffect",
    "ConstraintEffectKind",
    "DimensionScoreDelta",
    "GraphEffect",
    "GraphEffectKind",
    "OutcomeEffect",
    "RequirementAlternative",
    "RequirementAlternativeCandidate",
]
