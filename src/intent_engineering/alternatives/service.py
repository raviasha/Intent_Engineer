"""Provider-neutral generation and detached assessment of requirement alternatives."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

from intent_engineering.alternatives.grounding import AlternativeRequestBuilder
from intent_engineering.alternatives.models import (
    AlternativeComparison,
    AlternativeMode,
    AlternativeRequest,
    AlternativeResult,
    AlternativeStatus,
    RequirementAlternativeCandidate,
)
from intent_engineering.alternatives.overlay import apply_alternative_overlay
from intent_engineering.alternatives.validation import AlternativeValidator
from intent_engineering.assessment import (
    AssessmentPolicy,
    AssessmentSnapshot,
    GraphAssessmentService,
)

_MAX_CONTEXTS = 32


class AlternativeReasoner(Protocol):
    """Optional provider boundary that receives only one detached request."""

    def suggest(
        self,
        request: AlternativeRequest,
    ) -> tuple[RequirementAlternativeCandidate, ...]: ...


@dataclass(frozen=True)
class _RequestContext:
    request: AlternativeRequest
    snapshot: AssessmentSnapshot


class RequirementAlternativeService:
    """Generate, validate, deduplicate, and project explicit requirement alternatives."""

    def __init__(
        self,
        *,
        snapshot_loader: Callable[[], AssessmentSnapshot],
        reasoner: AlternativeReasoner | None = None,
        assessment: GraphAssessmentService | None = None,
        policy: AssessmentPolicy | None = None,
        request_builder: AlternativeRequestBuilder | None = None,
        validator: AlternativeValidator | None = None,
    ) -> None:
        self._snapshot_loader = snapshot_loader
        self._reasoner = reasoner
        self._assessment = assessment or GraphAssessmentService()
        self._policy = policy
        self._request_builder = request_builder or AlternativeRequestBuilder()
        self._validator = validator or AlternativeValidator()
        self._contexts: OrderedDict[str, _RequestContext] = OrderedDict()
        self._comparisons: OrderedDict[str, AlternativeComparison] = OrderedDict()
        self._comparison_requests: dict[str, str] = {}
        self._last_request_id: str | None = None
        self._lock = RLock()

    @property
    def last_request_id(self) -> str | None:
        """Expose the most recent local request identity for interactive adapters."""
        with self._lock:
            return self._last_request_id

    def _store_context(self, context: _RequestContext) -> None:
        with self._lock:
            self._contexts[context.request.id] = context
            self._contexts.move_to_end(context.request.id)
            self._last_request_id = context.request.id
            while len(self._contexts) > _MAX_CONTEXTS:
                expired_request_id, _expired = self._contexts.popitem(last=False)
                expired_comparisons = tuple(
                    comparison_id
                    for comparison_id, request_id in self._comparison_requests.items()
                    if request_id == expired_request_id
                )
                for comparison_id in expired_comparisons:
                    self._comparisons.pop(comparison_id, None)
                    self._comparison_requests.pop(comparison_id, None)

    def _store_comparison(self, comparison: AlternativeComparison, request_id: str) -> None:
        with self._lock:
            self._comparisons[comparison.id] = comparison
            self._comparisons.move_to_end(comparison.id)
            self._comparison_requests[comparison.id] = request_id
            while len(self._comparisons) > _MAX_CONTEXTS * 8:
                expired_id, _expired = self._comparisons.popitem(last=False)
                self._comparison_requests.pop(expired_id, None)

    def _new_context(
        self,
        *,
        subject_ref: str,
        modes: tuple[AlternativeMode, ...],
    ) -> _RequestContext:
        snapshot = self._snapshot_loader()
        report = self._assessment.assess(snapshot, self._policy)
        request = self._request_builder.build(
            snapshot=snapshot,
            report=report,
            subject_ref=subject_ref,
            requested_modes=modes,
        )
        context = _RequestContext(request=request, snapshot=snapshot)
        self._store_context(context)
        return context

    def context(
        self,
        *,
        subject_ref: str,
        modes: tuple[AlternativeMode, ...] = (AlternativeMode.SIMPLIFY,),
    ) -> AlternativeRequest:
        """Build and retain one bounded host-reasoning request without generation."""
        return self._new_context(subject_ref=subject_ref, modes=modes).request

    def request(
        self,
        *,
        subject_ref: str,
        modes: tuple[AlternativeMode, ...] = (AlternativeMode.SIMPLIFY,),
    ) -> AlternativeResult:
        context = self._new_context(subject_ref=subject_ref, modes=modes)
        request = context.request
        if self._reasoner is None:
            return AlternativeResult.create(
                request=request,
                status=AlternativeStatus.PROVIDER_UNAVAILABLE,
                alternatives=(),
            )

        candidates: tuple[RequirementAlternativeCandidate, ...] | None = None
        provider_failed = False
        try:
            raw_candidates = self._reasoner.suggest(request)
            if type(raw_candidates) is not tuple or len(raw_candidates) > 8:
                raise ValueError("invalid provider collection")
            if any(
                type(candidate) is not RequirementAlternativeCandidate
                for candidate in raw_candidates
            ):
                raise ValueError("invalid provider candidate")
            candidates = raw_candidates
        except Exception:  # noqa: BLE001 - optional provider has one fixed unavailable result
            provider_failed = True
        if provider_failed or candidates is None:
            return AlternativeResult.create(
                request=request,
                status=AlternativeStatus.PROVIDER_UNAVAILABLE,
                alternatives=(),
            )

        comparisons: dict[str, AlternativeComparison] = {}
        invalid_output = False
        for candidate in candidates:
            comparison = self._compare(context, candidate)
            if comparison is None:
                invalid_output = True
                continue
            self._store_comparison(comparison, context.request.id)
            comparisons[comparison.alternative.candidate.digest] = comparison
        if comparisons:
            return AlternativeResult.create(
                request=request,
                status=AlternativeStatus.AVAILABLE,
                alternatives=tuple(comparisons[key] for key in sorted(comparisons)),
            )
        return AlternativeResult.create(
            request=request,
            status=(
                AlternativeStatus.INVALID_PROVIDER_OUTPUT
                if invalid_output
                else AlternativeStatus.NO_VALID_ALTERNATIVES
            ),
            alternatives=(),
        )

    def _compare(
        self,
        context: _RequestContext,
        candidate: RequirementAlternativeCandidate,
    ) -> AlternativeComparison | None:
        try:
            alternative = self._validator.validate(context.request, candidate)
            graph = apply_alternative_overlay(context.snapshot.graph, alternative)
            reports = self._assessment.projected(context.snapshot, graph, self._policy)
            return AlternativeComparison.create(
                request=context.request,
                alternative=alternative,
                current_scorecard=reports.current.node(context.request.subject_ref),
                projected_scorecard=reports.projected.node(context.request.subject_ref),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def validate_candidate(
        self,
        *,
        request_id: str,
        candidate: RequirementAlternativeCandidate,
    ) -> AlternativeComparison | None:
        if type(request_id) is not str or type(candidate) is not RequirementAlternativeCandidate:
            return None
        with self._lock:
            context = self._contexts.get(request_id)
        if context is None:
            return None
        current = self._snapshot_loader()
        if (
            type(current) is not AssessmentSnapshot
            or current.aggregate_digest != context.request.snapshot_digest
            or current.principal_projection_digest != context.request.principal_projection_digest
        ):
            return None
        comparison = self._compare(context, candidate)
        if comparison is not None:
            self._store_comparison(comparison, context.request.id)
        return comparison

    def get_comparison(
        self,
        alternative_id: str,
        *,
        expected_request_digest: str,
        expected_effect_digest: str,
    ) -> AlternativeComparison | None:
        """Revalidate one stored comparison against the exact current safe snapshot."""
        if any(
            type(value) is not str or not value
            for value in (alternative_id, expected_request_digest, expected_effect_digest)
        ):
            return None
        with self._lock:
            comparison = self._comparisons.get(alternative_id)
            request_id = self._comparison_requests.get(alternative_id)
            context = self._contexts.get(request_id) if request_id is not None else None
        if (
            comparison is None
            or context is None
            or comparison.request_digest != expected_request_digest
            or comparison.alternative.graph_effect_digest != expected_effect_digest
        ):
            return None
        current = self._snapshot_loader()
        if (
            type(current) is not AssessmentSnapshot
            or current.aggregate_digest != context.request.snapshot_digest
            or current.principal_projection_digest != context.request.principal_projection_digest
        ):
            return None
        revalidated = self._compare(context, comparison.alternative.candidate)
        return comparison if revalidated == comparison else None


__all__ = ["AlternativeReasoner", "RequirementAlternativeService"]
