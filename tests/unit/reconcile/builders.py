"""Fixed, explicit inputs for deterministic reconciliation tests."""

from datetime import UTC, datetime

from intent_engineering.core.models import EvidenceSide, SourceMode
from intent_engineering.reconcile.detectors import DetectionInput

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def side(
    label: str,
    *,
    version: int,
    author: str = "author@example.com",
    claim: str | None = None,
    source_mode: SourceMode = SourceMode.EXPLICIT,
    current: bool = True,
) -> EvidenceSide:
    return EvidenceSide(
        label=label,
        claim=claim or label,
        evidence_refs=(f"ev-{label}-{version}",),
        observed_at=NOW,
        authors=(author,),
        confidence=0.8,
        source_mode=source_mode,
        current=current,
    )


def code_lag_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="req-export",
        affected_refs=("req-export", "symbol-export"),
        requirement=side("requirement", version=2),
        implementation=side("implementation", version=1),
        test=side("test", version=1),
        requirement_version=2,
        implementation_version=1,
        test_version=1,
        compatibility="contradicts",
    )


def requirement_lag_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="req-export",
        affected_refs=("req-export", "symbol-export", "test-export"),
        requirement=side("requirement", version=1),
        implementation=side("implementation", version=3),
        test=side("test", version=3),
        decision=side("decision", version=3),
        requirement_version=1,
        implementation_version=3,
        test_version=3,
        decision_version=3,
        compatibility="aligns",
    )


def test_lag_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="req-export",
        affected_refs=("req-export", "symbol-export", "test-export"),
        requirement=side("requirement", version=1),
        implementation=side("implementation", version=3),
        test=side("test", version=2),
        requirement_version=1,
        implementation_version=3,
        test_version=2,
        compatibility="aligns",
    )


def undocumented_code_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="symbol-unmapped",
        affected_refs=("symbol-unmapped",),
        implementation=side("implementation", version=2),
        implementation_version=2,
        test_version=0,
        compatibility="unknown",
        has_mapped_semantics=False,
        material_code_change=True,
    )


def ambiguous_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="req-export",
        affected_refs=("req-export", "symbol-export"),
        requirement=side("requirement", version=2),
        implementation=side("implementation", version=2),
        requirement_version=2,
        implementation_version=2,
        test_version=0,
        compatibility="unknown",
    )


def cross_author_conflict_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="req-export",
        affected_refs=("req-export",),
        requirement=side("requirement", version=2, author="product@example.com"),
        decision=side("decision", version=2, author="architecture@example.com"),
        requirement_version=2,
        implementation_version=0,
        test_version=0,
        decision_version=2,
        compatibility="contradicts",
    )


def precedence_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="req-export",
        affected_refs=("req-export", "symbol-export", "test-export"),
        requirement=side("requirement", version=4, author="product@example.com"),
        implementation=side("implementation", version=3, author="engineering@example.com"),
        test=side("test", version=1, author="engineering@example.com"),
        decision=side("decision", version=5, author="architecture@example.com"),
        requirement_version=4,
        implementation_version=3,
        test_version=1,
        decision_version=5,
        compatibility="contradicts",
    )
