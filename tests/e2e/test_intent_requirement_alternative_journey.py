"""Executable journey from projected alternative to independently reviewed requirement."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import yaml  # type: ignore[import-untyped]

from intent_engineering.alternatives import (
    AlternativeActor,
    AlternativeMode,
    AlternativeProposalService,
    RequirementAlternativeService,
)
from intent_engineering.assessment import GraphAssessmentService
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.control_plane import ControlPlaneService
from intent_engineering.core.models import EvidenceIngestion, ProjectConfig
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.clarification import (
    ProposalConfirmationService,
    ProposalConfirmationStatus,
)
from intent_engineering.storage.yaml.graph_store import serialize_graph
from tests.integration.alternatives.test_service import RecordingReasoner, SnapshotLoader
from tests.integration.intent_workflow.test_clarification import _harness
from tests.unit.alternatives.fixtures import assessment_pair

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
ORIGIN = "http://localhost:43127"


def test_manual_alternative_is_canonical_only_after_independent_review(tmp_path) -> None:
    snapshot, _fixture_report = assessment_pair()
    harness = _harness(tmp_path)
    snapshot = snapshot.model_copy(
        update={"graph": snapshot.graph.model_copy(update={"id": "graph:demo", "name": "demo"})}
    )
    harness.graph_store.initialize(snapshot.graph)
    harness.evidence_store.associate("markdown-alternative", snapshot.evidence[0])
    loader = SnapshotLoader(snapshot)
    assessment = GraphAssessmentService(clock=lambda: NOW)
    alternatives = RequirementAlternativeService(
        snapshot_loader=loader,
        reasoner=RecordingReasoner(),
        assessment=assessment,
    )
    proposals = AlternativeProposalService(
        alternatives=alternatives,
        snapshot_loader=loader,
        proposal_store=harness.proposal_store,
        actor_resolver=lambda: AlternativeActor(
            actor="local:asha",
            aliases=("github:asha", "local:asha", "slack:asha"),
        ),
        clock=lambda: NOW,
    )
    approved_before = assessment.assess(snapshot)
    graph_before = harness.paths["graph"].read_bytes()

    result = alternatives.request(
        subject_ref="req:export",
        modes=(AlternativeMode.CHANGE_APPROACH,),
    )
    comparison = result.alternatives[0]

    assert comparison.current_state == "approved"
    assert comparison.projected_state == "projected"
    assert comparison.alternative.candidate.proposed_text == (
        "An owner exports the report on request."
    )
    assert harness.paths["graph"].read_bytes() == graph_before

    proposal = proposals.submit(
        comparison.id,
        expected_request_digest=comparison.request_digest,
        expected_effect_digest=comparison.alternative.graph_effect_digest,
    )

    assert harness.paths["graph"].read_bytes() == graph_before
    high_risk, reasons = ProposalConfirmationService._risk(proposal, snapshot.graph)
    assert high_risk is True
    assert "node_update" in reasons

    self_review = harness.confirmation.confirm(
        proposal.id,
        actor="local:asha",
        at=NOW + timedelta(microseconds=1),
    )
    assert self_review.status is ProposalConfirmationStatus.REVIEW_REQUIRED
    assert harness.paths["graph"].read_bytes() == graph_before

    independent_review = harness.confirmation.confirm(
        proposal.id,
        actor="local:ben",
        at=NOW + timedelta(microseconds=2),
    )
    assert independent_review.status is ProposalConfirmationStatus.APPLIED

    approved_graph = harness.graph_store.load()
    approved_snapshot = snapshot.model_copy(
        update={
            "graph": approved_graph,
            "graph_digest": "sha256:" + "d" * 64,
            "aggregate_digest": "sha256:" + "e" * 64,
        }
    )
    approved_after = assessment.assess(approved_snapshot)

    assert approved_graph.version == snapshot.graph.version + 1
    assert approved_after.snapshot_digest != approved_before.snapshot_digest
    approved_requirement = next(node for node in approved_graph.nodes if node.id == "req:export")
    assert approved_requirement.label == "An owner exports the report on request."
    assert harness.paths["graph"].read_bytes() != graph_before


def test_independent_local_control_plane_can_preview_alternative_proposal(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialized = initialize_project(project)
    snapshot, _report = assessment_pair()
    graph = snapshot.graph.model_copy(update={"id": "graph:demo", "name": "demo"})
    initialized.graph_path.write_bytes(serialize_graph(graph))
    initialized.config_path.write_text(
        yaml.safe_dump(
            ProjectConfig(project_id="demo", local_actor="local:asha").model_dump(mode="json"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    ingestion = EvidenceIngestion(
        connector_id="markdown-alternative",
        sequence=1,
        predecessor_id=None,
        evidence=snapshot.evidence[0],
    )
    (initialized.workspace / "evidence/evidence.jsonl").write_text(
        json.dumps(
            ingestion.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (initialized.workspace / "approvals/policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": ["local:asha", "local:ben"],
                "approvers": ["local:ben"],
                "executors": ["local:ben"],
                "identities": {
                    "local:asha": ["github:asha", "local:asha"],
                    "local:ben": ["github:ben", "local:ben"],
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    runtime = load_runtime(project)
    proposer = ControlPlaneService(
        runtime,
        origin=ORIGIN,
        clock=lambda: NOW,
        alternative_reasoner=RecordingReasoner(),
    )
    try:
        generated = proposer.alternatives("req:export", ("change_approach",))
        item = generated["alternatives"][0]
        comparison = item["comparison"]
        pending = proposer.alternative_propose(
            item["comparison_id"],
            comparison["request_digest"],
            comparison["alternative"]["graph_effect_digest"],
        )
        proposal_id = pending["proposal_id"]
    finally:
        proposer.close()
        runtime.close()

    initialized.config_path.write_text(
        yaml.safe_dump(
            ProjectConfig(project_id="demo", local_actor="local:ben").model_dump(mode="json"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    reviewer_runtime = load_runtime(project)
    reviewer = ControlPlaneService(reviewer_runtime, origin=ORIGIN, clock=lambda: NOW)
    try:
        preview = reviewer.proposal_preview(proposal_id)
    finally:
        reviewer.close()
        reviewer_runtime.close()

    assert preview["payload"]["action"] == "confirm_proposal"
    assert preview["preview"]["kind"] == "requirement_alternative_proposal"
    assert preview["preview"]["high_risk"] is True
    assert "node_update" in preview["preview"]["risk_reasons"]
