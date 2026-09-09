"""Deterministic, non-canonical assessment overlays for generated graph views."""

from __future__ import annotations

import hashlib
import json

import pytest

from intent_engineering.assessment.models import (
    AssessmentDimension,
    AssessmentHealth,
    AssessmentReport,
    DimensionApplicability,
    DimensionResult,
    NodeScorecard,
    ProjectScorecard,
    RubricCheck,
)
from intent_engineering.render.markdown import render_markdown
from intent_engineering.render.mermaid import render_mermaid
from intent_engineering.render.renderer import GraphRenderer
from intent_engineering.storage.yaml.graph_store import YamlGraphStore

from .conftest import NOW, RenderFixture

_DIGEST = "sha256:" + "a" * 64


def _graph_digest(render_fixture: RenderFixture) -> str:
    content = json.dumps(
        render_fixture.graph.model_dump(mode="json", by_alias=True),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _assessment(render_fixture: RenderFixture) -> AssessmentReport:
    red_check = RubricCheck(
        rule_id="rubric:v1:test_verification:missing",
        points=60,
        severity=AssessmentHealth.RED,
        explanation="No current negative-path test <script>alert(1)</script>",
        references=("ev-render",),
    )
    red_dimension = DimensionResult(
        dimension=AssessmentDimension.TEST_VERIFICATION,
        applicability=DimensionApplicability.REQUIRED,
        score=40,
        health=AssessmentHealth.RED,
        confidence=82,
        failed=(red_check,),
        evidence_refs=("ev-render",),
        recommended_next_action="Add a current negative-path test",
    )
    green_dimension = DimensionResult(
        dimension=AssessmentDimension.INTENT_CLARITY,
        applicability=DimensionApplicability.REQUIRED,
        score=91,
        health=AssessmentHealth.GREEN,
        confidence=90,
        passed=(
            RubricCheck(
                rule_id="rubric:v1:intent_clarity:explicit",
                points=0,
                severity=AssessmentHealth.GREEN,
                explanation="Intent is explicit",
                references=(),
            ),
        ),
    )
    nodes = (
        NodeScorecard(
            node_id="req-a",
            node_type="REQUIREMENT",
            robustness=40,
            confidence=82,
            health=AssessmentHealth.RED,
            worst_dimension=AssessmentDimension.TEST_VERIFICATION,
            dimensions=(red_dimension,),
            recommended_next_action="Add a current negative-path test",
            projected_robustness=80,
            projected_confidence=82,
        ),
        NodeScorecard(
            node_id="req-z",
            node_type="REQUIREMENT",
            robustness=91,
            confidence=90,
            health=AssessmentHealth.GREEN,
            worst_dimension=AssessmentDimension.INTENT_CLARITY,
            dimensions=(green_dimension,),
        ),
    )
    return AssessmentReport(
        project_id="project:render",
        graph_id=render_fixture.graph.id,
        graph_version=render_fixture.graph.version,
        graph_digest=_graph_digest(render_fixture),
        evidence_digest=_DIGEST,
        ingestion_digest=_DIGEST,
        case_digest=_DIGEST,
        clarification_digest=_DIGEST,
        history_digest=_DIGEST,
        policy_digest=_DIGEST,
        snapshot_digest="sha256:" + "b" * 64,
        principal_projection_digest="sha256:" + "c" * 64,
        generated_at=NOW,
        project=ProjectScorecard(
            project_id="project:render",
            robustness=40,
            confidence=82,
            health=AssessmentHealth.RED,
            branch_ids=(),
            contributing_node_ids=("req-a", "req-z"),
        ),
        branches=(),
        nodes=nodes,
        gaps=(red_check,),
        assessment_complete=True,
    )


def test_mermaid_health_styles_are_stable_textual_and_noncanonical(
    render_fixture: RenderFixture,
) -> None:
    """Catches unstable, color-only, or canonical-writing Mermaid overlays."""
    assessment = _assessment(render_fixture)
    before = render_fixture.graph_path.read_bytes()

    first = render_mermaid(render_fixture.graph, assessment)
    second = render_mermaid(render_fixture.graph, assessment)

    assert first == second
    assert "classDef health_green" in first
    assert "classDef health_orange" in first
    assert "classDef health_red" in first
    assert "classDef health_unassessed" in first
    assert "× Red" in first and "✓ Green" in first
    assert "Score: 40" in first and "Confidence: 82" in first
    assert "◆ Worst dimension: Test verification" in first
    assert "<br/>" in first and "&lt;br/&gt;" not in first
    assert ":::health_red" in first and ":::health_green" in first
    assert "Non-canonical assessment overlay" in first
    assert assessment.snapshot_digest in first
    assert assessment.principal_projection_digest in first
    assert render_fixture.graph_path.read_bytes() == before


def test_markdown_assessment_table_and_deductions_are_stable_and_evidence_linked(
    render_fixture: RenderFixture,
) -> None:
    """Catches assessment scores or deductions diverging from the shared report."""
    assessment = _assessment(render_fixture)

    rendered = render_markdown(render_fixture.graph, render_fixture.cases, assessment)

    assert "## Assessment (non-canonical)" in rendered
    assert "| Node | Health | Robustness | Confidence | Worst dimension |" in rendered
    assert "`req-a` | × Red | 40 | 82 | Test verification" in rendered
    assert "### Evidence-linked deductions" in rendered
    assert r"No current negative-path test \<script\>alert(1)\</script\>" in rendered
    assert "`ev-render`" in rendered
    assert "60 points" in rendered
    assert assessment.snapshot_digest in rendered
    assert assessment.principal_projection_digest in rendered
    assert rendered == render_markdown(render_fixture.graph, render_fixture.cases, assessment)


@pytest.mark.parametrize("mutation", ("graph_id", "graph_version", "graph_digest", "nodes"))
def test_assessment_overlay_rejects_any_graph_projection_mismatch(
    render_fixture: RenderFixture,
    mutation: str,
) -> None:
    """Catches stale or hidden scorecards being copied into a visible projection."""
    assessment = _assessment(render_fixture)
    if mutation == "graph_id":
        assessment = assessment.model_copy(update={"graph_id": "other"})
    elif mutation == "graph_version":
        assessment = assessment.model_copy(update={"graph_version": 2})
    elif mutation == "graph_digest":
        assessment = assessment.model_copy(update={"graph_digest": _DIGEST})
    else:
        hidden = assessment.nodes[0].model_copy(update={"node_id": "req:hidden"})
        assessment = assessment.model_copy(update={"nodes": (*assessment.nodes, hidden)})

    with pytest.raises(ValueError, match="assessment projection"):
        render_mermaid(render_fixture.graph, assessment)
    with pytest.raises(ValueError, match="assessment projection"):
        render_markdown(render_fixture.graph, render_fixture.cases, assessment)


def test_assessment_overlay_revalidates_forged_duplicate_scorecards_before_mapping(
    render_fixture: RenderFixture,
) -> None:
    """Catches model-copy duplicates replacing a visible scorecard before validation."""
    marker = "PRIVATE-HIDDEN-DUPLICATE-8197"
    assessment = _assessment(render_fixture)
    original = assessment.nodes[0]
    dimension = original.dimensions[0].model_copy(update={"recommended_next_action": marker})
    duplicate = original.model_copy(update={"dimensions": (dimension,)})
    forged = assessment.model_copy(update={"nodes": (*assessment.nodes, duplicate)})

    with pytest.raises(ValueError, match="assessment projection") as mermaid_error:
        render_mermaid(render_fixture.graph, forged)
    with pytest.raises(ValueError, match="assessment projection") as markdown_error:
        render_markdown(render_fixture.graph, render_fixture.cases, forged)

    assert marker not in str(mermaid_error.value)
    assert marker not in str(markdown_error.value)


def test_graph_renderer_writes_only_assessed_generated_views(
    render_fixture: RenderFixture,
) -> None:
    """Catches the optional report being dropped or written into canonical storage."""
    before = render_fixture.graph_path.read_bytes()
    renderer = GraphRenderer(
        YamlGraphStore(render_fixture.graph_path),
        render_fixture.cases,
        assessment=_assessment(render_fixture),
    )

    markdown_path, mermaid_path = renderer.render_all(render_fixture.output_dir)

    assert "Assessment (non-canonical)" in markdown_path.read_text(encoding="utf-8")
    assert "classDef health_red" in mermaid_path.read_text(encoding="utf-8")
    assert render_fixture.graph_path.read_bytes() == before


def test_legacy_render_bytes_are_unchanged_without_assessment(
    render_fixture: RenderFixture,
) -> None:
    """Catches optional overlay work changing the established default projections."""
    assert render_mermaid(render_fixture.graph, None) == render_mermaid(render_fixture.graph)
    assert render_markdown(render_fixture.graph, render_fixture.cases, None) == render_markdown(
        render_fixture.graph, render_fixture.cases
    )
