"""Untrusted alternatives must account for visible intent and constraints."""

import pytest
from pydantic import ValidationError

from intent_engineering.alternatives.grounding import AlternativeRequestBuilder
from intent_engineering.alternatives.models import (
    AlternativeMode,
    ComplexityEstimate,
    ComplexityLevel,
    ConstraintEffect,
    ConstraintEffectKind,
    GraphEffect,
    GraphEffectKind,
    RequirementAlternativeCandidate,
)
from intent_engineering.alternatives.validation import (
    AlternativeValidationError,
    AlternativeValidator,
)
from tests.unit.alternatives.fixtures import assessment_pair


def _candidate() -> RequirementAlternativeCandidate:
    return RequirementAlternativeCandidate(
        subject_ref="req:export",
        proposed_text="An owner exports the report on request.",
        mode=AlternativeMode.CHANGE_APPROACH,
        preserved_outcome_refs=("outcome:portable-report",),
        weakened_outcomes=(),
        constraint_effects=(
            ConstraintEffect(
                constraint_ref="constraint:weekly",
                effect=ConstraintEffectKind.CHANGE,
                proposed_text="Make the report available on request.",
                rationale="The intent requires availability, not a scheduler.",
            ),
        ),
        supporting_evidence_refs=("evidence:visible",),
        complexity=ComplexityEstimate(
            implementation=ComplexityLevel.LOW,
            operations=ComplexityLevel.LOW,
        ),
        complexity_rationale="Reuses the existing export command.",
        risks=("The owner may not respond immediately.",),
        tradeoffs=("Lower automation cost for a manual service step.",),
        assumptions=("An owner is available for requests.",),
        graph_effects=(
            GraphEffect(
                effect=GraphEffectKind.UPDATE_NODE,
                target_ref="req:export",
                field="statement",
                before="Automate scheduled CSV export.",
                after="An owner exports the report on request.",
            ),
        ),
    )


def _request():
    snapshot, report = assessment_pair()
    return AlternativeRequestBuilder().build(
        snapshot=snapshot,
        report=report,
        subject_ref="req:export",
        requested_modes=(AlternativeMode.CHANGE_APPROACH,),
    )


def test_validator_accepts_grounded_intent_preserving_candidate() -> None:
    request = _request()
    candidate = _candidate()

    alternative = AlternativeValidator().validate(request, candidate)

    assert alternative.request_digest == request.digest
    assert alternative.candidate.preserved_outcome_refs == ("outcome:portable-report",)
    assert alternative.graph_effect_digest == candidate.graph_effect_digest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject_ref", "req:other"),
        ("preserved_outcome_refs", ("outcome:invented",)),
        ("supporting_evidence_refs", ("evidence:hidden",)),
    ],
)
def test_validator_rejects_ungrounded_references(field: str, value: object) -> None:
    candidate = RequirementAlternativeCandidate.model_validate(
        {**_candidate().model_dump(), field: value}
    )

    with pytest.raises(
        AlternativeValidationError, match="alternative candidate invalid"
    ) as captured:
        AlternativeValidator().validate(_request(), candidate)
    assert captured.value.__context__ is None


def test_validator_rejects_graph_effects_that_do_not_match_the_proposed_requirement() -> None:
    candidate = _candidate()
    effect = candidate.graph_effects[0].model_copy(update={"after": "A different requirement."})
    mismatched = RequirementAlternativeCandidate.model_validate(
        {**candidate.model_dump(), "graph_effects": (effect,)}
    )

    with pytest.raises(AlternativeValidationError, match="alternative candidate invalid"):
        AlternativeValidator().validate(_request(), mismatched)


def test_candidate_model_rejects_unknown_effect_shape_before_validation() -> None:
    with pytest.raises(ValidationError):
        GraphEffect.model_validate(
            {
                "effect": "update_node",
                "target_ref": "req:export",
                "field": "statement",
                "before": "old",
            }
        )
