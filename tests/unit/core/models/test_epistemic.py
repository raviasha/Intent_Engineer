from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import (
    ChangeKind,
    ConfidenceChange,
    ImplementationClaim,
    ImplementationStatus,
)

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def confidence_change(**changes: object) -> ConfidenceChange:
    payload: dict[str, object] = {
        "change_id": "confidence-1",
        "timestamp": NOW,
        "actor": "tester",
        "subject_ref": "req-1",
        "change_kind": ChangeKind.REFINE,
        "prior_confidence": 0.5,
        "new_confidence": 0.8,
        "evidence_refs": ("ev-1",),
        "reason": "Reviewed updated repository evidence.",
    }
    payload.update(changes)
    return ConfidenceChange(**payload)


def implementation_claim(**changes: object) -> ImplementationClaim:
    payload: dict[str, object] = {
        "id": "claim-1",
        "status": ImplementationStatus.IMPLEMENTED_BASELINE,
        "requirement_refs": ("req-1",),
        "current_behavior": "Exports run without a cloud account.",
        "code_evidence": ("ev-code-1",),
        "test_evidence": ("ev-test-1",),
        "verified_commit": "0123456789abcdef",
        "verified_at": NOW,
    }
    payload.update(changes)
    return ImplementationClaim(**payload)


def test_confidence_change_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="confidence change requires evidence"):
        confidence_change(evidence_refs=())


def test_confidence_change_requires_distinct_values() -> None:
    with pytest.raises(ValidationError, match="must change"):
        confidence_change(new_confidence=0.5)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requirement_refs", ()),
        ("code_evidence", ()),
        ("verified_commit", None),
        ("verified_at", None),
        ("test_evidence", ()),
    ],
)
def test_implemented_baseline_requires_complete_evidence(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match="implemented_baseline requires"):
        implementation_claim(**{field: value})


def test_implemented_baseline_can_skip_test_evidence_when_not_applicable() -> None:
    claim = implementation_claim(test_evidence=(), test_evidence_required=False)

    assert claim.test_evidence == ()


def test_models_are_frozen() -> None:
    claim = implementation_claim()

    with pytest.raises(ValidationError, match="frozen"):
        claim.status = ImplementationStatus.PARTIAL  # type: ignore[misc]
