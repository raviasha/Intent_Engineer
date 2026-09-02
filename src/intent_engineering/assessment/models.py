"""Strict, immutable records for detached graph assessment output."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import (
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from intent_engineering.core.models import (
    ChangeSet,
    EvidenceIngestion,
    EvidenceRecord,
    Graph,
    NodeType,
    ReconciliationCase,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow import ClarificationEvent

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class AssessmentDimension(StrEnum):
    """The fixed, explainable dimensions of rubric version 1."""

    INTENT_CLARITY = "intent_clarity"
    EVIDENCE_STRENGTH = "evidence_strength"
    REQUIREMENT_COVERAGE = "requirement_coverage"
    IMPLEMENTATION_TRACEABILITY = "implementation_traceability"
    TEST_VERIFICATION = "test_verification"
    CONSISTENCY = "consistency"
    FRESHNESS = "freshness"


class AssessmentHealth(StrEnum):
    """Visible health states for an assessment result."""

    GREEN = "green"
    ORANGE = "orange"
    RED = "red"
    UNASSESSED = "unassessed"


class DimensionApplicability(StrEnum):
    """Whether a dimension contributes directly, indirectly, or not at all."""

    REQUIRED = "required"
    INHERITED = "inherited"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "not_applicable"


class _AssessmentModel(StrictModel):
    """Fail-closed base for public detached assessment records."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


def _exact_string(value: object, *, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be an exact non-empty string")
    return value


def _exact_score(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an exact integer")
    return value


def _canonical_references(value: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if any(type(item) is not str or not item for item in value):
        raise ValueError(f"{label} must contain exact non-empty strings")
    return tuple(sorted(set(value)))


def _exact_weight_mapping(value: object, *, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")  # noqa: TRY004 - Pydantic wraps ValueError
    copied: dict[str, int] = {}
    for reference, weight in value.items():
        copied[_exact_string(reference, label=f"{label} key")] = _exact_score(
            weight, label=f"{label} value"
        )
    return copied


def _canonical_contribution_weights(
    value: Mapping[str, int],
    *,
    contributor_ids: tuple[str, ...],
    label: str,
) -> Mapping[str, int]:
    weights = dict(value) or {identifier: 1 for identifier in contributor_ids}
    if set(weights) != set(contributor_ids):
        raise ValueError(f"{label} must exactly cover contributors")
    if any(weight <= 0 for weight in weights.values()):
        raise ValueError(f"{label} must be positive")
    return MappingProxyType({identifier: weights[identifier] for identifier in sorted(weights)})


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class RubricCheck(_AssessmentModel):
    """One named, grounded rubric check and any deduction it applies."""

    rule_id: str
    points: int = Field(ge=0, le=100)
    severity: AssessmentHealth
    explanation: str
    references: tuple[str, ...] = ()

    @field_validator("rule_id", "explanation", mode="before")
    @classmethod
    def require_exact_text(cls, value: object, info: ValidationInfo) -> str:
        return _exact_string(value, label=str(info.field_name))

    @field_validator("points", mode="before")
    @classmethod
    def require_exact_points(cls, value: object) -> int:
        return _exact_score(value, label="points")

    @field_validator("references")
    @classmethod
    def canonicalize_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_references(value, label="references")


class DimensionResult(_AssessmentModel):
    """The complete, explainable evaluation for one assessment dimension."""

    dimension: AssessmentDimension
    applicability: DimensionApplicability
    score: int | None = Field(default=None, ge=0, le=100)
    health: AssessmentHealth
    confidence: int | None = Field(default=None, ge=0, le=100)
    passed: tuple[RubricCheck, ...] = ()
    failed: tuple[RubricCheck, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    related_refs: tuple[str, ...] = ()
    recommended_next_action: str | None = None

    @field_validator("score", "confidence", mode="before")
    @classmethod
    def require_exact_optional_scores(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return None
        return _exact_score(value, label=str(info.field_name))

    @field_validator("recommended_next_action", mode="before")
    @classmethod
    def require_exact_optional_action(cls, value: object) -> object:
        if value is None:
            return None
        return _exact_string(value, label="recommended_next_action")

    @field_validator("passed", "failed")
    @classmethod
    def canonicalize_checks(cls, value: tuple[RubricCheck, ...]) -> tuple[RubricCheck, ...]:
        if len({item.rule_id for item in value}) != len(value):
            raise ValueError("rubric checks must have unique rule identifiers")
        return tuple(sorted(value, key=lambda item: item.rule_id))

    @field_validator("evidence_refs", "related_refs")
    @classmethod
    def canonicalize_refs(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        return _canonical_references(value, label=str(info.field_name))

    @model_validator(mode="after")
    def validate_applicability(self) -> DimensionResult:
        if self.applicability is DimensionApplicability.NOT_APPLICABLE:
            if self.score is not None or self.confidence is not None:
                raise ValueError("not applicable dimensions cannot have a score or confidence")
            if self.health is not AssessmentHealth.UNASSESSED:
                raise ValueError("not applicable dimensions must be unassessed")
            if self.passed or self.failed:
                raise ValueError("not applicable dimensions cannot contain rubric checks")
            return self
        if self.score is None or self.confidence is None:
            raise ValueError("applicable dimensions require a score and confidence")
        if self.health is AssessmentHealth.UNASSESSED:
            raise ValueError("applicable dimensions cannot be unassessed")
        return self


class NodeScorecard(_AssessmentModel):
    """One visible graph node's detached robustness assessment."""

    node_id: str
    node_type: NodeType | str
    robustness: int | None = Field(default=None, ge=0, le=100)
    confidence: int | None = Field(default=None, ge=0, le=100)
    health: AssessmentHealth
    worst_dimension: AssessmentDimension | None = None
    dimensions: tuple[DimensionResult, ...]
    blocking_case_refs: tuple[str, ...] = ()
    recommended_next_action: str | None = None
    projected_robustness: int | None = Field(default=None, ge=0, le=100)
    projected_confidence: int | None = Field(default=None, ge=0, le=100)

    @field_validator("node_id", mode="before")
    @classmethod
    def require_exact_node_id(cls, value: object) -> str:
        return _exact_string(value, label="node_id")

    @field_validator("node_type", mode="before")
    @classmethod
    def normalize_node_type(cls, value: object) -> NodeType | str:
        if isinstance(value, NodeType):
            return value
        text = _exact_string(value, label="node_type")
        return NodeType(text) if text in NodeType._value2member_map_ else text

    @field_validator(
        "robustness", "confidence", "projected_robustness", "projected_confidence", mode="before"
    )
    @classmethod
    def require_exact_optional_score(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return None
        return _exact_score(value, label=str(info.field_name))

    @field_validator("dimensions")
    @classmethod
    def canonicalize_dimensions(
        cls, value: tuple[DimensionResult, ...]
    ) -> tuple[DimensionResult, ...]:
        if len({item.dimension for item in value}) != len(value):
            raise ValueError("dimensions must have unique identifiers")
        return tuple(sorted(value, key=lambda item: item.dimension.value))

    @field_validator("blocking_case_refs")
    @classmethod
    def canonicalize_case_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_references(value, label="blocking_case_refs")

    @field_validator("recommended_next_action", mode="before")
    @classmethod
    def require_exact_action(cls, value: object) -> object:
        if value is None:
            return None
        return _exact_string(value, label="recommended_next_action")

    @model_validator(mode="after")
    def validate_scorecard(self) -> NodeScorecard:
        if self.health is AssessmentHealth.UNASSESSED:
            if self.robustness is not None or self.confidence is not None:
                raise ValueError("unassessed nodes cannot have a score or confidence")
            if self.worst_dimension is not None:
                raise ValueError("unassessed nodes cannot have a worst dimension")
            return self
        if self.robustness is None or self.confidence is None or self.worst_dimension is None:
            raise ValueError("assessed nodes require score, confidence, and worst dimension")
        if self.worst_dimension not in {item.dimension for item in self.dimensions}:
            raise ValueError("worst dimension must be present in dimensions")
        return self

    def dimension(self, dimension: AssessmentDimension) -> DimensionResult:
        """Return one explicitly evaluated dimension by stable identifier."""
        for result in self.dimensions:
            if result.dimension is dimension:
                return result
        raise KeyError(dimension)


class BranchScorecard(_AssessmentModel):
    """A critical intent branch rollup over visible node scorecards."""

    branch_id: str
    root_node_id: str
    node_ids: tuple[str, ...]
    robustness: int | None = Field(default=None, ge=0, le=100)
    confidence: int | None = Field(default=None, ge=0, le=100)
    health: AssessmentHealth
    contribution_weights: Mapping[str, int] = Field(default_factory=dict)

    @field_validator("branch_id", "root_node_id", mode="before")
    @classmethod
    def require_exact_ids(cls, value: object, info: ValidationInfo) -> str:
        return _exact_string(value, label=str(info.field_name))

    @field_validator("node_ids")
    @classmethod
    def canonicalize_node_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_references(value, label="node_ids")

    @field_validator("robustness", "confidence", mode="before")
    @classmethod
    def require_exact_scores(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return None
        return _exact_score(value, label=str(info.field_name))

    @field_validator("contribution_weights", mode="before")
    @classmethod
    def require_exact_contribution_weights(cls, value: object) -> dict[str, int]:
        return _exact_weight_mapping(value, label="contribution_weights")

    @field_validator("contribution_weights")
    @classmethod
    def canonicalize_contribution_weights(
        cls, value: Mapping[str, int], info: ValidationInfo
    ) -> Mapping[str, int]:
        node_ids = info.data.get("node_ids")
        if not isinstance(node_ids, tuple):
            raise ValueError(  # noqa: TRY004 - Pydantic wraps ValueError
                "contribution weights require node identifiers"
            )
        return _canonical_contribution_weights(
            value, contributor_ids=node_ids, label="contribution_weights"
        )

    @field_serializer("contribution_weights")
    def serialize_contribution_weights(self, value: Mapping[str, int]) -> dict[str, int]:
        return dict(value)

    @model_validator(mode="after")
    def validate_rollup(self) -> BranchScorecard:
        if self.health is AssessmentHealth.UNASSESSED:
            if self.robustness is not None or self.confidence is not None:
                raise ValueError("unassessed branches cannot have a score or confidence")
            return self
        if self.robustness is None or self.confidence is None:
            raise ValueError("assessed branches require score and confidence")
        return self


class ProjectScorecard(_AssessmentModel):
    """The project-level rollup over visible critical intent branches."""

    project_id: str
    robustness: int | None = Field(default=None, ge=0, le=100)
    confidence: int | None = Field(default=None, ge=0, le=100)
    health: AssessmentHealth
    branch_ids: tuple[str, ...]
    contributing_node_ids: tuple[str, ...]
    contribution_weights: Mapping[str, int] = Field(default_factory=dict)

    @field_validator("project_id", mode="before")
    @classmethod
    def require_exact_project_id(cls, value: object) -> str:
        return _exact_string(value, label="project_id")

    @field_validator("branch_ids", "contributing_node_ids")
    @classmethod
    def canonicalize_ids(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        return _canonical_references(value, label=str(info.field_name))

    @field_validator("robustness", "confidence", mode="before")
    @classmethod
    def require_exact_scores(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return None
        return _exact_score(value, label=str(info.field_name))

    @field_validator("contribution_weights", mode="before")
    @classmethod
    def require_exact_contribution_weights(cls, value: object) -> dict[str, int]:
        return _exact_weight_mapping(value, label="contribution_weights")

    @field_validator("contribution_weights")
    @classmethod
    def canonicalize_contribution_weights(
        cls, value: Mapping[str, int], info: ValidationInfo
    ) -> Mapping[str, int]:
        branch_ids = info.data.get("branch_ids")
        if not isinstance(branch_ids, tuple):
            raise ValueError(  # noqa: TRY004 - Pydantic wraps ValueError
                "contribution weights require branch identifiers"
            )
        return _canonical_contribution_weights(
            value, contributor_ids=branch_ids, label="contribution_weights"
        )

    @field_serializer("contribution_weights")
    def serialize_contribution_weights(self, value: Mapping[str, int]) -> dict[str, int]:
        return dict(value)

    @model_validator(mode="after")
    def validate_rollup(self) -> ProjectScorecard:
        if self.health is AssessmentHealth.UNASSESSED:
            if self.robustness is not None or self.confidence is not None:
                raise ValueError("unassessed projects cannot have a score or confidence")
            return self
        if self.robustness is None or self.confidence is None:
            raise ValueError("assessed projects require score and confidence")
        return self


class AssessmentReport(_AssessmentModel):
    """A version-bound, detached report with canonically ordered scorecards."""

    schema_version: Literal[1] = 1
    project_id: str
    graph_id: str
    graph_version: int = Field(ge=0)
    graph_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    evidence_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    ingestion_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    case_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    clarification_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    history_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    policy_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    snapshot_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    principal_projection_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    generated_at: datetime
    project: ProjectScorecard
    branches: tuple[BranchScorecard, ...]
    nodes: tuple[NodeScorecard, ...]
    gaps: tuple[RubricCheck, ...] = ()
    warnings: tuple[str, ...] = ()
    assessment_complete: bool

    @field_validator(
        "project_id",
        "graph_id",
        "graph_digest",
        "evidence_digest",
        "ingestion_digest",
        "case_digest",
        "clarification_digest",
        "history_digest",
        "policy_digest",
        "snapshot_digest",
        "principal_projection_digest",
        mode="before",
    )
    @classmethod
    def require_exact_strings(cls, value: object, info: ValidationInfo) -> str:
        return _exact_string(value, label=str(info.field_name))

    @field_validator("schema_version", "graph_version", mode="before")
    @classmethod
    def require_exact_integers(cls, value: object, info: ValidationInfo) -> int:
        return _exact_score(value, label=str(info.field_name))

    @field_validator("branches")
    @classmethod
    def canonicalize_branches(
        cls, value: tuple[BranchScorecard, ...]
    ) -> tuple[BranchScorecard, ...]:
        if len({item.branch_id for item in value}) != len(value):
            raise ValueError("branches must have unique identifiers")
        return tuple(sorted(value, key=lambda item: item.branch_id))

    @field_validator("nodes")
    @classmethod
    def canonicalize_nodes(cls, value: tuple[NodeScorecard, ...]) -> tuple[NodeScorecard, ...]:
        if len({item.node_id for item in value}) != len(value):
            raise ValueError("nodes must have unique identifiers")
        return tuple(sorted(value, key=lambda item: item.node_id))

    @field_validator("gaps")
    @classmethod
    def canonicalize_gaps(cls, value: tuple[RubricCheck, ...]) -> tuple[RubricCheck, ...]:
        if len({item.rule_id for item in value}) != len(value):
            raise ValueError("gaps must have unique rule identifiers")
        return tuple(sorted(value, key=lambda item: item.rule_id))

    @field_validator("warnings")
    @classmethod
    def canonicalize_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_references(value, label="warnings")

    @model_validator(mode="after")
    def validate_visible_reference_closure(self) -> AssessmentReport:
        if self.project.project_id != self.project_id:
            raise ValueError("report project identity must match project scorecard")
        visible_branches = {branch.branch_id for branch in self.branches}
        if set(self.project.branch_ids) != visible_branches:
            raise ValueError("project branch references must exactly match visible branches")
        visible_nodes = {node.node_id for node in self.nodes}
        if not set(self.project.contributing_node_ids).issubset(visible_nodes):
            raise ValueError("project contributors must reference visible nodes")
        for branch in self.branches:
            if branch.root_node_id not in visible_nodes or not set(branch.node_ids).issubset(
                visible_nodes
            ):
                raise ValueError("branch contributors must reference visible nodes")
        return self

    def semantic_bytes(self) -> bytes:
        """Return canonical report content excluding the presentation timestamp."""
        material = self.model_dump(mode="json")
        material.pop("generated_at")
        return _canonical_json(material)

    @property
    def semantic_digest(self) -> str:
        """Return the stable digest of semantic report content."""
        return f"sha256:{hashlib.sha256(self.semantic_bytes()).hexdigest()}"

    def node(self, node_id: str) -> NodeScorecard:
        """Return one visible node scorecard by stable identifier."""
        for scorecard in self.nodes:
            if scorecard.node_id == node_id:
                return scorecard
        raise KeyError(node_id)

    def branch(self, branch_id: str) -> BranchScorecard:
        """Return one visible branch scorecard by stable identifier."""
        for scorecard in self.branches:
            if scorecard.branch_id == branch_id:
                return scorecard
        raise KeyError(branch_id)


class AssessmentSnapshot(_AssessmentModel):
    """An immutable, ACL-filtered preimage consumed by the assessment service."""

    schema_version: Literal[1] = 1
    project_id: str
    graph: Graph
    evidence: tuple[EvidenceRecord, ...]
    ingestions: tuple[EvidenceIngestion, ...]
    cases: tuple[ReconciliationCase, ...]
    clarifications: tuple[ClarificationEvent, ...]
    history: tuple[ChangeSet, ...]
    graph_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    evidence_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    ingestion_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    case_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    clarification_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    history_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    config_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    principal_projection_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    aggregate_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)

    @field_validator(
        "project_id",
        "graph_digest",
        "evidence_digest",
        "ingestion_digest",
        "case_digest",
        "clarification_digest",
        "history_digest",
        "config_digest",
        "principal_projection_digest",
        "aggregate_digest",
        mode="before",
    )
    @classmethod
    def require_exact_strings(cls, value: object, info: ValidationInfo) -> str:
        return _exact_string(value, label=str(info.field_name))

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_exact_schema_version(cls, value: object) -> int:
        return _exact_score(value, label="schema_version")

    @field_validator("evidence")
    @classmethod
    def canonicalize_evidence(cls, value: tuple[EvidenceRecord, ...]) -> tuple[EvidenceRecord, ...]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("evidence must have unique identifiers")
        return tuple(sorted(value, key=lambda item: item.id))

    @field_validator("ingestions")
    @classmethod
    def canonicalize_ingestions(
        cls, value: tuple[EvidenceIngestion, ...]
    ) -> tuple[EvidenceIngestion, ...]:
        if len({(item.connector_id, item.sequence) for item in value}) != len(value):
            raise ValueError("ingestions must have unique connector sequences")
        return tuple(sorted(value, key=lambda item: (item.connector_id, item.sequence)))

    @field_validator("cases")
    @classmethod
    def canonicalize_cases(
        cls, value: tuple[ReconciliationCase, ...]
    ) -> tuple[ReconciliationCase, ...]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("cases must have unique identifiers")
        return tuple(sorted(value, key=lambda item: item.id))

    @field_validator("clarifications")
    @classmethod
    def canonicalize_clarifications(
        cls, value: tuple[ClarificationEvent, ...]
    ) -> tuple[ClarificationEvent, ...]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("clarifications must have unique identifiers")
        return tuple(sorted(value, key=lambda item: item.id))

    @field_validator("history")
    @classmethod
    def canonicalize_history(cls, value: tuple[ChangeSet, ...]) -> tuple[ChangeSet, ...]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("history must have unique identifiers")
        return tuple(sorted(value, key=lambda item: item.id))
