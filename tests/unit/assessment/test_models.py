"""Behavioral contracts for detached assessment records."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from intent_engineering.assessment.models import (
    AssessmentDimension,
    AssessmentHealth,
    AssessmentReport,
    BranchScorecard,
    DimensionApplicability,
    DimensionResult,
    NodeScorecard,
    ProjectScorecard,
    RubricCheck,
)
from intent_engineering.core.models import NodeType


def _dimension(dimension: AssessmentDimension) -> DimensionResult:
    return DimensionResult(
        dimension=dimension,
        applicability=DimensionApplicability.REQUIRED,
        score=80,
        health=AssessmentHealth.GREEN,
        confidence=90,
        passed=(
            RubricCheck(
                rule_id=f"rubric:v1:{dimension.value}:supported",
                points=0,
                severity=AssessmentHealth.GREEN,
                explanation="The required input is present.",
                references=("evidence:public",),
            ),
        ),
        failed=(),
        evidence_refs=("evidence:public",),
        related_refs=("node:intent:export",),
        recommended_next_action=None,
    )


def _node(node_id: str) -> NodeScorecard:
    dimensions = tuple(_dimension(dimension) for dimension in AssessmentDimension)
    return NodeScorecard(
        node_id=node_id,
        node_type=NodeType.REQUIREMENT,
        robustness=80,
        confidence=90,
        health=AssessmentHealth.GREEN,
        worst_dimension=AssessmentDimension.CONSISTENCY,
        dimensions=dimensions,
        blocking_case_refs=(),
        recommended_next_action=None,
    )


def report_fixture(*, node_ids: tuple[str, ...]) -> AssessmentReport:
    """Create a complete detached report without reading canonical state."""
    return AssessmentReport(
        project_id="project:alpha",
        graph_id="graph:alpha",
        graph_version=7,
        graph_digest="sha256:" + "1" * 64,
        evidence_digest="sha256:" + "2" * 64,
        ingestion_digest="sha256:" + "3" * 64,
        case_digest="sha256:" + "4" * 64,
        clarification_digest="sha256:" + "5" * 64,
        history_digest="sha256:" + "6" * 64,
        policy_digest="sha256:" + "7" * 64,
        snapshot_digest="sha256:" + "8" * 64,
        principal_projection_digest="sha256:" + "9" * 64,
        generated_at=datetime(2026, 9, 2, tzinfo=UTC),
        project=ProjectScorecard(
            project_id="project:alpha",
            robustness=80,
            confidence=90,
            health=AssessmentHealth.GREEN,
            branch_ids=("intent:export",),
            contributing_node_ids=tuple(sorted(node_ids)),
        ),
        branches=(
            BranchScorecard(
                branch_id="intent:export",
                root_node_id="intent:export",
                node_ids=tuple(sorted(node_ids)),
                robustness=80,
                confidence=90,
                health=AssessmentHealth.GREEN,
            ),
        ),
        nodes=tuple(_node(node_id) for node_id in node_ids),
        gaps=(),
        warnings=(),
        assessment_complete=True,
    )


def test_report_is_strict_frozen_and_canonically_ordered() -> None:
    """Catches ordering or mutability that would make reports non-reproducible."""
    report = report_fixture(node_ids=("req:z", "req:a"))

    assert tuple(item.node_id for item in report.nodes) == ("req:a", "req:z")
    assert AssessmentReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        AssessmentReport.model_validate({**report.model_dump(), "unknown": True})
    with pytest.raises(ValidationError):
        report.nodes = ()  # type: ignore[misc]


def test_dimensions_make_not_applicable_explicit_and_exclude_a_score() -> None:
    """Catches treating a non-applicable dimension as a zero-score failure."""
    result = DimensionResult(
        dimension=AssessmentDimension.TEST_VERIFICATION,
        applicability=DimensionApplicability.NOT_APPLICABLE,
        score=None,
        health=AssessmentHealth.UNASSESSED,
        confidence=None,
        passed=(),
        failed=(),
        evidence_refs=(),
        related_refs=(),
        recommended_next_action=None,
    )

    assert result.score is None
    with pytest.raises(ValidationError, match="not applicable"):
        DimensionResult.model_validate({**result.model_dump(), "score": 0})


def test_scores_reject_booleans_and_out_of_range_values() -> None:
    """Catches truthy or unbounded values entering deterministic score rollups."""
    material = _node("req:export").model_dump()

    with pytest.raises(ValidationError, match="exact integer"):
        NodeScorecard.model_validate({**material, "robustness": True})
    with pytest.raises(ValidationError):
        NodeScorecard.model_validate({**material, "confidence": 101})
