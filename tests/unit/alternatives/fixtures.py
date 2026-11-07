"""Detached fixtures shared by alternative contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

from intent_engineering.assessment import (
    AssessmentDimension,
    AssessmentHealth,
    AssessmentReport,
    AssessmentSnapshot,
    BranchScorecard,
    DimensionApplicability,
    DimensionResult,
    NodeScorecard,
    ProjectScorecard,
)
from intent_engineering.core.models import (
    Edge,
    EvidenceRecord,
    Graph,
    Node,
    NodeType,
    RelationType,
    SourceMode,
)


def _node(node_id: str, node_type: NodeType, label: str, *evidence_refs: str) -> Node:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    return Node(
        id=node_id,
        type=node_type,
        label=label,
        status="active",
        created_by="actor:owner",
        created_at=now,
        last_modified_by="actor:owner",
        last_modified_at=now,
        source_mode=SourceMode.EXPLICIT,
        intent_fidelity_confidence=0.9,
        confidence_basis="Captured from approved evidence.",
        evidence_refs=evidence_refs,
    )


def _edge(edge_id: str, source: str, relation: RelationType, target: str) -> Edge:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    return Edge(
        id=edge_id,
        **{"from": source, "to": target},
        relation=relation,
        status="active",
        created_by="actor:owner",
        created_at=now,
        last_modified_by="actor:owner",
        last_modified_at=now,
    )


def _scorecard(node: Node) -> NodeScorecard:
    dimensions = tuple(
        DimensionResult(
            dimension=dimension,
            applicability=DimensionApplicability.REQUIRED,
            score=55,
            health=AssessmentHealth.ORANGE,
            confidence=80,
        )
        for dimension in AssessmentDimension
    )
    return NodeScorecard(
        node_id=node.id,
        node_type=node.type,
        robustness=55,
        confidence=80,
        health=AssessmentHealth.ORANGE,
        worst_dimension=AssessmentDimension.REQUIREMENT_COVERAGE,
        dimensions=dimensions,
    )


def assessment_pair(*, include_hidden: bool = False) -> tuple[AssessmentSnapshot, AssessmentReport]:
    requirement = _node(
        "req:export",
        NodeType.REQUIREMENT,
        "Automate scheduled CSV export.",
        "evidence:visible",
    )
    intent = _node(
        "intent:portable-report",
        NodeType.PRODUCT_INTENT,
        "Customers can take their reports with them.",
        "evidence:visible",
    )
    outcome = _node(
        "outcome:portable-report",
        NodeType.DESIRED_OUTCOME,
        "A customer can obtain a portable report.",
        "evidence:visible",
    )
    constraint = _node(
        "constraint:weekly",
        NodeType.CONSTRAINT,
        "The report is available weekly.",
        "evidence:visible",
    )
    nodes = [requirement, intent, outcome, constraint]
    if include_hidden:
        nodes.append(
            _node(
                "req:hidden",
                NodeType.REQUIREMENT,
                "TOP-SECRET-HIDDEN-REQUIREMENT",
                "evidence:hidden",
            )
        )
    graph = Graph(
        id="graph:alpha",
        version=7,
        nodes=tuple(nodes),
        edges=(
            _edge("edge:req-intent", "req:export", RelationType.REFINES, intent.id),
            _edge("edge:intent-outcome", intent.id, RelationType.SEEKS_OUTCOME, outcome.id),
            _edge("edge:intent-constraint", intent.id, RelationType.CONSTRAINS, constraint.id),
        ),
    )
    visible = EvidenceRecord(
        id="evidence:visible",
        connector_type="markdown",
        external_object_id="docs/requirements.md",
        external_version="sha256:visible",
        author="actor:owner",
        observed_at=datetime(2026, 9, 2, tzinfo=UTC),
        source_locator="/private/workspace/docs/requirements.md",
        content_hash="sha256:" + "a" * 64,
        payload={"text": "A weekly report is sufficient.", "secret": "PRIVATE-SENTINEL"},
    )
    evidence = (visible,)
    if include_hidden:
        evidence += (
            EvidenceRecord(
                id="evidence:hidden",
                connector_type="markdown",
                external_object_id="secret.md",
                external_version="sha256:hidden",
                author="actor:hidden",
                observed_at=datetime(2026, 9, 2, tzinfo=UTC),
                source_locator="/private/hidden/secret.md",
                content_hash="sha256:" + "b" * 64,
                payload={"text": "TOP-SECRET-HIDDEN-EVIDENCE"},
                acl=("actor:hidden",),
            ),
        )
    snapshot = AssessmentSnapshot(
        project_id="project:alpha",
        graph=graph,
        evidence=evidence,
        ingestions=(),
        cases=(),
        clarifications=(),
        history=(),
        graph_digest="sha256:" + "1" * 64,
        evidence_digest="sha256:" + "2" * 64,
        ingestion_digest="sha256:" + "3" * 64,
        case_digest="sha256:" + "4" * 64,
        clarification_digest="sha256:" + "5" * 64,
        history_digest="sha256:" + "6" * 64,
        config_digest="sha256:" + "7" * 64,
        principal_projection_digest="sha256:" + "8" * 64,
        aggregate_digest="sha256:" + "9" * 64,
    )
    scorecards = tuple(_scorecard(node) for node in graph.nodes)
    report = AssessmentReport(
        project_id=snapshot.project_id,
        graph_id=graph.id,
        graph_version=graph.version,
        graph_digest=snapshot.graph_digest,
        evidence_digest=snapshot.evidence_digest,
        ingestion_digest=snapshot.ingestion_digest,
        case_digest=snapshot.case_digest,
        clarification_digest=snapshot.clarification_digest,
        history_digest=snapshot.history_digest,
        policy_digest="sha256:" + "c" * 64,
        snapshot_digest=snapshot.aggregate_digest,
        principal_projection_digest=snapshot.principal_projection_digest,
        generated_at=datetime(2026, 9, 2, tzinfo=UTC),
        project=ProjectScorecard(
            project_id=snapshot.project_id,
            robustness=55,
            confidence=80,
            health=AssessmentHealth.ORANGE,
            branch_ids=(intent.id,),
            contributing_node_ids=tuple(node.id for node in graph.nodes),
        ),
        branches=(
            BranchScorecard(
                branch_id=intent.id,
                root_node_id=intent.id,
                node_ids=tuple(node.id for node in graph.nodes),
                robustness=55,
                confidence=80,
                health=AssessmentHealth.ORANGE,
            ),
        ),
        nodes=scorecards,
        assessment_complete=True,
    )
    return snapshot, report
