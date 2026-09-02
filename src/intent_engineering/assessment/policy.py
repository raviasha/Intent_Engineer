"""Versioned policy for deterministic assessment semantics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
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

from intent_engineering.assessment.models import AssessmentDimension
from intent_engineering.core.models import NodeType, RelationType
from intent_engineering.core.models._base import StrictModel


class AssessmentPolicy(StrictModel):
    """Immutable, digest-addressed policy that defines one rubric evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    schema_version: Literal[1] = 1
    rubric_version: Literal["rubric:v1"] = "rubric:v1"
    red_below: int = 50
    green_at: int = 75
    green_confidence_at: int = 75
    dimension_weights: Mapping[AssessmentDimension, int]
    default_branch_weight: int = 1
    branch_weights: Mapping[str, int] = Field(default_factory=dict)
    critical_node_types: tuple[NodeType, ...]
    critical_relations: tuple[RelationType, ...]

    @classmethod
    def v1(cls) -> AssessmentPolicy:
        """Return the complete, explicit policy for rubric version 1."""
        return cls(
            dimension_weights={dimension: 1 for dimension in AssessmentDimension},
            critical_node_types=(
                NodeType.PRODUCT_INTENT,
                NodeType.DESIRED_OUTCOME,
                NodeType.CONSTRAINT,
                NodeType.REQUIREMENT,
                NodeType.ACCEPTANCE_CRITERION,
            ),
            critical_relations=(
                RelationType.MOTIVATES,
                RelationType.SEEKS_OUTCOME,
                RelationType.CONSTRAINS,
                RelationType.REFINES,
                RelationType.SPECIFIED_BY,
                RelationType.HAS_ACCEPTANCE_CRITERION,
            ),
        )

    @field_validator(
        "schema_version",
        "red_below",
        "green_at",
        "green_confidence_at",
        "default_branch_weight",
        mode="before",
    )
    @classmethod
    def require_exact_integers(cls, value: object, info: ValidationInfo) -> int:
        if type(value) is not int:
            raise ValueError(f"{info.field_name} must be an exact integer")
        return value

    @field_validator("dimension_weights", mode="before")
    @classmethod
    def require_exact_weights(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError(  # noqa: TRY004 - Pydantic wraps ValueError
                "dimension_weights must be a mapping"
            )
        for dimension, weight in value.items():
            if not isinstance(dimension, AssessmentDimension) and type(dimension) is not str:
                raise ValueError("dimension_weights must use assessment dimensions")
            if type(weight) is not int:
                raise ValueError("dimension weights must be exact integers")
        return dict(value)

    @field_validator("dimension_weights")
    @classmethod
    def freeze_weights(
        cls, value: Mapping[AssessmentDimension, int]
    ) -> Mapping[AssessmentDimension, int]:
        if set(value) != set(AssessmentDimension):
            raise ValueError("dimension weights must cover every assessment dimension")
        if any(weight <= 0 for weight in value.values()):
            raise ValueError("dimension weights must be positive")
        ordered = {
            dimension: value[dimension] for dimension in sorted(value, key=lambda item: item.value)
        }
        return MappingProxyType(ordered)

    @field_serializer("dimension_weights")
    def serialize_weights(
        self, value: Mapping[AssessmentDimension, int]
    ) -> dict[AssessmentDimension, int]:
        return dict(value)

    @field_validator("branch_weights", mode="before")
    @classmethod
    def require_exact_branch_weights(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError(  # noqa: TRY004 - Pydantic wraps ValueError
                "branch_weights must be a mapping"
            )
        for branch_id, weight in value.items():
            if type(branch_id) is not str or not branch_id:
                raise ValueError("branch weights must use exact branch identifiers")
            if type(weight) is not int:
                raise ValueError("branch weights must be exact integers")
        return dict(value)

    @field_validator("branch_weights")
    @classmethod
    def freeze_branch_weights(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        if any(weight <= 0 for weight in value.values()):
            raise ValueError("branch weights must be positive")
        return MappingProxyType({branch_id: value[branch_id] for branch_id in sorted(value)})

    @field_serializer("branch_weights")
    def serialize_branch_weights(self, value: Mapping[str, int]) -> dict[str, int]:
        return dict(value)

    @field_validator("critical_node_types")
    @classmethod
    def canonicalize_critical_node_types(cls, value: tuple[NodeType, ...]) -> tuple[NodeType, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("critical_relations")
    @classmethod
    def canonicalize_critical_relations(
        cls, value: tuple[RelationType, ...]
    ) -> tuple[RelationType, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @model_validator(mode="after")
    def validate_thresholds(self) -> AssessmentPolicy:
        if not 0 <= self.red_below < self.green_at <= 100:
            raise ValueError("health thresholds must satisfy 0 <= red_below < green_at <= 100")
        if not 0 <= self.green_confidence_at <= 100:
            raise ValueError("green confidence threshold must be within 0 to 100")
        if self.default_branch_weight <= 0:
            raise ValueError("default branch weight must be positive")
        return self

    def canonical_bytes(self) -> bytes:
        """Return the canonical JSON policy representation used for its digest."""
        return json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return the stable version-and-content identity for this policy."""
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"
