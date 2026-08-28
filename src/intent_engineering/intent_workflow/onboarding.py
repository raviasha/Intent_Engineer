"""Read-only inspection of an intent repository's onboarding state."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import ConfigDict, Field

from intent_engineering.core.models import Graph
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.proposal_store import IntentProposalStore
from intent_engineering.storage.interfaces import GraphStore

_MAX_PENDING_PROPOSALS = 256


class OnboardingError(ValueError):
    """Fixed public failure for unavailable or inconsistent onboarding state."""

    def __init__(self) -> None:
        super().__init__("intent onboarding unavailable")


class OnboardingState(StrEnum):
    """The durable baseline state relevant to guided onboarding."""

    REQUIRED = "required"
    REVIEW_REQUIRED = "review_required"
    READY = "ready"


class OnboardingStatus(StrictModel):
    """Frozen, detached summary suitable for CLI and host adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    state: OnboardingState
    graph_version: int
    active_node_count: int
    pending_proposal_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_PENDING_PROPOSALS)]


class OnboardingRuntime(Protocol):
    """Read-only runtime surface required to inspect onboarding readiness."""

    graph_store: GraphStore
    intent_proposals: IntentProposalStore


def _validated_graph(runtime: OnboardingRuntime) -> Graph:
    graph = runtime.graph_store.load()
    if type(graph) is not Graph:
        raise ValueError("invalid graph")
    graph.assert_invariants()
    return graph


def inspect_onboarding(runtime: OnboardingRuntime) -> OnboardingStatus:
    """Return a bounded, read-only view of baseline and proposal readiness."""
    graph: Graph | None = None
    pending_ids: list[str] = []
    pending_seen: set[str] = set()
    failed = False
    try:
        graph = _validated_graph(runtime)
        proposals = runtime.intent_proposals.list()
        if type(proposals) is not tuple:
            raise ValueError("invalid proposal ledger")
        for proposal in proposals[:_MAX_PENDING_PROPOSALS]:
            proposal_id = proposal.id
            if type(proposal_id) is not str or proposal_id in pending_seen:
                raise ValueError("invalid proposal ledger")
            if runtime.intent_proposals.decision_for(proposal_id) is None:
                pending_seen.add(proposal_id)
                if len(pending_ids) < _MAX_PENDING_PROPOSALS:
                    pending_ids.append(proposal_id)
        state = (
            OnboardingState.READY
            if graph.version > 0 and graph.nodes
            else OnboardingState.REVIEW_REQUIRED
            if pending_ids
            else OnboardingState.REQUIRED
        )
        return OnboardingStatus(
            state=state,
            graph_version=graph.version,
            active_node_count=len(graph.nodes),
            pending_proposal_ids=tuple(pending_ids),
        )
    except Exception:  # noqa: BLE001 - storage details are deliberately hidden at this boundary
        failed = True
    finally:
        graph = None
        pending_ids.clear()
        pending_seen.clear()
    if failed:
        raise OnboardingError() from None
    raise OnboardingError()
