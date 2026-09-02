"""Literal contracts for critical-branch and project assessment rollups."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

from intent_engineering.assessment.models import (
    AssessmentHealth,
    AssessmentSnapshot,
)
from intent_engineering.assessment.policy import AssessmentPolicy
from intent_engineering.assessment.rollup import roll_up
from intent_engineering.assessment.rubric import assess_node
from intent_engineering.core.models import Edge, EvidenceRecord, Graph, NodeType, RelationType

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "assessment" / "rubric-v1.yaml"
_DIGESTS = {
    "graph_digest": "sha256:" + "1" * 64,
    "evidence_digest": "sha256:" + "2" * 64,
    "ingestion_digest": "sha256:" + "3" * 64,
    "case_digest": "sha256:" + "4" * 64,
    "clarification_digest": "sha256:" + "5" * 64,
    "history_digest": "sha256:" + "6" * 64,
    "config_digest": "sha256:" + "7" * 64,
    "principal_projection_digest": "sha256:" + "8" * 64,
    "aggregate_digest": "sha256:" + "9" * 64,
}


def _snapshot() -> AssessmentSnapshot:
    payload = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    graph = Graph.model_validate(payload["graph"])
    evidence = tuple(EvidenceRecord.model_validate(item) for item in payload["evidence"])
    return AssessmentSnapshot(
        project_id=payload["project_id"],
        graph=graph,
        evidence=evidence,
        ingestions=(),
        cases=(),
        clarifications=(),
        history=(),
        **_DIGESTS,
    )


def _scorecards(snapshot: AssessmentSnapshot, policy: AssessmentPolicy):
    return tuple(assess_node(snapshot, node.id, policy) for node in snapshot.graph.nodes)


def test_red_critical_path_caps_branch_and_project_at_49() -> None:
    """Catches a healthy-node mean hiding one red critical requirement."""
    snapshot = _snapshot()
    policy = AssessmentPolicy.v1()

    project, branches = roll_up(snapshot, _scorecards(snapshot, policy), policy)

    assert branches[0].branch_id == "intent:export"
    assert branches[0].robustness == 49
    assert project.robustness == 49
    assert project.health is AssessmentHealth.RED
    assert branches[0].node_ids == ("intent:export", "req:csv")
    assert dict(branches[0].contribution_weights) == {
        "intent:export": 1,
        "req:csv": 1,
    }


def test_branches_follow_only_active_policy_relations_and_stop_at_cycles() -> None:
    """Catches inactive, undeclared, or cyclic topology leaking into critical membership."""
    snapshot = _snapshot()
    graph = snapshot.graph
    req = next(node for node in graph.nodes if node.id == "req:csv")
    file_node = next(node for node in graph.nodes if node.id == "file:export")
    cycle = Edge(
        id="edge:cycle",
        from_id=req.id,
        relation=RelationType.REFINES,
        to_id="intent:export",
        status="active",
        created_by=req.created_by,
        created_at=req.created_at,
        last_modified_by=req.last_modified_by,
        last_modified_at=req.last_modified_at,
    )
    inactive = cycle.model_copy(
        update={"id": "edge:inactive", "from_id": "intent:export", "status": "superseded"}
    )
    undeclared = cycle.model_copy(
        update={
            "id": "edge:undeclared",
            "from_id": "intent:export",
            "to_id": file_node.id,
            "relation": RelationType.IMPLEMENTED_BY,
        }
    )
    changed = graph.model_copy(update={"edges": (*graph.edges, cycle, inactive, undeclared)})
    changed_snapshot = snapshot.model_copy(update={"graph": changed})
    policy = AssessmentPolicy.v1()

    project, branches = roll_up(changed_snapshot, _scorecards(changed_snapshot, policy), policy)

    assert branches[0].node_ids == ("intent:export", "req:csv")
    assert project.contributing_node_ids == ("intent:export", "req:csv")


def test_project_uses_published_custom_branch_weights() -> None:
    """Catches branch policy overrides being ignored or omitted from the project scorecard."""
    snapshot = _snapshot()
    graph = snapshot.graph
    intent = next(node for node in graph.nodes if node.id == "intent:export")
    outcome = intent.model_copy(update={"id": "outcome:empty", "type": NodeType.DESIRED_OUTCOME})
    changed = graph.model_copy(update={"nodes": (*graph.nodes, outcome)})
    changed_snapshot = snapshot.model_copy(update={"graph": changed})
    baseline = AssessmentPolicy.v1()
    policy = AssessmentPolicy.model_validate(
        {
            **baseline.model_dump(),
            "red_below": 1,
            "branch_weights": {"intent:export": 1, "outcome:empty": 3},
        }
    )

    project, branches = roll_up(changed_snapshot, _scorecards(changed_snapshot, policy), policy)

    assert tuple(branch.branch_id for branch in branches) == ("intent:export", "outcome:empty")
    assert [branch.robustness for branch in branches] == [91, 80]
    assert project.robustness == 82
    assert dict(project.contribution_weights) == {"intent:export": 1, "outcome:empty": 3}


def test_red_critical_path_outranks_an_unassessed_member() -> None:
    """Catches one unsupported critical member downgrading a known red path to unassessed."""
    snapshot = _snapshot()
    graph = snapshot.graph
    requirement = next(node for node in graph.nodes if node.id == "req:csv")
    capability = requirement.model_copy(
        update={"id": "capability:export", "type": NodeType.CAPABILITY}
    )
    root_edge = graph.edges[0].model_copy(
        update={"id": "edge:intent-capability", "to_id": capability.id}
    )
    requirement_edge = graph.edges[0].model_copy(
        update={
            "id": "edge:capability-requirement",
            "from_id": capability.id,
            "to_id": requirement.id,
        }
    )
    changed = graph.model_copy(
        update={
            "nodes": (*graph.nodes, capability),
            "edges": (*graph.edges, root_edge, requirement_edge),
        }
    )
    changed_snapshot = snapshot.model_copy(update={"graph": changed})
    baseline = AssessmentPolicy.v1()
    policy = AssessmentPolicy.model_validate(
        {
            **baseline.model_dump(),
            "critical_node_types": (*baseline.critical_node_types, NodeType.CAPABILITY),
        }
    )

    project, branches = roll_up(changed_snapshot, _scorecards(changed_snapshot, policy), policy)

    assert branches[0].node_ids == ("intent:export", "req:csv")
    assert dict(branches[0].contribution_weights) == {
        "intent:export": 1,
        "req:csv": 1,
    }
    assert (branches[0].robustness, branches[0].confidence, branches[0].health) == (
        49,
        85,
        AssessmentHealth.RED,
    )
    assert (project.robustness, project.confidence, project.health) == (
        49,
        85,
        AssessmentHealth.RED,
    )
