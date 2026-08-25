"""Fixed, explicit inputs for deterministic reconciliation tests."""

from datetime import UTC, datetime

from intent_engineering.core.models import EvidenceSide, SourceMode
from intent_engineering.reconcile.detectors import DetectionInput

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
EARLIER = datetime(2026, 8, 24, 12, tzinfo=UTC)
LATEST = datetime(2026, 8, 26, 12, tzinfo=UTC)


def side(
    label: str,
    *,
    version: int,
    author: str = "author@example.com",
    claim: str | None = None,
    source_mode: SourceMode = SourceMode.EXPLICIT,
    current: bool = True,
    observed_at: datetime = NOW,
) -> EvidenceSide:
    return EvidenceSide(
        label=label,
        claim=claim or label,
        evidence_refs=(f"ev-{label}-{version}",),
        observed_at=observed_at,
        authors=(author,),
        confidence=0.8,
        source_mode=source_mode,
        current=current,
    )


def code_lag_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="req-export",
        affected_refs=("req-export", "symbol-export"),
        requirement=side("requirement", version=2, observed_at=LATEST),
        implementation=side("implementation", version=1, observed_at=EARLIER),
        test=side("test", version=1, observed_at=EARLIER),
        compatibility="contradicts",
    )


def requirement_lag_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="req-export",
        affected_refs=("req-export", "symbol-export", "test-export"),
        requirement=side("requirement", version=1, observed_at=EARLIER),
        decision=side("decision", version=3, observed_at=NOW),
        implementation=side("implementation", version=3, observed_at=LATEST),
        test=side("test", version=3, observed_at=LATEST),
        compatibility="aligns",
    )


def test_lag_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="req-export",
        affected_refs=("req-export", "symbol-export", "test-export"),
        requirement=side("requirement", version=1, observed_at=EARLIER),
        implementation=side("implementation", version=3, observed_at=LATEST),
        test=side("test", version=2, observed_at=NOW),
        compatibility="aligns",
    )


def undocumented_code_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="symbol-unmapped",
        affected_refs=("symbol-unmapped",),
        implementation=side("implementation", version=2),
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
        compatibility="unknown",
    )


def cross_author_conflict_input() -> DetectionInput:
    return DetectionInput(
        subject_ref="req-export",
        affected_refs=("req-export",),
        requirement=side("requirement", version=2, author="product@example.com"),
        decision=side("decision", version=2, author="architecture@example.com"),
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
        compatibility="contradicts",
    )
