"""Strict contracts for grounded requirement alternatives."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from intent_engineering.alternatives.models import (
    AlternativeComparison,
    AlternativeContextNode,
    AlternativeEvidenceSummary,
    AlternativeMode,
    AlternativeRequest,
    AlternativeResult,
    AlternativeStatus,
    ComplexityEstimate,
    ComplexityLevel,
    ConstraintEffect,
    ConstraintEffectKind,
    GraphEffect,
    GraphEffectKind,
    OutcomeEffect,
    RequirementAlternative,
    RequirementAlternativeCandidate,
)
from intent_engineering.assessment import (
    AssessmentDimension,
    AssessmentHealth,
    DimensionApplicability,
    DimensionResult,
    NodeScorecard,
)
from intent_engineering.core.models import NodeType


def _scorecard(*, robustness: int = 45) -> NodeScorecard:
    dimensions = tuple(
        DimensionResult(
            dimension=dimension,
            applicability=DimensionApplicability.REQUIRED,
            score=robustness,
            health=AssessmentHealth.RED,
            confidence=90,
        )
        for dimension in AssessmentDimension
    )
    return NodeScorecard(
        node_id="req:export",
        node_type=NodeType.REQUIREMENT,
        robustness=robustness,
        confidence=90,
        health=AssessmentHealth.RED,
        worst_dimension=AssessmentDimension.IMPLEMENTATION_TRACEABILITY,
        dimensions=dimensions,
    )


def _request() -> AlternativeRequest:
    return AlternativeRequest.create(
        subject_ref="req:export",
        subject_text="Automate scheduled CSV export.",
        requested_modes=(AlternativeMode.CHANGE_APPROACH, AlternativeMode.SIMPLIFY),
        intent_refs=("intent:portable-report",),
        desired_outcome_refs=("outcome:portable-report",),
        constraint_refs=("constraint:weekly",),
        context_nodes=(
            AlternativeContextNode(
                node_ref="constraint:weekly",
                node_type="constraint",
                label="Export weekly.",
            ),
            AlternativeContextNode(
                node_ref="intent:portable-report",
                node_type="product_intent",
                label="Customers can take reports with them.",
            ),
            AlternativeContextNode(
                node_ref="outcome:portable-report",
                node_type="desired_outcome",
                label="Customers obtain a portable report.",
            ),
        ),
        evidence_summaries=(
            AlternativeEvidenceSummary(
                evidence_ref="evidence:customer-interview",
                summary="A weekly downloadable report is sufficient.",
            ),
        ),
        current_scorecard=_scorecard(),
        snapshot_digest="sha256:" + "1" * 64,
        policy_digest="sha256:" + "2" * 64,
        principal_projection_digest="sha256:" + "3" * 64,
    )


def _candidate() -> RequirementAlternativeCandidate:
    return RequirementAlternativeCandidate(
        subject_ref="req:export",
        proposed_text="An owner exports the report on request.",
        mode=AlternativeMode.CHANGE_APPROACH,
        preserved_intent_refs=("intent:portable-report",),
        preserved_outcome_refs=("outcome:portable-report",),
        weakened_outcomes=(
            OutcomeEffect(
                outcome_ref="outcome:portable-report",
                rationale="The report is still available, but delivery is not automatic.",
            ),
        ),
        constraint_effects=(
            ConstraintEffect(
                constraint_ref="constraint:weekly",
                effect=ConstraintEffectKind.CHANGE,
                proposed_text="Export on request rather than weekly.",
                rationale="This removes scheduler operations.",
            ),
        ),
        supporting_evidence_refs=("evidence:customer-interview",),
        complexity=ComplexityEstimate(
            implementation=ComplexityLevel.LOW,
            operations=ComplexityLevel.LOW,
        ),
        complexity_rationale="Uses an existing export command.",
        risks=("A report may arrive later than a scheduled export.",),
        tradeoffs=("Lower automation cost for operator involvement.",),
        assumptions=("An owner is available when a report is requested.",),
        graph_effects=(
            GraphEffect(
                effect=GraphEffectKind.UPDATE_NODE,
                target_ref="req:export",
                field="statement",
                before="Automate scheduled CSV export.",
                after="An owner exports the report on request.",
            ),
            GraphEffect(
                effect=GraphEffectKind.UPDATE_NODE,
                target_ref="constraint:weekly",
                field="statement",
                before="Export weekly.",
                after="Export on request rather than weekly.",
            ),
        ),
    )


def test_candidate_requires_grounding_tradeoffs_and_graph_effects() -> None:
    with pytest.raises(ValidationError):
        RequirementAlternativeCandidate.model_validate(
            {
                "schema_version": 1,
                "subject_ref": "req:export",
                "proposed_text": "Export manually.",
                "mode": "change_approach",
                "preserved_outcome_refs": (),
                "weakened_outcomes": (),
                "constraint_effects": (),
                "supporting_evidence_refs": (),
                "complexity": {"implementation": "low", "operations": "low"},
                "complexity_rationale": "Shorter.",
                "risks": (),
                "tradeoffs": (),
                "assumptions": (),
                "graph_effects": (),
            }
        )


def test_request_candidate_and_result_have_content_addressed_identities() -> None:
    request = _request()
    candidate = _candidate()
    alternative = RequirementAlternative.from_candidate(
        candidate,
        request_digest=request.digest,
        graph_effect_digest=candidate.activation_effect_digest,
    )
    comparison = AlternativeComparison.create(
        request=request,
        alternative=alternative,
        current_scorecard=request.current_scorecard,
        projected_scorecard=_scorecard(robustness=78),
    )
    result = AlternativeResult.create(
        request=request,
        status=AlternativeStatus.AVAILABLE,
        alternatives=(comparison,),
    )

    assert request.id == f"alternative-request:{request.digest}"
    assert candidate.id == f"alternative-candidate:{candidate.digest}"
    assert alternative.id == f"requirement-alternative:{alternative.digest}"
    assert comparison.id == f"alternative-comparison:{comparison.digest}"
    assert result.id == f"alternative-result:{result.digest}"
    assert result.request_digest == request.digest
    assert RequirementAlternative.model_validate_json(alternative.model_dump_json()) == alternative


def test_reference_collections_are_canonical_and_duplicate_free() -> None:
    material = _candidate().model_dump()
    material["supporting_evidence_refs"] = (
        "evidence:z",
        "evidence:a",
        "evidence:z",
    )

    candidate = RequirementAlternativeCandidate.model_validate(material)

    assert candidate.supporting_evidence_refs == ("evidence:a", "evidence:z")
    with pytest.raises(ValidationError, match="unique graph effects"):
        RequirementAlternativeCandidate.model_validate(
            {**candidate.model_dump(), "graph_effects": candidate.graph_effects * 2}
        )


def test_models_reject_subclasses_booleans_unknown_fields_and_oversized_content() -> None:
    class SneakyString(str):
        pass

    request = _request()
    with pytest.raises(ValidationError, match="exact non-empty string"):
        AlternativeRequest.model_validate(
            {**request.model_dump(), "subject_ref": SneakyString("x")}
        )
    with pytest.raises(ValidationError):
        AlternativeRequest.model_validate({**request.model_dump(), "unknown": True})
    with pytest.raises(ValidationError, match="byte limit"):
        RequirementAlternativeCandidate.model_validate(
            {**_candidate().model_dump(), "proposed_text": "x" * (16 * 1024)}
        )


def test_result_status_is_consistent_with_alternatives() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="available results require alternatives"):
        AlternativeResult.create(
            request=request,
            status=AlternativeStatus.AVAILABLE,
            alternatives=(),
        )
    with pytest.raises(ValidationError, match="unavailable results cannot contain alternatives"):
        AlternativeResult.create(
            request=request,
            status=AlternativeStatus.PROVIDER_UNAVAILABLE,
            alternatives=(
                AlternativeComparison.create(
                    request=request,
                    alternative=RequirementAlternative.from_candidate(
                        _candidate(),
                        request_digest=request.digest,
                        graph_effect_digest=_candidate().activation_effect_digest,
                    ),
                    current_scorecard=request.current_scorecard,
                    projected_scorecard=_scorecard(robustness=78),
                ),
            ),
        )
