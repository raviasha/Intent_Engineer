"""Fail-closed local validation for untrusted requirement alternatives."""

from __future__ import annotations

from intent_engineering.alternatives.models import (
    AlternativeRequest,
    GraphEffectKind,
    RequirementAlternative,
    RequirementAlternativeCandidate,
)


class AlternativeValidationError(ValueError):
    """Fixed public rejection that never reflects provider content."""

    def __init__(self) -> None:
        super().__init__("alternative candidate invalid")


class AlternativeValidator:
    """Validate references and effects against one exact grounding request."""

    def validate(
        self,
        request: AlternativeRequest,
        candidate: RequirementAlternativeCandidate,
    ) -> RequirementAlternative:
        alternative: RequirementAlternative | None = None
        failed = False
        try:
            alternative = self._validate(request, candidate)
        except (TypeError, ValueError):
            failed = True
        if failed or alternative is None:
            raise AlternativeValidationError()
        return alternative

    def _validate(
        self,
        request: AlternativeRequest,
        candidate: RequirementAlternativeCandidate,
    ) -> RequirementAlternative:
        if type(request) is not AlternativeRequest:
            raise ValueError("invalid request")
        if type(candidate) is not RequirementAlternativeCandidate:
            raise ValueError("invalid candidate")
        if candidate.subject_ref != request.subject_ref:
            raise ValueError("subject mismatch")
        if candidate.mode not in request.requested_modes:
            raise ValueError("mode mismatch")

        outcome_refs = set(request.desired_outcome_refs)
        if not set(candidate.preserved_outcome_refs).issubset(outcome_refs):
            raise ValueError("invented preserved outcome")
        if not {item.outcome_ref for item in candidate.weakened_outcomes}.issubset(outcome_refs):
            raise ValueError("invented weakened outcome")

        constraint_refs = set(request.constraint_refs)
        if not {item.constraint_ref for item in candidate.constraint_effects}.issubset(
            constraint_refs
        ):
            raise ValueError("invented constraint")

        evidence_refs = {item.evidence_ref for item in request.evidence_summaries}
        if not set(candidate.supporting_evidence_refs).issubset(evidence_refs):
            raise ValueError("invisible evidence")

        allowed_targets = {
            request.subject_ref,
            *request.intent_refs,
            *request.desired_outcome_refs,
            *request.constraint_refs,
        }
        if any(effect.target_ref not in allowed_targets for effect in candidate.graph_effects):
            raise ValueError("graph effect outside request scope")
        subject_updates = tuple(
            effect
            for effect in candidate.graph_effects
            if effect.effect is GraphEffectKind.UPDATE_NODE
            and effect.target_ref == request.subject_ref
        )
        if len(subject_updates) != 1:
            raise ValueError("candidate requires one subject update")
        update = subject_updates[0]
        if (
            update.field not in {"label", "statement"}
            or update.before != request.subject_text
            or update.after != candidate.proposed_text
        ):
            raise ValueError("subject update does not match candidate")

        return RequirementAlternative.from_candidate(
            candidate,
            request_digest=request.digest,
            graph_effect_digest=candidate.graph_effect_digest,
        )


__all__ = ["AlternativeValidationError", "AlternativeValidator"]
