"""Behavioral contracts for the versioned assessment policy."""

import hashlib

import pytest
from pydantic import ValidationError

from intent_engineering.assessment.models import AssessmentDimension
from intent_engineering.assessment.policy import AssessmentPolicy


def test_v1_policy_has_explicit_thresholds_equal_default_weights_and_digest() -> None:
    """Catches policy versions that silently alter assessment semantics."""
    policy = AssessmentPolicy.v1()

    assert (policy.red_below, policy.green_at, policy.green_confidence_at) == (50, 75, 75)
    assert set(policy.dimension_weights.values()) == {1}
    assert policy.digest == "sha256:" + hashlib.sha256(policy.canonical_bytes()).hexdigest()


def test_policy_normalizes_reference_order_and_rejects_boolean_weights() -> None:
    """Catches policy identity changing with input order or truthy numeric weights."""
    policy = AssessmentPolicy.v1()
    reversed_dimensions = dict(reversed(tuple(policy.dimension_weights.items())))

    normalized = AssessmentPolicy(
        dimension_weights=reversed_dimensions,
        critical_node_types=tuple(reversed(policy.critical_node_types)),
        critical_relations=tuple(reversed(policy.critical_relations)),
    )

    assert tuple(normalized.dimension_weights) == tuple(sorted(AssessmentDimension, key=str))
    assert normalized.critical_node_types == tuple(sorted(set(policy.critical_node_types), key=str))
    assert normalized.critical_relations == tuple(sorted(set(policy.critical_relations), key=str))
    with pytest.raises(ValidationError, match="exact integer"):
        AssessmentPolicy(
            dimension_weights={dimension: True for dimension in AssessmentDimension},
            critical_node_types=(),
            critical_relations=(),
        )


def test_policy_publishes_canonical_default_and_custom_branch_weights() -> None:
    """Catches custom branch rollup weights being absent from policy identity or reports."""
    baseline = AssessmentPolicy.v1()
    custom = AssessmentPolicy.model_validate(
        {
            **baseline.model_dump(),
            "branch_weights": {"intent:z": 3, "intent:a": 2},
        }
    )

    assert baseline.default_branch_weight == 1
    assert dict(baseline.branch_weights) == {}
    assert tuple(custom.branch_weights) == ("intent:a", "intent:z")
    assert dict(custom.branch_weights) == {"intent:a": 2, "intent:z": 3}
    assert custom.digest != baseline.digest
    with pytest.raises(TypeError):
        custom.branch_weights["intent:a"] = 7  # type: ignore[index]


def test_policy_rejects_non_mapping_weights_as_a_validation_error() -> None:
    """Catches public policy validation leaking a raw TypeError for malformed input."""
    policy = AssessmentPolicy.v1()

    with pytest.raises(ValidationError, match="dimension_weights must be a mapping"):
        AssessmentPolicy.model_validate({**policy.model_dump(), "dimension_weights": ()})
