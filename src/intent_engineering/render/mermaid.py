"""Deterministic Mermaid projection of an immutable graph."""

import json
from hashlib import sha256

from intent_engineering.assessment.models import (
    AssessmentHealth,
    AssessmentReport,
    NodeScorecard,
)
from intent_engineering.core.models import Edge, Graph, Node

_HEALTH_PRESENTATION: dict[AssessmentHealth, tuple[str, str, str]] = {
    AssessmentHealth.GREEN: ("health_green", "✓", "Green"),
    AssessmentHealth.ORANGE: ("health_orange", "!", "Orange"),
    AssessmentHealth.RED: ("health_red", "×", "Red"),
    AssessmentHealth.UNASSESSED: ("health_unassessed", "?", "Unassessed"),
}
_HEALTH_CLASSES = (
    "classDef health_green fill:#ecfdf3,stroke:#067647,color:#067647,stroke-width:2px",
    "classDef health_orange fill:#fffaeb,stroke:#93370d,color:#93370d,stroke-width:2px,stroke-dasharray:5 3",
    "classDef health_red fill:#fef3f2,stroke:#b42318,color:#b42318,stroke-width:4px",
    "classDef health_unassessed fill:#f2f4f7,stroke:#344054,color:#344054,stroke-width:2px,stroke-dasharray:2 3",
)


def _node_id(stable_id: str) -> str:
    return f"n_{sha256(stable_id.encode('utf-8')).hexdigest()[:16]}"


def _escape_mermaid(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )


def _render_node(node: Node) -> str:
    return f'{_node_id(node.id)}["{_escape_mermaid(node.label)}"]'


def _graph_digest(graph: Graph) -> str:
    material = json.dumps(
        graph.model_dump(mode="json", by_alias=True),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(material).hexdigest()}"


def _node_type(value: object) -> str:
    candidate = getattr(value, "value", value)
    return candidate if isinstance(candidate, str) else ""


def _assessment_scorecards(
    graph: Graph,
    assessment: AssessmentReport,
) -> dict[str, NodeScorecard]:
    try:
        if type(assessment) is not AssessmentReport:
            raise ValueError("invalid assessment report")
        validated = AssessmentReport.model_validate(
            assessment.model_dump(mode="python"), strict=True
        )
        if validated != assessment:
            raise ValueError("invalid assessment report")
    except (TypeError, ValueError):
        raise ValueError("assessment projection does not match rendered graph") from None

    graph_nodes = {node.id: node for node in graph.nodes}
    scorecards = {scorecard.node_id: scorecard for scorecard in validated.nodes}
    if (
        validated.graph_id != graph.id
        or validated.graph_version != graph.version
        or validated.graph_digest != _graph_digest(graph)
        or set(scorecards) != set(graph_nodes)
        or any(
            _node_type(scorecards[node_id].node_type) != _node_type(node.type)
            for node_id, node in graph_nodes.items()
        )
    ):
        raise ValueError("assessment projection does not match rendered graph")
    return scorecards


def _score(value: int | None) -> str:
    return "N/A" if value is None else str(value)


def _dimension_label(scorecard: NodeScorecard) -> tuple[str, str]:
    if scorecard.worst_dimension is None:
        return "N/A", "N/A"
    dimension = scorecard.dimension(scorecard.worst_dimension)
    label = scorecard.worst_dimension.value.replace("_", " ").capitalize()
    return label, _score(dimension.score)


def _render_assessed_node(node: Node, scorecard: NodeScorecard) -> str:
    class_name, icon, health = _HEALTH_PRESENTATION[scorecard.health]
    dimension, dimension_score = _dimension_label(scorecard)
    label = (
        f"{_escape_mermaid(node.label)}<br/>{icon} {health} · "
        f"Score: {_score(scorecard.robustness)} · Confidence: {_score(scorecard.confidence)}"
        f"<br/>◆ Worst dimension: {_escape_mermaid(dimension)}: {dimension_score}"
    )
    return f'{_node_id(node.id)}["{label}"]:::{class_name}'


def _render_edge(edge: Edge) -> str:
    return f"{_node_id(edge.from_id)} -->|{edge.relation.value}| {_node_id(edge.to_id)}"


def render_mermaid(graph: Graph, assessment: AssessmentReport | None = None) -> str:
    """Render a stable, safely escaped Mermaid flowchart."""
    if assessment is None:
        lines = ["flowchart LR"]
        lines.extend(_render_node(node) for node in sorted(graph.nodes, key=lambda item: item.id))
        lines.extend(_render_edge(edge) for edge in sorted(graph.edges, key=lambda item: item.id))
        return "\n".join(lines) + "\n"

    scorecards = _assessment_scorecards(graph, assessment)
    lines = ["flowchart LR"]
    lines.append(
        "%% Non-canonical assessment overlay; "
        f"graph: {_escape_mermaid(assessment.graph_id)}; version: {assessment.graph_version}; "
        f"snapshot: {assessment.snapshot_digest}; "
        f"principal projection: {assessment.principal_projection_digest}"
    )
    lines.extend(_HEALTH_CLASSES)
    lines.extend(
        _render_assessed_node(node, scorecards[node.id])
        for node in sorted(graph.nodes, key=lambda item: item.id)
    )
    lines.extend(_render_edge(edge) for edge in sorted(graph.edges, key=lambda item: item.id))
    return "\n".join(lines) + "\n"
