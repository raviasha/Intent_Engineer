"""Reconciliation case lifecycle transitions."""

from __future__ import annotations

from datetime import datetime

from intent_engineering.core.models import (
    ClassificationEvent,
    ReconciliationCase,
    ReconciliationStatus,
    ResolutionAction,
)
from intent_engineering.core.models.reconciliation import ALLOWED_CASE_TRANSITIONS

ALLOWED_TRANSITIONS = ALLOWED_CASE_TRANSITIONS


class InvalidCaseTransition(ValueError):
    """Raised for a lifecycle transition outside the alpha state machine."""

    def __init__(self, prior: ReconciliationStatus, target: ReconciliationStatus) -> None:
        super().__init__(f"invalid reconciliation transition: {prior} -> {target}")


class MissingResolutionEvidence(ValueError):
    """Raised when a resolution cannot be attached to a complete audit record."""

    def __init__(self, case_id: str) -> None:
        super().__init__(f"resolution requires actor, timestamp, action, and changeset: {case_id}")


def transition_case(
    case: ReconciliationCase,
    target: ReconciliationStatus,
    actor: str,
    at: datetime | None,
    resolution: ResolutionAction | None = None,
    changeset_id: str | None = None,
) -> ReconciliationCase:
    """Return a new immutable, auditable case version after a valid transition."""
    if target not in ALLOWED_TRANSITIONS[case.status]:
        raise InvalidCaseTransition(case.status, target)
    if target is ReconciliationStatus.RESOLVED and (
        not actor or at is None or resolution is None or changeset_id is None
    ):
        raise MissingResolutionEvidence(case.id)
    if not actor or at is None:
        raise MissingResolutionEvidence(case.id)
    event = ClassificationEvent(actor=actor, at=at, prior=case.status, new=target)
    updated = case.model_copy(
        update={
            "status": target,
            "resolution": resolution if target is ReconciliationStatus.RESOLVED else None,
            "resolved_by_changeset": changeset_id if target is ReconciliationStatus.RESOLVED else None,
            "history": case.history + (event,),
        }
    )
    return ReconciliationCase.model_validate(updated.model_dump())
