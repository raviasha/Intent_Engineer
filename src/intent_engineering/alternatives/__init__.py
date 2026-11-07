"""Grounded, proposal-only alternatives for intent-aligned requirements."""

from intent_engineering.alternatives.grounding import (
    AlternativeRequestBuilder,
    AlternativeSubjectUnavailable,
)
from intent_engineering.alternatives.models import (
    AlternativeComparison,
    AlternativeEvidenceSummary,
    AlternativeMode,
    AlternativeRequest,
    AlternativeResult,
    AlternativeStatus,
    AlternativeUnavailable,
    ComplexityEstimate,
    ComplexityLevel,
    ConstraintEffect,
    ConstraintEffectKind,
    DimensionScoreDelta,
    GraphEffect,
    GraphEffectKind,
    OutcomeEffect,
    RequirementAlternative,
    RequirementAlternativeCandidate,
)
from intent_engineering.alternatives.validation import (
    AlternativeValidationError,
    AlternativeValidator,
)

__all__ = [
    "AlternativeComparison",
    "AlternativeEvidenceSummary",
    "AlternativeMode",
    "AlternativeRequest",
    "AlternativeRequestBuilder",
    "AlternativeResult",
    "AlternativeStatus",
    "AlternativeSubjectUnavailable",
    "AlternativeUnavailable",
    "AlternativeValidationError",
    "AlternativeValidator",
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
