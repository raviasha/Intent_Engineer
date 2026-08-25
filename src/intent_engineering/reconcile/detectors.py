"""Pure deterministic drift detectors with explicit precedence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import ConfigDict

from intent_engineering.core.models import (
    DriftObservation,
    EvidenceSide,
    ReconciliationCaseType,
    SourceMode,
)
from intent_engineering.core.models._base import StrictModel

SemanticCompatibility = Literal["aligns", "contradicts", "unknown"]


class DetectionInput(StrictModel):
    """Versioned, evidence-backed facts for a single deterministic comparison."""

    model_config = ConfigDict(frozen=True)

    subject_ref: str
    affected_refs: tuple[str, ...]
    requirement: EvidenceSide | None = None
    implementation: EvidenceSide | None = None
    test: EvidenceSide | None = None
    decision: EvidenceSide | None = None
    requirement_version: int | None = None
    implementation_version: int | None = None
    test_version: int | None = None
    decision_version: int | None = None
    compatibility: SemanticCompatibility
    requirement_active: bool = True
    has_mapped_semantics: bool = True
    material_code_change: bool = False


def _fingerprint(
    detector_id: str,
    subject_ref: str,
    affected_refs: Sequence[str],
    evidence_sides: Sequence[EvidenceSide],
) -> str:
    payload = {
        "detector_id": detector_id,
        "subject_ref": subject_ref,
        "affected_refs": sorted(affected_refs),
        "evidence_refs": sorted({ref for side in evidence_sides for ref in side.evidence_refs}),
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _canonical_affected_refs(affected_refs: Sequence[str]) -> tuple[str, ...]:
    """Return the sole affected-reference representation used by observations and hashes."""
    return tuple(sorted(set(affected_refs)))


def _observation(
    input: DetectionInput,
    case_type: ReconciliationCaseType,
    detector_id: str,
    *sides: EvidenceSide | None,
) -> DriftObservation:
    evidence_sides = tuple(side for side in sides if side is not None)
    affected_refs = _canonical_affected_refs(input.affected_refs)
    return DriftObservation(
        subject_ref=input.subject_ref,
        case_type=case_type,
        affected_refs=affected_refs,
        evidence_sides=evidence_sides,
        detector_id=detector_id,
        fingerprint=_fingerprint(detector_id, input.subject_ref, affected_refs, evidence_sides),
    )


def detect_conflicting_sources(input: DetectionInput) -> DriftObservation | None:
    """Preserve incompatible current explicit positions from distinct authors."""
    candidates = tuple(
        side
        for side in (input.requirement, input.decision, input.implementation, input.test)
        if side is not None and side.current and side.source_mode is SourceMode.EXPLICIT
    )
    authors = {author for side in candidates for author in side.authors}
    if input.compatibility != "contradicts" or len(candidates) < 2 or len(authors) < 2:
        return None
    return _observation(
        input,
        ReconciliationCaseType.CONFLICTING_SOURCES,
        "conflicting_sources",
        *candidates,
    )


def detect_requirement_lag(input: DetectionInput) -> DriftObservation | None:
    """Find old requirements contradicted by newer aligned decision, code, and tests."""
    if (
        input.requirement is None
        or input.implementation is None
        or input.test is None
        or input.decision is None
        or input.requirement_version is None
        or input.implementation_version is None
        or input.test_version is None
        or input.decision_version is None
        or input.compatibility != "aligns"
        or input.decision.source_mode is not SourceMode.EXPLICIT
        or not input.decision.current
        or input.decision_version <= input.requirement_version
        or input.implementation_version < input.decision_version
        or input.test_version < input.decision_version
    ):
        return None
    return _observation(
        input,
        ReconciliationCaseType.REQUIREMENT_LAG,
        "requirement_lag",
        input.requirement,
        input.decision,
        input.implementation,
        input.test,
    )


def detect_code_lag(input: DetectionInput) -> DriftObservation | None:
    """Find active requirements newer than their mapped implementation evidence."""
    if (
        input.requirement is None
        or input.implementation is None
        or input.requirement_version is None
        or input.implementation_version is None
        or not input.requirement_active
        or input.requirement_version <= input.implementation_version
    ):
        return None
    return _observation(
        input,
        ReconciliationCaseType.CODE_LAG,
        "code_lag",
        input.requirement,
        input.implementation,
        input.test,
    )


def detect_test_lag(input: DetectionInput) -> DriftObservation | None:
    """Find active requirements whose implementation outran verifying test evidence."""
    if (
        input.requirement is None
        or input.implementation is None
        or input.test is None
        or input.implementation_version is None
        or input.test_version is None
        or not input.requirement_active
        or input.implementation_version <= input.test_version
    ):
        return None
    return _observation(
        input,
        ReconciliationCaseType.TEST_LAG,
        "test_lag",
        input.requirement,
        input.implementation,
        input.test,
    )


def detect_undocumented_code(input: DetectionInput) -> DriftObservation | None:
    """Find material code without any mapped intent, requirement, or decision."""
    if input.implementation is None or not input.material_code_change or input.has_mapped_semantics:
        return None
    return _observation(
        input,
        ReconciliationCaseType.UNDOCUMENTED_CODE,
        "undocumented_code",
        input.implementation,
    )


def detect_ambiguous_divergence(input: DetectionInput) -> DriftObservation | None:
    """Classify meaningful but under-specified disagreement after specific rules fail."""
    sides = tuple(
        side
        for side in (input.requirement, input.decision, input.implementation, input.test)
        if side is not None
    )
    if input.compatibility != "unknown" or len(sides) < 2:
        return None
    return _observation(
        input,
        ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
        "ambiguous_divergence",
        *sides,
    )


DETECTORS: Sequence[Callable[[DetectionInput], DriftObservation | None]] = (
    detect_conflicting_sources,
    detect_requirement_lag,
    detect_code_lag,
    detect_test_lag,
    detect_undocumented_code,
    detect_ambiguous_divergence,
)


def detect_drift(input: DetectionInput) -> Sequence[DriftObservation]:
    """Run precedence-ordered pure detectors, emitting one case per subject."""
    observations: list[DriftObservation] = []
    classified_subjects: set[str] = set()
    for detector in DETECTORS:
        observation = detector(input)
        if observation is not None and observation.subject_ref not in classified_subjects:
            observations.append(observation)
            classified_subjects.add(observation.subject_ref)
    return tuple(sorted(observations, key=lambda item: (item.case_type.value, item.fingerprint)))
