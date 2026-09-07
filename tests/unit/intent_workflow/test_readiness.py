"""Read-only developer readiness behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from intent_engineering.core.models import Graph, Node, NodeType, ReconciliationStatus
from intent_engineering.intent_workflow.readiness import (
    EnsureRequest,
    EnsureResult,
    EnsureStatus,
    ReadinessError,
    ReadinessService,
    ReadinessTarget,
)

NOW = datetime(2026, 9, 7, tzinfo=UTC)


@dataclass
class _GraphStore:
    graph: Graph

    def load(self) -> Graph:
        return self.graph


@dataclass
class _ProposalStore:
    proposals: tuple[object, ...] = ()
    clarification_event_items: tuple[object, ...] = ()

    def list(self) -> tuple[object, ...]:
        return self.proposals

    def decision_for(self, proposal_id: str) -> object | None:
        del proposal_id
        return None

    def clarification_events(self) -> tuple[object, ...]:
        return self.clarification_event_items


@dataclass
class _Runtime:
    graph_store: _GraphStore
    intent_proposals: _ProposalStore
    case_items: tuple[object, ...] = ()

    def cases(self) -> tuple[object, ...]:
        return self.case_items


def _ready_runtime() -> _Runtime:
    node = Node(
        id="intent:readiness",
        type=NodeType.PRODUCT_INTENT,
        label="Readiness reports the approved baseline without changing it",
        status="active",
        created_by="local:owner",
        created_at=NOW,
        last_modified_by="local:owner",
        last_modified_at=NOW,
    )
    return _Runtime(
        graph_store=_GraphStore(Graph(id="graph:readiness", version=1, nodes=(node,), edges=())),
        intent_proposals=_ProposalStore(),
    )


def _empty_runtime() -> _Runtime:
    return _Runtime(
        graph_store=_GraphStore(Graph(id="graph:readiness", version=0, nodes=(), edges=())),
        intent_proposals=_ProposalStore(),
    )


def test_ready_baseline_returns_a_replay_stable_readiness_projection() -> None:
    """Catches an aligned baseline being blocked or readiness changing graph state."""
    runtime = _ready_runtime()
    before = runtime.graph_store.graph
    service = ReadinessService(runtime)

    first = service.ensure(EnsureRequest())
    second = service.ensure(EnsureRequest())

    assert (
        first
        == second
        == EnsureResult(
            status=EnsureStatus.READY,
            attention_route=ReadinessTarget.HOME,
            graph_version=1,
            pending_proposal_ids=(),
            open_case_ids=(),
        )
    )
    assert runtime.graph_store.graph == before


def test_empty_baseline_routes_the_developer_to_onboarding() -> None:
    """Catches an unapproved empty graph being treated as ready."""
    result = ReadinessService(_empty_runtime()).ensure(EnsureRequest())

    assert result == EnsureResult(
        status=EnsureStatus.ONBOARDING_REQUIRED,
        attention_route=ReadinessTarget.ONBOARDING,
        graph_version=0,
        pending_proposal_ids=(),
        open_case_ids=(),
    )


def test_pending_baseline_proposal_requires_human_review() -> None:
    """Catches a pending baseline proposal being ignored by developer readiness."""

    @dataclass(frozen=True)
    class Proposal:
        id: str

    runtime = _Runtime(
        graph_store=_GraphStore(Graph(id="graph:readiness", version=0, nodes=(), edges=())),
        intent_proposals=_ProposalStore((Proposal("proposal:pending-baseline"),)),
    )

    result = ReadinessService(runtime).ensure(EnsureRequest())

    assert result == EnsureResult(
        status=EnsureStatus.HUMAN_ATTENTION_REQUIRED,
        attention_route=ReadinessTarget.INBOX,
        graph_version=0,
        pending_proposal_ids=("proposal:pending-baseline",),
        open_case_ids=(),
    )


def test_open_reconciliation_case_requires_human_review() -> None:
    """Catches an unresolved divergence being hidden behind a ready status."""

    @dataclass(frozen=True)
    class Case:
        id: str
        status: ReconciliationStatus

    runtime = _ready_runtime()
    runtime.case_items = (Case("case:readiness-divergence", ReconciliationStatus.OPEN),)

    result = ReadinessService(runtime).ensure(EnsureRequest())

    assert result == EnsureResult(
        status=EnsureStatus.HUMAN_ATTENTION_REQUIRED,
        attention_route=ReadinessTarget.INBOX,
        graph_version=1,
        pending_proposal_ids=(),
        open_case_ids=("case:readiness-divergence",),
    )


def test_unanswered_clarification_requires_human_review() -> None:
    """Catches an active clarification being treated as an aligned prompt."""

    @dataclass(frozen=True)
    class Question:
        id: str

    @dataclass(frozen=True)
    class Session:
        id: str
        status: str
        questions: tuple[Question, ...]
        answers: tuple[object, ...]

    @dataclass(frozen=True)
    class Event:
        session: Session

    runtime = _ready_runtime()
    runtime.intent_proposals.clarification_event_items = (
        Event(
            Session(
                id="clarification:pending",
                status="open",
                questions=(Question("audience"),),
                answers=(),
            )
        ),
    )

    result = ReadinessService(runtime).ensure(EnsureRequest())

    assert result == EnsureResult(
        status=EnsureStatus.HUMAN_ATTENTION_REQUIRED,
        attention_route=ReadinessTarget.INBOX,
        graph_version=1,
        pending_proposal_ids=(),
        open_case_ids=(),
    )


def test_closed_clarification_supersedes_its_older_open_snapshot() -> None:
    """Catches historical clarification events blocking a now-resolved workflow."""

    @dataclass(frozen=True)
    class Question:
        id: str

    @dataclass(frozen=True)
    class Session:
        id: str
        status: str
        questions: tuple[Question, ...]
        answers: tuple[object, ...]

    @dataclass(frozen=True)
    class Event:
        session: Session

    runtime = _ready_runtime()
    runtime.intent_proposals.clarification_event_items = (
        Event(
            Session(
                id="clarification:resolved",
                status="open",
                questions=(Question("audience"),),
                answers=(),
            )
        ),
        Event(
            Session(
                id="clarification:resolved",
                status="closed",
                questions=(Question("audience"),),
                answers=(),
            )
        ),
    )

    result = ReadinessService(runtime).ensure(EnsureRequest())

    assert result.status is EnsureStatus.READY


def test_invalid_state_has_one_fixed_readiness_failure() -> None:
    """Catches storage details escaping the machine-facing readiness boundary."""

    @dataclass
    class BrokenGraphStore:
        def load(self) -> Graph:
            raise ValueError("PRIVATE graph storage detail")

    runtime = _Runtime(BrokenGraphStore(), _ProposalStore())  # type: ignore[arg-type]

    with pytest.raises(ReadinessError) as caught:
        ReadinessService(runtime).ensure(EnsureRequest())

    assert caught.value.args == ("intent readiness unavailable",)
    assert caught.value.__context__ is None
