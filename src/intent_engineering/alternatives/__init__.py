"""Grounded, proposal-only alternatives for intent-aligned requirements."""

from intent_engineering.alternatives.grounding import (
    AlternativeRequestBuilder,
    AlternativeSubjectUnavailable,
)
from intent_engineering.alternatives.models import (
    AlternativeComparison,
    AlternativeContextNode,
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
from intent_engineering.alternatives.overlay import (
    AlternativeProjectionError,
    apply_alternative_overlay,
)
from intent_engineering.alternatives.proposals import (
    AlternativeActor,
    AlternativeProposalConflictError,
    AlternativeProposalService,
)
from intent_engineering.alternatives.service import (
    AlternativeReasoner,
    RequirementAlternativeService,
)
from intent_engineering.alternatives.validation import (
    AlternativeValidationError,
    AlternativeValidator,
)

__all__ = [
    "AlternativeActor",
    "AlternativeComparison",
    "AlternativeContextNode",
    "AlternativeEvidenceSummary",
    "AlternativeMode",
    "AlternativeProjectionError",
    "AlternativeProposalConflictError",
    "AlternativeProposalService",
    "AlternativeReasoner",
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
    "RequirementAlternativeService",
    "apply_alternative_overlay",
]
