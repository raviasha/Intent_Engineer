"""Integration contracts for snapshot-bound graph assessment reports."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.assessment.models import AssessmentDimension, AssessmentSnapshot
from intent_engineering.assessment.policy import AssessmentPolicy
from intent_engineering.assessment.service import GraphAssessmentService
from intent_engineering.assessment.snapshot import AssessmentUnavailable
from intent_engineering.core.models import Edge, EvidenceRecord, Graph, NodeType, RelationType

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "assessment" / "rubric-v1.yaml"
_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
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


@pytest.fixture
def complete_snapshot() -> AssessmentSnapshot:
    payload = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    return AssessmentSnapshot(
        project_id=payload["project_id"],
        graph=Graph.model_validate(payload["graph"]),
        evidence=tuple(EvidenceRecord.model_validate(item) for item in payload["evidence"]),
        ingestions=(),
        cases=(),
        clarifications=(),
        history=(),
        **_DIGESTS,
    )


@pytest.fixture
def service() -> GraphAssessmentService:
    return GraphAssessmentService(clock=lambda: _NOW)


def _digest(marker: str) -> str:
    return "sha256:" + hashlib.sha256(marker.encode()).hexdigest()


def test_assessment_package_exports_the_report_service() -> None:
    """Catches callers having to depend on the service module's private package layout."""
    from intent_engineering import assessment

    assert assessment.GraphAssessmentService is GraphAssessmentService


def test_identical_snapshot_is_semantically_byte_identical(
    service: GraphAssessmentService,
    complete_snapshot: AssessmentSnapshot,
) -> None:
    """Catches presentation time or traversal order entering stable report identity."""
    first = service.assess(complete_snapshot)
    second = service.assess(complete_snapshot)

    assert first.semantic_bytes() == second.semantic_bytes()
    assert first is second
    assert first.snapshot_digest == complete_snapshot.aggregate_digest
    assert first.generated_at == _NOW


def test_identical_concurrent_assessments_converge_on_one_detached_report(
    service: GraphAssessmentService,
    complete_snapshot: AssessmentSnapshot,
) -> None:
    """Catches races publishing multiple reports for the same exact assessment identity."""
    with ThreadPoolExecutor(max_workers=8) as executor:
        reports = tuple(executor.map(service.assess, (complete_snapshot,) * 32))

    assert len({id(report) for report in reports}) == 1
    assert service.cache_entry_count == 1


def test_report_chooses_the_worst_dimension_by_health_severity_and_stable_id(
    service: GraphAssessmentService,
    complete_snapshot: AssessmentSnapshot,
) -> None:
    """Catches lower raw points overriding the specified stable worst-gap ordering."""
    requirement = next(node for node in complete_snapshot.graph.nodes if node.id == "req:csv")
    unclear = requirement.model_copy(
        update={"source_mode": None, "intent_fidelity_confidence": None}
    )
    graph = complete_snapshot.graph.model_copy(
        update={
            "nodes": tuple(
                unclear if node.id == unclear.id else node for node in complete_snapshot.graph.nodes
            )
        }
    )
    snapshot = complete_snapshot.model_copy(
        update={"graph": graph, "aggregate_digest": _digest("unclear requirement")}
    )

    scorecard = service.assess(snapshot).node(requirement.id)

    assert scorecard.worst_dimension is AssessmentDimension.INTENT_CLARITY


def test_snapshot_principal_and_policy_changes_never_share_cache_entries(
    service: GraphAssessmentService,
    complete_snapshot: AssessmentSnapshot,
) -> None:
    """Catches ACL projection, snapshot, or policy identities aliasing cached output."""
    changed_snapshot = complete_snapshot.model_copy(
        update={"aggregate_digest": _digest("changed snapshot")}
    )
    changed_principal = complete_snapshot.model_copy(
        update={"principal_projection_digest": _digest("changed principal")}
    )
    baseline = AssessmentPolicy.v1()
    changed_policy = AssessmentPolicy.model_validate(
        {**baseline.model_dump(), "green_confidence_at": 80}
    )

    reports = (
        service.assess(complete_snapshot, baseline),
        service.assess(changed_snapshot, baseline),
        service.assess(changed_principal, baseline),
        service.assess(complete_snapshot, changed_policy),
    )

    assert len({id(report) for report in reports}) == 4
    assert service.cache_entry_count == 4


def test_service_reports_only_the_acl_closed_snapshot(assessment_runtime) -> None:
    """Catches assessment traversing back to hidden store topology after snapshot construction."""
    snapshot = assessment_runtime.runtime.assessment_snapshot("local:asha")
    report = GraphAssessmentService(clock=lambda: _NOW).assess(snapshot)

    assert tuple(scorecard.node_id for scorecard in report.nodes) == (
        "intent:public",
        "req:public",
    )
    assert report.branch("intent:public").node_ids == ("intent:public", "req:public")
    assert "hidden" not in report.model_dump_json()


def test_cache_is_lru_bounded_to_thirty_two_entries(
    service: GraphAssessmentService,
    complete_snapshot: AssessmentSnapshot,
) -> None:
    """Catches detached report retention growing beyond the fixed in-memory bound."""
    first = service.assess(complete_snapshot)
    for index in range(32):
        service.assess(
            complete_snapshot.model_copy(update={"aggregate_digest": _digest(str(index))})
        )

    assert service.cache_entry_count == 32
    assert service.assess(complete_snapshot) is not first


def test_projected_assessment_is_in_memory_and_keeps_approved_report_unchanged(
    service: GraphAssessmentService,
    complete_snapshot: AssessmentSnapshot,
) -> None:
    """Catches projection mutating the approved graph or replacing approved scores."""
    graph = complete_snapshot.graph
    requirement = next(node for node in graph.nodes if node.id == "req:csv")
    file_node = next(node for node in graph.nodes if node.id == "file:export")
    test_node = file_node.model_copy(update={"id": "test:csv", "type": NodeType.TEST})
    test_edge = Edge(
        id="edge:verified-by",
        from_id=requirement.id,
        relation=RelationType.VERIFIED_BY,
        to_id=test_node.id,
        status="active",
        created_by=requirement.created_by,
        created_at=requirement.created_at,
        last_modified_by=requirement.last_modified_by,
        last_modified_at=requirement.last_modified_at,
    )
    proposed = Graph.model_validate(
        {
            **graph.model_dump(mode="python", by_alias=True),
            "nodes": (*graph.nodes, test_node),
            "edges": (*graph.edges, test_edge),
        }
    )
    before = graph.model_dump_json(by_alias=True)

    comparison = service.projected(complete_snapshot, proposed)

    assert comparison.current.node("req:csv").robustness == 91
    assert comparison.projected.node("req:csv").robustness == 100
    assert comparison.current.snapshot_digest == complete_snapshot.aggregate_digest
    assert comparison.projected.snapshot_digest != comparison.current.snapshot_digest
    assert complete_snapshot.graph.model_dump_json(by_alias=True) == before


def test_projected_assessment_rejects_a_stale_or_foreign_graph_with_one_fixed_error(
    service: GraphAssessmentService,
    complete_snapshot: AssessmentSnapshot,
) -> None:
    """Catches an overlay escaping assessment after its graph identity no longer matches."""
    for proposed in (
        complete_snapshot.graph.model_copy(update={"version": 0}),
        complete_snapshot.graph.model_copy(update={"id": "graph:foreign"}),
    ):
        with pytest.raises(AssessmentUnavailable, match="^assessment unavailable$"):
            service.projected(complete_snapshot, proposed)
