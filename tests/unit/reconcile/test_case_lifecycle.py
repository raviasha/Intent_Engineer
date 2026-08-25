"""Lifecycle and schema tests for immutable reconciliation cases."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import (
    ClassificationEvent,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    ResolutionAction,
)
from intent_engineering.core.models.schemas import schema_bytes
from intent_engineering.reconcile.service import (
    InvalidCaseTransition,
    MissingResolutionEvidence,
    transition_case,
)
from tests.unit.reconcile.builders import NOW, side

SCHEMA_DIRECTORY = Path(__file__).parents[3] / "schemas"


def reconciliation_case(**changes: object) -> ReconciliationCase:
    payload: dict[str, object] = {
        "id": "case-1",
        "subject_ref": "req-export",
        "case_type": ReconciliationCaseType.CODE_LAG,
        "affected_refs": ("req-export", "symbol-export"),
        "evidence_sides": (side("requirement", version=2), side("implementation", version=1)),
        "detector_id": "code_lag",
        "fingerprint": "f" * 64,
        "created_at": NOW,
        "status": ReconciliationStatus.OPEN,
        "requires_human": True,
        "history": (),
    }
    payload.update(changes)
    return ReconciliationCase(**payload)


def test_case_evidence_refs_are_an_ordered_union_across_sides() -> None:
    case = reconciliation_case(
        evidence_sides=(
            side("requirement", version=2),
            side("implementation", version=1),
            side("requirement", version=2),
        )
    )

    assert case.all_evidence_refs == ("ev-implementation-1", "ev-requirement-2")


def test_case_requires_evidence_references() -> None:
    with pytest.raises(ValidationError, match="reconciliation case requires evidence"):
        reconciliation_case(evidence_sides=())


def test_lifecycle_allows_only_approved_non_terminal_path() -> None:
    opened = reconciliation_case()
    proposed = transition_case(opened, ReconciliationStatus.PROPOSED, "reviewer", NOW)
    needs_human = transition_case(proposed, ReconciliationStatus.NEEDS_HUMAN, "reviewer", NOW)
    resolved = transition_case(
        needs_human,
        ReconciliationStatus.RESOLVED,
        "reviewer",
        NOW,
        resolution=ResolutionAction.UPDATE_IMPLEMENTATION,
        changeset_id="cs-1",
    )

    assert [opened.status, proposed.status, needs_human.status, resolved.status] == [
        ReconciliationStatus.OPEN,
        ReconciliationStatus.PROPOSED,
        ReconciliationStatus.NEEDS_HUMAN,
        ReconciliationStatus.RESOLVED,
    ]
    assert len(resolved.history) == 3
    assert opened.history == ()


@pytest.mark.parametrize("terminal", [ReconciliationStatus.DEFERRED, ReconciliationStatus.FALSE_POSITIVE])
def test_open_case_can_take_terminal_alternative(terminal: ReconciliationStatus) -> None:
    result = transition_case(reconciliation_case(), terminal, "reviewer", NOW)

    assert result.status is terminal


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (ReconciliationStatus.PROPOSED, ReconciliationStatus.DEFERRED),
        (ReconciliationStatus.PROPOSED, ReconciliationStatus.FALSE_POSITIVE),
        (ReconciliationStatus.NEEDS_HUMAN, ReconciliationStatus.DEFERRED),
        (ReconciliationStatus.NEEDS_HUMAN, ReconciliationStatus.FALSE_POSITIVE),
        (ReconciliationStatus.RESOLVED, ReconciliationStatus.DEFERRED),
        (ReconciliationStatus.RESOLVED, ReconciliationStatus.FALSE_POSITIVE),
        (ReconciliationStatus.DEFERRED, ReconciliationStatus.FALSE_POSITIVE),
        (ReconciliationStatus.FALSE_POSITIVE, ReconciliationStatus.DEFERRED),
    ],
)
def test_only_open_cases_can_take_terminal_alternatives(
    source: ReconciliationStatus, target: ReconciliationStatus
) -> None:
    if source is ReconciliationStatus.PROPOSED:
        case = transition_case(reconciliation_case(), source, "reviewer", NOW)
    elif source is ReconciliationStatus.NEEDS_HUMAN:
        case = transition_case(reconciliation_case(), ReconciliationStatus.PROPOSED, "reviewer", NOW)
        case = transition_case(case, source, "reviewer", NOW)
    elif source is ReconciliationStatus.RESOLVED:
        case = transition_case(reconciliation_case(), ReconciliationStatus.PROPOSED, "reviewer", NOW)
        case = transition_case(case, ReconciliationStatus.NEEDS_HUMAN, "reviewer", NOW)
        case = transition_case(
            case,
            source,
            "reviewer",
            NOW,
            resolution=ResolutionAction.UPDATE_IMPLEMENTATION,
            changeset_id="cs-1",
        )
    else:
        case = reconciliation_case(status=source)

    with pytest.raises(InvalidCaseTransition):
        transition_case(case, target, "reviewer", NOW)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (ReconciliationStatus.OPEN, ReconciliationStatus.NEEDS_HUMAN),
        (ReconciliationStatus.OPEN, ReconciliationStatus.RESOLVED),
        (ReconciliationStatus.PROPOSED, ReconciliationStatus.RESOLVED),
        (ReconciliationStatus.DEFERRED, ReconciliationStatus.OPEN),
        (ReconciliationStatus.FALSE_POSITIVE, ReconciliationStatus.OPEN),
    ],
)
def test_lifecycle_rejects_forbidden_or_terminal_transitions(
    source: ReconciliationStatus, target: ReconciliationStatus
) -> None:
    case = reconciliation_case(status=source)

    with pytest.raises(InvalidCaseTransition):
        transition_case(case, target, "reviewer", NOW)


def test_resolved_case_cannot_reopen() -> None:
    case = transition_case(reconciliation_case(), ReconciliationStatus.PROPOSED, "reviewer", NOW)
    case = transition_case(case, ReconciliationStatus.NEEDS_HUMAN, "reviewer", NOW)
    resolved = transition_case(
        case,
        ReconciliationStatus.RESOLVED,
        "reviewer",
        NOW,
        resolution=ResolutionAction.UPDATE_IMPLEMENTATION,
        changeset_id="cs-1",
    )

    with pytest.raises(InvalidCaseTransition):
        transition_case(resolved, ReconciliationStatus.OPEN, "reviewer", NOW)


@pytest.mark.parametrize(
    ("actor", "at", "resolution", "changeset_id"),
    [
        ("", NOW, ResolutionAction.UPDATE_IMPLEMENTATION, "cs-1"),
        ("reviewer", None, ResolutionAction.UPDATE_IMPLEMENTATION, "cs-1"),
        ("reviewer", NOW, None, "cs-1"),
        ("reviewer", NOW, ResolutionAction.UPDATE_IMPLEMENTATION, None),
    ],
)
def test_resolved_requires_complete_resolution_audit_data(
    actor: str,
    at: datetime | None,
    resolution: ResolutionAction | None,
    changeset_id: str | None,
) -> None:
    case = transition_case(reconciliation_case(), ReconciliationStatus.PROPOSED, "reviewer", NOW)
    case = transition_case(case, ReconciliationStatus.NEEDS_HUMAN, "reviewer", NOW)

    with pytest.raises(MissingResolutionEvidence):
        transition_case(case, ReconciliationStatus.RESOLVED, actor, at, resolution, changeset_id)  # type: ignore[arg-type]


def test_case_history_is_immutable() -> None:
    event = ClassificationEvent(
        actor="reviewer",
        at=datetime(2026, 8, 25, tzinfo=UTC),
        prior=ReconciliationStatus.OPEN,
        new=ReconciliationStatus.PROPOSED,
    )
    case = reconciliation_case(history=[event])

    assert case.history == (event,)
    with pytest.raises(ValidationError):
        case.history += (event,)  # type: ignore[misc]


def test_checked_in_reconciliation_schema_matches_deterministic_regeneration() -> None:
    assert (SCHEMA_DIRECTORY / "reconciliation.schema.json").read_bytes() == schema_bytes(
        "ReconciliationCase"
    )
