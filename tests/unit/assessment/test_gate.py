"""Policy contracts for comparing detached assessment reports in CI."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from intent_engineering.assessment.gate import AssessmentGate, AssessmentGatePolicy
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

_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + "1" * 64


def _report(
    health: AssessmentHealth,
    *,
    branch_id: str = "intent:export",
    robustness: int | None = None,
    confidence: int = 90,
) -> AssessmentReport:
    score = (
        robustness
        if robustness is not None
        else {
            AssessmentHealth.GREEN: 90,
            AssessmentHealth.ORANGE: 70,
            AssessmentHealth.RED: 40,
        }[health]
    )
    dimension = DimensionResult(
        dimension=AssessmentDimension.CONSISTENCY,
        applicability=DimensionApplicability.REQUIRED,
        score=score,
        confidence=confidence,
        health=health,
    )
    node = NodeScorecard(
        node_id=f"req:{branch_id}",
        node_type=NodeType.REQUIREMENT,
        robustness=score,
        confidence=confidence,
        health=health,
        worst_dimension=AssessmentDimension.CONSISTENCY,
        dimensions=(dimension,),
    )
    branch = BranchScorecard(
        branch_id=branch_id,
        root_node_id=node.node_id,
        node_ids=(node.node_id,),
        robustness=score,
        confidence=confidence,
        health=health,
    )
    project = ProjectScorecard(
        project_id="project:gate",
        robustness=score,
        confidence=confidence,
        health=health,
        branch_ids=(branch_id,),
        contributing_node_ids=(node.node_id,),
    )
    return AssessmentReport(
        project_id=project.project_id,
        graph_id="graph:gate",
        graph_version=1,
        graph_digest=_DIGEST,
        evidence_digest=_DIGEST,
        ingestion_digest=_DIGEST,
        case_digest=_DIGEST,
        clarification_digest=_DIGEST,
        history_digest=_DIGEST,
        policy_digest=_DIGEST,
        snapshot_digest=_DIGEST,
        principal_projection_digest=_DIGEST,
        generated_at=_NOW,
        project=project,
        branches=(branch,),
        nodes=(node,),
        assessment_complete=True,
    )


def test_gate_fails_only_for_new_red_critical_gap_by_default() -> None:
    """Catches the default gate failing existing debt or non-red score changes."""
    gate = AssessmentGate()

    introduced = gate.evaluate(
        base=_report(AssessmentHealth.GREEN),
        head=_report(AssessmentHealth.RED),
    )
    existing = gate.evaluate(
        base=_report(AssessmentHealth.RED),
        head=_report(AssessmentHealth.RED),
    )
    regression = gate.evaluate(
        base=_report(AssessmentHealth.GREEN, robustness=95),
        head=_report(AssessmentHealth.GREEN, robustness=80),
    )

    assert introduced.exit_code == 5
    assert introduced.failures == ("new_red:intent:export",)
    assert existing.exit_code == regression.exit_code == 0
    assert existing.failures == regression.failures == ()


def test_gate_detects_a_new_red_branch_when_other_red_debt_already_exists() -> None:
    """Catches project-level red masking a newly broken critical intent branch."""
    base = _report(AssessmentHealth.RED, branch_id="intent:existing")
    new_branch = _report(AssessmentHealth.RED, branch_id="intent:new").branches[0]
    new_node = _report(AssessmentHealth.RED, branch_id="intent:new").nodes[0]
    head = base.model_copy(
        update={
            "branches": (*base.branches, new_branch),
            "nodes": (*base.nodes, new_node),
            "project": base.project.model_copy(
                update={
                    "branch_ids": ("intent:existing", "intent:new"),
                    "contributing_node_ids": (base.nodes[0].node_id, new_node.node_id),
                    "contribution_weights": {"intent:existing": 1, "intent:new": 1},
                }
            ),
        }
    )

    result = AssessmentGate().evaluate(base=base, head=head)

    assert result.exit_code == 5
    assert result.failures == ("new_red:intent:new",)


def test_gate_detects_a_new_red_critical_node_inside_an_existing_red_branch() -> None:
    """Catches an already-red branch masking additional newly broken critical behavior."""
    base = _report(AssessmentHealth.RED)
    added = _report(AssessmentHealth.RED, branch_id="intent:added").nodes[0]
    head = base.model_copy(
        update={
            "nodes": (*base.nodes, added),
            "branches": (
                base.branches[0].model_copy(
                    update={
                        "node_ids": (*base.branches[0].node_ids, added.node_id),
                        "contribution_weights": {
                            base.nodes[0].node_id: 1,
                            added.node_id: 1,
                        },
                    }
                ),
            ),
            "project": base.project.model_copy(
                update={
                    "contributing_node_ids": (*base.project.contributing_node_ids, added.node_id)
                }
            ),
        }
    )

    result = AssessmentGate().evaluate(base=base, head=head)

    assert result.exit_code == 5
    assert result.failures == (f"new_red:{added.node_id}",)


def test_gate_diffs_stable_red_rubric_gap_identity_on_an_already_red_node() -> None:
    """Catches a newly failed rule hiding behind an unchanged red node and branch color."""
    base = _report(AssessmentHealth.RED)
    existing = RubricCheck(
        rule_id="rubric:v1:consistency:existing",
        points=60,
        severity=AssessmentHealth.RED,
        explanation="Existing critical contradiction",
        references=("evidence:existing",),
    )
    introduced = RubricCheck(
        rule_id="rubric:v1:consistency:introduced",
        points=20,
        severity=AssessmentHealth.RED,
        explanation="New critical contradiction",
        references=("evidence:new", "case:new"),
    )

    def with_checks(report: AssessmentReport, checks: tuple[RubricCheck, ...]) -> AssessmentReport:
        dimension = report.nodes[0].dimensions[0].model_copy(update={"failed": checks})
        node = report.nodes[0].model_copy(update={"dimensions": (dimension,)})
        return report.model_copy(update={"nodes": (node,)})

    result = AssessmentGate().evaluate(
        base=with_checks(base, (existing,)),
        head=with_checks(base, (existing, introduced)),
    )

    assert result.exit_code == 5
    assert len(result.new_red_gaps) == 1
    assert result.new_red_gaps[0].rule_id == introduced.rule_id
    assert result.new_red_gaps[0].critical_node_id == base.nodes[0].node_id
    assert result.new_red_gaps[0].dimension is AssessmentDimension.CONSISTENCY
    assert result.new_red_gaps[0].references == ("case:new", "evidence:new")
    assert result.failures == (f"new_red_gap:{result.new_red_gaps[0].digest}",)


def test_gate_reference_churn_does_not_create_a_new_red_gap_identity() -> None:
    """Catches diagnostic evidence-reference churn being treated as a new failed rule."""
    report = _report(AssessmentHealth.RED)

    def with_reference(reference: str) -> AssessmentReport:
        failed = (
            RubricCheck(
                rule_id="rubric:v1:consistency:existing",
                points=60,
                severity=AssessmentHealth.RED,
                explanation="Existing critical contradiction",
                references=(reference,),
            ),
        )
        dimension = report.nodes[0].dimensions[0].model_copy(update={"failed": failed})
        node = report.nodes[0].model_copy(update={"dimensions": (dimension,)})
        return report.model_copy(update={"nodes": (node,)})

    result = AssessmentGate().evaluate(
        base=with_reference("evidence:old"),
        head=with_reference("evidence:new"),
    )

    assert result.exit_code == 0
    assert result.new_red_gaps == ()
    assert result.failures == ()


def test_gate_supports_minimum_confidence_orange_warnings_and_regressions() -> None:
    """Catches opt-in gate rules being ignored or accidentally made fatal warnings."""
    base = _report(AssessmentHealth.GREEN, robustness=90, confidence=90)
    orange = _report(AssessmentHealth.ORANGE, robustness=70, confidence=60)
    result = AssessmentGate().evaluate(
        base=base,
        head=orange,
        policy=AssessmentGatePolicy(
            minimum_confidence=75,
            orange_warning=True,
            robustness_regression=True,
        ),
    )

    assert result.exit_code == 5
    assert result.failures == (
        "minimum_confidence:intent:export:60<75",
        "robustness_regression:intent:export:90>70",
        "robustness_regression:project:90>70",
    )
    assert result.warnings == ("orange:intent:export",)


def test_gate_results_are_versioned_and_bind_both_report_identities() -> None:
    """Catches a CI decision that cannot be traced to its exact base and head reports."""
    base = _report(AssessmentHealth.GREEN)
    head = _report(AssessmentHealth.RED)

    result = AssessmentGate().evaluate(base=base, head=head)
    policy = AssessmentGatePolicy()

    assert result.schema_version == 1
    assert result.base_assessment_digest == base.semantic_digest
    assert result.head_assessment_digest == head.semantic_digest
    assert result.gate_policy_digest == policy.digest


def test_gate_rejects_incomparable_report_authority() -> None:
    """Catches comparisons crossing repositories, actors, or scoring policies."""
    base = _report(AssessmentHealth.GREEN)
    for head in (
        base.model_copy(update={"project_id": "project:other"}),
        base.model_copy(update={"graph_id": "graph:other"}),
        base.model_copy(update={"policy_digest": "sha256:" + "2" * 64}),
        base.model_copy(update={"principal_projection_digest": "sha256:" + "3" * 64}),
    ):
        with pytest.raises(ValueError, match="^assessment gate unavailable$"):
            AssessmentGate().evaluate(base=base, head=head)


def test_assessment_package_exports_gate_contracts() -> None:
    """Catches callers depending on the gate module's private package layout."""
    from intent_engineering import assessment

    assert assessment.AssessmentGate is AssessmentGate
    assert assessment.AssessmentGatePolicy is AssessmentGatePolicy
