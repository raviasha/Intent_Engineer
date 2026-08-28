"""Read-only onboarding-state behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import ChangeSet, Graph, Node
from intent_engineering.intent_workflow.models import IntentProposal, ProposalKind
from intent_engineering.intent_workflow.onboarding import (
    OnboardingError,
    OnboardingState,
    OnboardingStatus,
    inspect_onboarding,
)

NOW = datetime(2026, 8, 28, tzinfo=UTC)


@dataclass
class _GraphStore:
    graph: Graph

    def load(self) -> Graph:
        return self.graph


@dataclass
class _ProposalStore:
    proposals: tuple[IntentProposal, ...] = ()
    decided_ids: frozenset[str] = frozenset()

    def list(self) -> tuple[IntentProposal, ...]:
        return self.proposals

    def decision_for(self, proposal_id: str) -> object | None:
        return object() if proposal_id in self.decided_ids else None


@dataclass
class _BoundedProposalStore(_ProposalStore):
    decision_lookups: int = 0

    def decision_for(self, proposal_id: str) -> object | None:
        if self.decision_lookups >= 256:
            raise AssertionError("decision lookup exceeded the proposal-list limit")
        self.decision_lookups += 1
        return super().decision_for(proposal_id)


@dataclass
class _Runtime:
    graph_store: _GraphStore
    intent_proposals: _ProposalStore


def _graph(*, version: int = 0, nodes: tuple[Node, ...] = ()) -> Graph:
    return Graph(id="graph:onboarding-test", version=version, nodes=nodes, edges=())


def _node() -> Node:
    return Node(
        id="intent:onboarding",
        type="PRODUCT_INTENT",
        label="Onboarding creates a confirmed baseline",
        status="active",
        created_by="local:owner",
        created_at=NOW,
        last_modified_by="local:owner",
        last_modified_at=NOW,
    )


def _proposal(index: int = 0) -> IntentProposal:
    changeset = ChangeSet(
        id=f"changeset:onboarding:{index}",
        actor="agent:codex",
        timestamp=NOW,
        baseline_graph_version=0,
        evidence_refs=("evidence:prd:v1",),
        nodes_added=(),
        nodes_updated=(),
        nodes_superseded=(),
        edges_added=(),
        edges_updated=(),
        edges_superseded=(),
        confidence_changes=(),
        implementation_status_changes=(),
        reconciliation_cases_created=(),
        reconciliation_cases_resolved=(),
        validation_status="validated",
    )
    material = {
        "schema_version": 1,
        "kind": "requirement",
        "proposed_by": "agent:codex",
        "proposed_at": "2026-08-28T00:00:00Z",
        "baseline_graph_version": 0,
        "evidence_refs": ["evidence:prd:v1"],
        "source_roles": [],
        "changeset": changeset.model_dump(mode="json"),
        "core_node_ids": [],
        "provisional_node_ids": [],
        "assumptions": [f"assumption {index}"],
        "unanswered_questions": [],
        "conflicting_authors": [],
        "destructive": False,
    }
    proposal_id = (
        "proposal:sha256:"
        + sha256(
            json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
    )
    return IntentProposal(
        id=proposal_id,
        kind=ProposalKind.REQUIREMENT,
        proposed_by="agent:codex",
        proposed_at=NOW,
        baseline_graph_version=0,
        evidence_refs=("evidence:prd:v1",),
        source_roles=(),
        changeset=changeset,
        assumptions=(f"assumption {index}",),
    )


@pytest.fixture
def runtime() -> _Runtime:
    return _Runtime(_GraphStore(_graph()), _ProposalStore())


@pytest.fixture
def runtime_with_confirmed_baseline() -> _Runtime:
    return _Runtime(_GraphStore(_graph(version=1, nodes=(_node(),))), _ProposalStore())


@pytest.fixture
def runtime_with_proposal() -> _Runtime:
    return _Runtime(_GraphStore(_graph()), _ProposalStore((_proposal(),)))


def test_empty_initialized_repository_requires_onboarding(runtime: _Runtime) -> None:
    """Catches treating a zero-version, empty graph as an approved baseline."""
    status = inspect_onboarding(runtime)

    assert status == OnboardingStatus(
        state=OnboardingState.REQUIRED,
        graph_version=0,
        active_node_count=0,
        pending_proposal_ids=(),
    )


def test_active_baseline_is_ready(runtime_with_confirmed_baseline: _Runtime) -> None:
    """Catches hiding an already activated baseline behind onboarding."""
    status = inspect_onboarding(runtime_with_confirmed_baseline)

    assert status.state is OnboardingState.READY
    assert status.graph_version == 1
    assert status.active_node_count > 0


def test_pending_proposal_is_reported_without_becoming_a_baseline(
    runtime_with_proposal: _Runtime,
) -> None:
    """Catches treating a pending proposal as an approved graph baseline."""
    status = inspect_onboarding(runtime_with_proposal)

    assert status.state is OnboardingState.REVIEW_REQUIRED
    assert status.graph_version == 0
    assert status.pending_proposal_ids == (runtime_with_proposal.intent_proposals.proposals[0].id,)


def test_pending_proposals_are_bounded_to_the_public_proposal_list_limit() -> None:
    """Catches decision lookups continuing past the bounded proposal summary."""
    proposals = tuple(_proposal(index) for index in range(257))
    proposal_store = _BoundedProposalStore(proposals)
    runtime = _Runtime(_GraphStore(_graph()), proposal_store)

    status = inspect_onboarding(runtime)

    assert status.pending_proposal_ids == tuple(proposal.id for proposal in proposals[:256])
    assert proposal_store.decision_lookups == 256


def test_status_rejects_unbounded_pending_proposal_ids() -> None:
    """Catches callers bypassing the inspector's proposal-summary bound."""
    with pytest.raises(ValidationError):
        OnboardingStatus(
            state=OnboardingState.REVIEW_REQUIRED,
            graph_version=0,
            active_node_count=0,
            pending_proposal_ids=tuple(f"proposal:{index}" for index in range(257)),
        )


def test_malformed_durable_state_has_one_fixed_onboarding_failure() -> None:
    """Catches leaking malformed storage details through the onboarding read boundary."""

    class BrokenGraphStore:
        def load(self) -> Graph:
            raise ValueError("private graph parse detail")

    runtime = _Runtime(BrokenGraphStore(), _ProposalStore())  # type: ignore[arg-type]

    with pytest.raises(OnboardingError) as caught:
        inspect_onboarding(runtime)

    assert caught.value.args == ("intent onboarding unavailable",)
    assert caught.value.__context__ is None
