"""Deterministic Markdown projection of an immutable graph."""

from collections.abc import Sequence
from unicodedata import category

from intent_engineering.assessment.models import AssessmentHealth, AssessmentReport, NodeScorecard
from intent_engineering.core.models import (
    Graph,
    Node,
    NodeType,
    ReconciliationCase,
    is_nonterminal_case_status,
)
from intent_engineering.render.mermaid import _assessment_scorecards

SEMANTIC_GROUPS: tuple[tuple[str, frozenset[NodeType]], ...] = (
    (
        "Intent",
        frozenset(
            {NodeType.CONTEXT, NodeType.NEED, NodeType.PRODUCT_INTENT, NodeType.DESIRED_OUTCOME}
        ),
    ),
    ("Requirements", frozenset({NodeType.REQUIREMENT, NodeType.CAPABILITY})),
    (
        "Decisions",
        frozenset(
            {NodeType.DECISION, NodeType.ARCHITECTURE, NodeType.INTERFACE, NodeType.DATA_CONTRACT}
        ),
    ),
    ("Constraints", frozenset({NodeType.CONSTRAINT, NodeType.POLICY})),
    ("Acceptance criteria", frozenset({NodeType.ACCEPTANCE_CRITERION})),
    (
        "Code references",
        frozenset(
            {
                NodeType.REPOSITORY,
                NodeType.MODULE,
                NodeType.FILE,
                NodeType.SYMBOL,
                NodeType.ENDPOINT,
                NodeType.SCHEMA,
            }
        ),
    ),
    ("Test references", frozenset({NodeType.TEST})),
)
_HEALTH_TEXT: dict[AssessmentHealth, str] = {
    AssessmentHealth.GREEN: "✓ Green",
    AssessmentHealth.ORANGE: "! Orange",
    AssessmentHealth.RED: "× Red",
    AssessmentHealth.UNASSESSED: "? Unassessed",
}


def _escape_markdown(value: str) -> str:
    value = "".join(
        " " if character in "\r\n\u2028\u2029" or category(character).startswith("C") else character
        for character in value
    )
    return "".join(
        f"\\{character}" if character in r'\\`*_{}[]<>#|"' else character for character in value
    )


def _render_node_group(group: tuple[str, frozenset[NodeType]], nodes: Sequence[Node]) -> str:
    title, node_types = group
    selected = sorted((node for node in nodes if node.type in node_types), key=lambda node: node.id)
    if not selected:
        return ""
    lines = [f"## {title}"]
    for node in selected:
        lines.append(f"- `{_escape_markdown(node.id)}` — {_escape_markdown(node.label)}")
        if node.evidence_refs:
            lines.append(
                "  - Evidence: "
                + ", ".join(f"`{_escape_markdown(ref)}`" for ref in sorted(node.evidence_refs))
            )
    return "\n".join(lines)


def _render_cases(cases: Sequence[ReconciliationCase]) -> str:
    open_cases = sorted(
        (case for case in cases if is_nonterminal_case_status(case.status)),
        key=lambda case: case.id,
    )
    if not open_cases:
        return ""
    lines = ["## Open reconciliation cases"]
    for case in open_cases:
        lines.append(
            f"- `{_escape_markdown(case.id)}` — {_escape_markdown(case.case_type.value)}: {_escape_markdown(case.subject_ref)}"
        )
        lines.append(
            "  - Evidence: "
            + ", ".join(f"`{_escape_markdown(ref)}`" for ref in case.all_evidence_refs)
        )
    return "\n".join(lines)


def _score(value: int | None) -> str:
    return "N/A" if value is None else str(value)


def _dimension_label(scorecard: NodeScorecard) -> str:
    return (
        "N/A"
        if scorecard.worst_dimension is None
        else scorecard.worst_dimension.value.replace("_", " ").capitalize()
    )


def _render_assessment(graph: Graph, assessment: AssessmentReport) -> str:
    scorecards = _assessment_scorecards(graph, assessment)
    lines = [
        "## Assessment (non-canonical)",
        "",
        "This detached, explainable overlay does not change canonical graph state.",
        "",
        f"- graph: {_escape_markdown(assessment.graph_id)}",
        f"- version: {assessment.graph_version}",
        f"- snapshot: {assessment.snapshot_digest}",
        f"- principal projection: {assessment.principal_projection_digest}",
        "",
        (
            f"Project: {_HEALTH_TEXT[assessment.project.health]} · "
            f"Robustness {_score(assessment.project.robustness)} · "
            f"Confidence {_score(assessment.project.confidence)}"
        ),
        "",
        "| Node | Health | Robustness | Confidence | Worst dimension |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for node in sorted(graph.nodes, key=lambda item: item.id):
        scorecard = scorecards[node.id]
        lines.append(
            f"| `{_escape_markdown(node.id)}` | {_HEALTH_TEXT[scorecard.health]} | "
            f"{_score(scorecard.robustness)} | {_score(scorecard.confidence)} | "
            f"{_dimension_label(scorecard)} |"
        )

    deductions: list[str] = []
    for node in sorted(graph.nodes, key=lambda item: item.id):
        scorecard = scorecards[node.id]
        for dimension in scorecard.dimensions:
            label = dimension.dimension.value.replace("_", " ").capitalize()
            for check in dimension.failed:
                deductions.append(
                    f"- `{_escape_markdown(node.id)}` — {_HEALTH_TEXT[check.severity]} — "
                    f"{label} — {check.points} points: {_escape_markdown(check.explanation)}"
                )
                references = tuple(sorted({*check.references, *dimension.evidence_refs}))
                deductions.append(
                    "  - Evidence: "
                    + (
                        ", ".join(f"`{_escape_markdown(reference)}`" for reference in references)
                        if references
                        else "none"
                    )
                )
            if dimension.recommended_next_action:
                deductions.append(
                    "  - Recommended next action: "
                    + _escape_markdown(dimension.recommended_next_action)
                )
    lines.extend(("", "### Evidence-linked deductions", ""))
    lines.extend(deductions or ("_No score deductions._",))
    return "\n".join(lines)


def render_markdown(
    graph: Graph,
    cases: Sequence[ReconciliationCase],
    assessment: AssessmentReport | None = None,
) -> str:
    """Render a deterministic Markdown view without mutating graph state."""
    sections = [
        f"# {_escape_markdown(graph.name or graph.id)}",
        _escape_markdown(graph.purpose or ""),
    ]
    if assessment is not None:
        sections.append(_render_assessment(graph, assessment))
    sections.extend(_render_node_group(group, graph.nodes) for group in SEMANTIC_GROUPS)
    sections.append(_render_cases(cases))
    return "\n\n".join(section for section in sections if section) + "\n"
