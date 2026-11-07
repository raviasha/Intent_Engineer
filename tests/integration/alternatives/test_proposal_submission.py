"""Selected alternatives enter ordinary proposal governance without direct mutation."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from intent_engineering.alternatives.models import AlternativeMode
from intent_engineering.alternatives.proposals import (
    AlternativeActor,
    AlternativeProposalConflictError,
    AlternativeProposalService,
)
from intent_engineering.alternatives.service import RequirementAlternativeService
from intent_engineering.intent_workflow import ProposalKind
from intent_engineering.intent_workflow.clarification import ProposalConfirmationService
from intent_engineering.intent_workflow.proposal_store import IntentProposalStore
from tests.integration.alternatives.test_service import RecordingReasoner, SnapshotLoader
from tests.unit.alternatives.fixtures import assessment_pair

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


def _services(tmp_path: Path):
    snapshot, _report = assessment_pair()
    loader = SnapshotLoader(snapshot)
    alternatives = RequirementAlternativeService(
        snapshot_loader=loader,
        reasoner=RecordingReasoner(),
    )
    result = alternatives.request(
        subject_ref="req:export",
        modes=(AlternativeMode.CHANGE_APPROACH,),
    )
    comparison = result.alternatives[0]
    store = IntentProposalStore(tmp_path / "intent-proposals.jsonl")
    proposals = AlternativeProposalService(
        alternatives=alternatives,
        snapshot_loader=loader,
        proposal_store=store,
        actor_resolver=lambda: AlternativeActor(
            actor="actor:owner",
            aliases=("actor:owner", "github:42"),
        ),
        clock=lambda: NOW,
    )
    return loader, alternatives, comparison, store, proposals


def test_submit_creates_pending_proposal_without_mutating_graph(tmp_path: Path) -> None:
    loader, _alternatives, comparison, store, proposals = _services(tmp_path)
    before = loader.snapshot.graph.model_dump_json()

    proposal = proposals.submit(
        comparison.id,
        expected_request_digest=comparison.request_digest,
        expected_effect_digest=comparison.alternative.graph_effect_digest,
    )

    assert proposal.kind is ProposalKind.REQUIREMENT
    assert proposal.proposed_by == "actor:owner"
    assert proposal.evidence_refs == ("evidence:visible",)
    assert proposal.changeset.nodes_updated[0].node_id == "req:export"
    assert proposal.changeset.nodes_updated[0].replacement.label == (
        "An owner exports the report on request."
    )
    assert loader.snapshot.graph.model_dump_json() == before
    assert store.get(proposal.id) == proposal
    store.close()


def test_selected_alternative_uses_existing_independent_high_risk_review(
    tmp_path: Path,
) -> None:
    loader, _alternatives, comparison, store, proposals = _services(tmp_path)
    proposal = proposals.submit(
        comparison.id,
        expected_request_digest=comparison.request_digest,
        expected_effect_digest=comparison.alternative.graph_effect_digest,
    )

    high_risk, reasons = ProposalConfirmationService._risk(proposal, loader.snapshot.graph)

    assert high_risk is True
    assert "node_update" in reasons
    assert proposal.proposed_by == proposal.changeset.actor
    store.close()


def test_stale_or_digest_substituted_alternative_cannot_be_submitted(
    tmp_path: Path,
) -> None:
    loader, _alternatives, comparison, store, proposals = _services(tmp_path)
    loader.snapshot = loader.snapshot.model_copy(update={"aggregate_digest": "sha256:" + "d" * 64})

    with pytest.raises(AlternativeProposalConflictError, match="alternative proposal unavailable"):
        proposals.submit(
            comparison.id,
            expected_request_digest=comparison.request_digest,
            expected_effect_digest=comparison.alternative.graph_effect_digest,
        )
    with pytest.raises(AlternativeProposalConflictError, match="alternative proposal unavailable"):
        proposals.submit(
            comparison.id,
            expected_request_digest="sha256:" + "a" * 64,
            expected_effect_digest=comparison.alternative.graph_effect_digest,
        )
    assert store.list() == ()
    store.close()


def test_replay_converges_on_the_same_pending_proposal(tmp_path: Path) -> None:
    _loader, _alternatives, comparison, store, proposals = _services(tmp_path)

    first = proposals.submit(
        comparison.id,
        expected_request_digest=comparison.request_digest,
        expected_effect_digest=comparison.alternative.graph_effect_digest,
    )
    second = proposals.submit(
        comparison.id,
        expected_request_digest=comparison.request_digest,
        expected_effect_digest=comparison.alternative.graph_effect_digest,
    )

    assert first == second
    assert store.list() == (first,)
    store.close()
