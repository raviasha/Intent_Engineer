"""Literal behavioral contracts for the deterministic rubric version 1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.assessment.models import (
    AssessmentDimension,
    AssessmentHealth,
    AssessmentSnapshot,
)
from intent_engineering.assessment.policy import AssessmentPolicy
from intent_engineering.assessment.rubric import applicability, assess_node
from intent_engineering.core.models import (
    Edge,
    EvidenceRecord,
    EvidenceSide,
    Graph,
    Node,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    RelationType,
    TypeRegistry,
)

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


@dataclass(frozen=True)
class RubricFixture:
    """A visible snapshot plus convenient stable graph accessors."""

    snapshot: AssessmentSnapshot

    @property
    def graph(self) -> Graph:
        return self.snapshot.graph

    def node(self, node_id: str) -> Node:
        return next(node for node in self.graph.nodes if node.id == node_id)


@pytest.fixture
def rubric_snapshot() -> RubricFixture:
    """Load the hand-authored visible graph used by rubric contract tests."""
    payload = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    graph = Graph.model_validate(payload["graph"])
    evidence = tuple(EvidenceRecord.model_validate(item) for item in payload["evidence"])
    return RubricFixture(
        AssessmentSnapshot(
            project_id=payload["project_id"],
            graph=graph,
            evidence=evidence,
            ingestions=(),
            cases=(),
            clarifications=(),
            history=(),
            **_DIGESTS,
        )
    )


def _with_snapshot(
    fixture: RubricFixture,
    *,
    graph: Graph | None = None,
    evidence: tuple[EvidenceRecord, ...] | None = None,
    cases: tuple[ReconciliationCase, ...] | None = None,
) -> AssessmentSnapshot:
    return fixture.snapshot.model_copy(
        update={
            "graph": fixture.graph if graph is None else graph,
            "evidence": fixture.snapshot.evidence if evidence is None else evidence,
            "cases": fixture.snapshot.cases if cases is None else cases,
        }
    )


def _replace_node(graph: Graph, replacement: Node) -> Graph:
    return graph.model_copy(
        update={
            "nodes": tuple(replacement if node.id == replacement.id else node for node in graph.nodes)
        }
    )


def _replace_edges(graph: Graph, edges: tuple[Edge, ...]) -> Graph:
    return graph.model_copy(update={"edges": edges})


@pytest.mark.parametrize(
    ("node_id", "dimension", "want"),
    [
        ("intent:export", AssessmentDimension.TEST_VERIFICATION, "inherited"),
        ("req:csv", AssessmentDimension.TEST_VERIFICATION, "required"),
        ("file:export", AssessmentDimension.INTENT_CLARITY, "not_applicable"),
    ],
)
def test_v1_applicability_is_explicit(
    rubric_snapshot: RubricFixture,
    node_id: str,
    dimension: AssessmentDimension,
    want: str,
) -> None:
    """Catches node-type applicability silently changing across rubric versions."""
    assert applicability(rubric_snapshot.node(node_id), rubric_snapshot.graph)[dimension] == want


def test_failed_rules_deduct_once_and_explain_the_score(rubric_snapshot: RubricFixture) -> None:
    """Catches an absent current test link being hidden or deducted more than once."""
    scorecard = assess_node(rubric_snapshot.snapshot, "req:csv", AssessmentPolicy.v1())

    verification = scorecard.dimension(AssessmentDimension.TEST_VERIFICATION)
    assert verification.score == 40
    assert [item.rule_id for item in verification.failed] == [
        "rubric:v1:test_verification:no_current_test_evidence"
    ]
    assert scorecard.health is AssessmentHealth.RED


@pytest.mark.parametrize(
    ("node_update", "dimension", "score", "health"),
    [
        (
            {"source_mode": None, "intent_fidelity_confidence": None},
            AssessmentDimension.INTENT_CLARITY,
            49,
            AssessmentHealth.RED,
        ),
        (
            {"evidence_refs": ()},
            AssessmentDimension.EVIDENCE_STRENGTH,
            50,
            AssessmentHealth.ORANGE,
        ),
        (
            {},
            AssessmentDimension.IMPLEMENTATION_TRACEABILITY,
            74,
            AssessmentHealth.ORANGE,
        ),
        (
            {},
            AssessmentDimension.FRESHNESS,
            75,
            AssessmentHealth.GREEN,
        ),
    ],
)
def test_v1_dimension_boundaries_are_literal(
    rubric_snapshot: RubricFixture,
    node_update: dict[str, object],
    dimension: AssessmentDimension,
    score: int,
    health: AssessmentHealth,
) -> None:
    """Catches deductions crossing the 49/50/74/75 health boundaries."""
    graph = rubric_snapshot.graph
    if node_update:
        graph = _replace_node(graph, rubric_snapshot.node("req:csv").model_copy(update=node_update))
    if dimension is AssessmentDimension.IMPLEMENTATION_TRACEABILITY:
        graph = _replace_edges(
            graph,
            tuple(edge for edge in graph.edges if edge.id != "edge:implemented-by"),
        )
    if dimension is AssessmentDimension.FRESHNESS:
        current = next(item for item in rubric_snapshot.snapshot.evidence if item.id == "evidence:req")
        newer = current.model_copy(
            update={"id": "evidence:req:new", "external_version": "2", "observed_at": datetime(2026, 9, 2, tzinfo=UTC)}
        )
        snapshot = _with_snapshot(rubric_snapshot, graph=graph, evidence=(*rubric_snapshot.snapshot.evidence, newer))
    else:
        snapshot = _with_snapshot(rubric_snapshot, graph=graph)

    result = assess_node(snapshot, "req:csv", AssessmentPolicy.v1()).dimension(dimension)

    assert (result.score, result.health) == (score, health)


def test_v1_scores_start_at_one_hundred_and_clamp_at_zero(rubric_snapshot: RubricFixture) -> None:
    """Catches score initialization or clamping that changes bounded deductions."""
    baseline = assess_node(rubric_snapshot.snapshot, "req:csv", AssessmentPolicy.v1())
    contradiction = rubric_snapshot.graph.edges[0].model_copy(
        update={"id": "edge:contradiction", "relation": RelationType.CONTRADICTS, "to_id": "req:csv"}
    )
    graph = _replace_edges(rubric_snapshot.graph, (*rubric_snapshot.graph.edges, contradiction))
    blocked = assess_node(_with_snapshot(rubric_snapshot, graph=graph), "req:csv", AssessmentPolicy.v1())

    assert baseline.dimension(AssessmentDimension.EVIDENCE_STRENGTH).score == 100
    assert blocked.dimension(AssessmentDimension.CONSISTENCY).score == 0


def test_duplicate_evidence_references_are_not_double_counted(rubric_snapshot: RubricFixture) -> None:
    """Catches duplicate visible references making a stable score change."""
    duplicate = rubric_snapshot.node("req:csv").model_copy(
        update={"evidence_refs": ("evidence:req", "evidence:req")}
    )
    snapshot = _with_snapshot(rubric_snapshot, graph=_replace_node(rubric_snapshot.graph, duplicate))

    baseline = assess_node(rubric_snapshot.snapshot, "req:csv", AssessmentPolicy.v1())
    repeated = assess_node(snapshot, "req:csv", AssessmentPolicy.v1())

    assert repeated == baseline


def test_missing_visible_evidence_lowers_confidence_without_using_labels(
    rubric_snapshot: RubricFixture,
) -> None:
    """Catches a missing visible input being inferred from node labels or treated as complete."""
    missing = rubric_snapshot.node("req:csv").model_copy(
        update={"label": "unrelated words", "evidence_refs": ("evidence:hidden",)}
    )
    snapshot = _with_snapshot(rubric_snapshot, graph=_replace_node(rubric_snapshot.graph, missing))

    result = assess_node(snapshot, "req:csv", AssessmentPolicy.v1()).dimension(
        AssessmentDimension.EVIDENCE_STRENGTH
    )

    assert (result.score, result.confidence) == (50, 0)


def test_external_relation_without_a_visible_target_is_ignored(rubric_snapshot: RubricFixture) -> None:
    """Catches a visible external edge making assessment dereference hidden topology."""
    external = rubric_snapshot.graph.edges[0].model_copy(
        update={"to_id": "req:hidden", "external": True}
    )
    graph = _replace_edges(
        rubric_snapshot.graph,
        (external, rubric_snapshot.graph.edges[1]),
    )

    coverage = assess_node(
        _with_snapshot(rubric_snapshot, graph=graph), "intent:export", AssessmentPolicy.v1()
    ).dimension(AssessmentDimension.REQUIREMENT_COVERAGE)

    assert coverage.score == 49


def test_blocking_case_overrides_a_high_average(rubric_snapshot: RubricFixture) -> None:
    """Catches a blocking visible reconciliation case being diluted by healthy dimensions."""
    blocking_case = ReconciliationCase(
        id="blocking",
        subject_ref="req:csv",
        case_type=ReconciliationCaseType.CONFLICTING_SOURCES,
        affected_refs=("req:csv",),
        evidence_sides=(
            EvidenceSide(
                label="source-a",
                claim="conflict",
                evidence_refs=("evidence:req",),
                observed_at=datetime(2026, 9, 1, tzinfo=UTC),
                authors=("Asha",),
                confidence=0.9,
            ),
        ),
        detector_id="detector:fixture",
        fingerprint="a" * 64,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        created_by="local:asha",
        status=ReconciliationStatus.OPEN,
    )

    scorecard = assess_node(
        _with_snapshot(rubric_snapshot, cases=(blocking_case,)), "req:csv", AssessmentPolicy.v1()
    )

    assert scorecard.health is AssessmentHealth.RED
    assert scorecard.robustness == 49
    assert scorecard.blocking_case_refs == ("blocking",)


def test_unknown_node_types_are_unassessed(rubric_snapshot: RubricFixture) -> None:
    """Catches new graph vocabulary receiving an implicit rubric pass."""
    unknown = rubric_snapshot.node("req:csv").model_copy(
        update={"id": "future:csv", "type": "vendor:FUTURE_REQUIREMENT"}
    )
    graph = rubric_snapshot.graph.model_copy(
        update={
            "nodes": (rubric_snapshot.node("intent:export"), unknown, rubric_snapshot.node("file:export")),
            "edges": (),
            "type_registry": TypeRegistry(extensions=frozenset({"vendor:FUTURE_REQUIREMENT"})),
        }
    )

    scorecard = assess_node(_with_snapshot(rubric_snapshot, graph=graph), "future:csv", AssessmentPolicy.v1())

    assert scorecard.health is AssessmentHealth.UNASSESSED
    assert scorecard.robustness is None
    assert all(result.score is None for result in scorecard.dimensions)


def test_assessment_is_stable_under_visible_input_permutation(rubric_snapshot: RubricFixture) -> None:
    """Catches traversal or collection order leaking into detached scorecards."""
    permuted_graph = rubric_snapshot.graph.model_copy(
        update={"nodes": tuple(reversed(rubric_snapshot.graph.nodes)), "edges": tuple(reversed(rubric_snapshot.graph.edges))}
    )
    permuted = _with_snapshot(
        rubric_snapshot,
        graph=permuted_graph,
        evidence=tuple(reversed(rubric_snapshot.snapshot.evidence)),
    )

    assert assess_node(permuted, "req:csv", AssessmentPolicy.v1()) == assess_node(
        rubric_snapshot.snapshot, "req:csv", AssessmentPolicy.v1()
    )
