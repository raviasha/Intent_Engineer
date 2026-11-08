"""Provider-neutral generation and projection for explicit requirement alternatives."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from intent_engineering.alternatives.models import (
    AlternativeMode,
    AlternativeStatus,
)
from intent_engineering.alternatives.service import RequirementAlternativeService
from intent_engineering.assessment import AssessmentSnapshot
from tests.unit.alternatives.fixtures import assessment_pair
from tests.unit.alternatives.test_validation import _candidate


class RecordingReasoner:
    def __init__(self) -> None:
        self.request = None

    def suggest(self, request):
        self.request = request
        return (_candidate(),)


class FailingReasoner:
    def suggest(self, request):
        del request
        raise RuntimeError("PROVIDER-SECRET-SENTINEL")


class CancellingReasoner:
    def suggest(self, request):
        del request
        raise KeyboardInterrupt


@dataclass
class SnapshotLoader:
    snapshot: AssessmentSnapshot

    def __call__(self) -> AssessmentSnapshot:
        return self.snapshot


def _service(reasoner=None):
    snapshot, _report = assessment_pair()
    loader = SnapshotLoader(snapshot)
    return RequirementAlternativeService(snapshot_loader=loader, reasoner=reasoner), loader


def test_alternative_projection_uses_the_project_assessment_scope():
    from intent_engineering.assessment.models import AssessmentDimension, DimensionApplicability

    service, loader = _service(RecordingReasoner())
    loader.snapshot = loader.snapshot.model_copy(update={"assessment_scope": "intent_requirements"})
    result = service.request(subject_ref="req:export", modes=(AlternativeMode.CHANGE_APPROACH,))
    assert result.status is AlternativeStatus.AVAILABLE
    for scorecard in (result.alternatives[0].current_scorecard,
                      result.alternatives[0].projected_scorecard):
        assert scorecard.dimension(AssessmentDimension.TEST_VERIFICATION).applicability == DimensionApplicability.NOT_APPLICABLE


def test_manual_alternative_preserves_outcome_and_gets_hypothetical_scores() -> None:
    reasoner = RecordingReasoner()
    service, loader = _service(reasoner)
    approved_bytes = loader.snapshot.graph.model_dump_json()

    result = service.request(
        subject_ref="req:export",
        modes=(AlternativeMode.CHANGE_APPROACH,),
    )

    assert result.status is AlternativeStatus.AVAILABLE
    comparison = result.alternatives[0]
    assert comparison.alternative.candidate.proposed_text == (
        "An owner exports the report on request."
    )
    assert comparison.alternative.candidate.preserved_outcome_refs == ("outcome:portable-report",)
    assert comparison.current_state == "approved"
    assert comparison.projected_state == "projected"
    assert loader.snapshot.graph.model_dump_json() == approved_bytes
    assert reasoner.request is not None
    assert "PRIVATE-SENTINEL" not in reasoner.request.model_dump_json()


def test_absent_or_failing_provider_returns_fixed_unavailable_without_affecting_assessment() -> (
    None
):
    absent, absent_loader = _service(None)
    failing, failing_loader = _service(FailingReasoner())
    absent_before = absent_loader.snapshot.graph.model_dump_json()
    failing_before = failing_loader.snapshot.graph.model_dump_json()

    absent_result = absent.request(subject_ref="req:export")
    failing_result = failing.request(subject_ref="req:export")

    assert absent_result.status is AlternativeStatus.PROVIDER_UNAVAILABLE
    assert failing_result.status is AlternativeStatus.PROVIDER_UNAVAILABLE
    assert "PROVIDER-SECRET-SENTINEL" not in failing_result.model_dump_json()
    assert absent_loader.snapshot.graph.model_dump_json() == absent_before
    assert failing_loader.snapshot.graph.model_dump_json() == failing_before


def test_validation_rejects_stale_request_before_projecting() -> None:
    service, loader = _service(RecordingReasoner())
    result = service.request(
        subject_ref="req:export",
        modes=(AlternativeMode.CHANGE_APPROACH,),
    )
    request_id = service.last_request_id
    assert request_id is not None
    loader.snapshot = loader.snapshot.model_copy(update={"aggregate_digest": "sha256:" + "d" * 64})

    comparison = service.validate_candidate(request_id=request_id, candidate=_candidate())

    assert comparison is None
    assert result.alternatives


def test_identical_generation_is_deterministic_and_deduplicated() -> None:
    class DuplicateReasoner:
        def suggest(self, request):
            del request
            return (_candidate(), _candidate())

    service, _loader = _service(DuplicateReasoner())

    first = service.request(subject_ref="req:export", modes=(AlternativeMode.CHANGE_APPROACH,))
    second = service.request(subject_ref="req:export", modes=(AlternativeMode.CHANGE_APPROACH,))

    assert len(first.alternatives) == 1
    assert first.model_dump_json() == second.model_dump_json()


def test_reasoner_cancellation_preserves_signal_identity() -> None:
    service, _loader = _service(CancellingReasoner())

    with pytest.raises(KeyboardInterrupt):
        service.request(subject_ref="req:export")


def test_host_validation_rejects_non_model_candidates() -> None:
    service, _loader = _service(RecordingReasoner())
    service.request(subject_ref="req:export", modes=(AlternativeMode.CHANGE_APPROACH,))
    request_id = service.last_request_id
    assert request_id is not None

    assert (
        service.validate_candidate(
            request_id=request_id,
            candidate={"subject_ref": "req:export"},  # type: ignore[arg-type]
        )
        is None
    )
