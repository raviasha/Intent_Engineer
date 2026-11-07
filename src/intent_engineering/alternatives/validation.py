"""Fail-closed local validation for untrusted requirement alternatives."""

from __future__ import annotations

from intent_engineering.alternatives.models import (
    AlternativeRequest,
    ConstraintEffectKind,
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
        preserved_outcomes = set(candidate.preserved_outcome_refs)
        weakened_outcomes = {item.outcome_ref for item in candidate.weakened_outcomes}
        if (
            preserved_outcomes & weakened_outcomes
            or preserved_outcomes | weakened_outcomes != outcome_refs
        ):
            raise ValueError("outcomes are not exactly accounted for")
        if set(candidate.preserved_intent_refs) != set(request.intent_refs):
            raise ValueError("intent is not exactly preserved")

        constraint_refs = set(request.constraint_refs)
        if not {item.constraint_ref for item in candidate.constraint_effects}.issubset(
            constraint_refs
        ):
            raise ValueError("invented constraint")

        evidence_refs = {item.evidence_ref for item in request.evidence_summaries}
        if not set(candidate.supporting_evidence_refs).issubset(evidence_refs):
            raise ValueError("invisible evidence")

        allowed_targets = {request.subject_ref, *request.constraint_refs}
        if any(effect.target_ref not in allowed_targets for effect in candidate.graph_effects):
            raise ValueError("graph effect outside mutable request scope")
        subject_effects = tuple(
            effect for effect in candidate.graph_effects if effect.target_ref == request.subject_ref
        )
        if (
            len(subject_effects) != 1
            or subject_effects[0].effect is not GraphEffectKind.UPDATE_NODE
        ):
            raise ValueError("candidate requires one subject update")
        update = subject_effects[0]
        if (
            update.field not in {"label", "statement"}
            or update.before != request.subject_text
            or update.after != candidate.proposed_text
        ):
            raise ValueError("subject update does not match candidate")

        context = {item.node_ref: item for item in request.context_nodes}
        constraint_effect_items = tuple(
            effect for effect in candidate.graph_effects if effect.target_ref in constraint_refs
        )
        constraint_graph_effects = {effect.target_ref: effect for effect in constraint_effect_items}
        declared_constraints = {item.constraint_ref: item for item in candidate.constraint_effects}
        if len(constraint_graph_effects) != len(constraint_effect_items) or set(
            constraint_graph_effects
        ) != set(declared_constraints):
            raise ValueError("constraint mutations are not exactly accounted for")
        for reference, declaration in declared_constraints.items():
            effect = constraint_graph_effects[reference]
            current = context[reference]
            if declaration.effect is ConstraintEffectKind.CHANGE:
                if (
                    effect.effect is not GraphEffectKind.UPDATE_NODE
                    or effect.field not in {"label", "statement"}
                    or effect.before != current.label
                    or effect.after != declaration.proposed_text
                ):
                    raise ValueError("constraint change does not match graph effect")
            elif declaration.effect is ConstraintEffectKind.REMOVE:
                if (
                    effect.effect is not GraphEffectKind.REMOVE_NODE
                    or effect.before != current.label
                ):
                    raise ValueError("constraint removal does not match graph effect")
            else:
                raise ValueError("adding constraints is not supported")

        return RequirementAlternative.from_candidate(
            candidate,
            request_digest=request.digest,
            graph_effect_digest=candidate.activation_effect_digest,
        )


__all__ = ["AlternativeValidationError", "AlternativeValidator"]
