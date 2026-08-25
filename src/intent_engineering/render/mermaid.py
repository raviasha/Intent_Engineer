"""Deterministic Mermaid projection of an immutable graph."""

from hashlib import sha256

from intent_engineering.core.models import Edge, Graph, Node


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


def _render_edge(edge: Edge) -> str:
    return f"{_node_id(edge.from_id)} -->|{edge.relation.value}| {_node_id(edge.to_id)}"


def render_mermaid(graph: Graph) -> str:
    """Render a stable, safely escaped Mermaid flowchart."""
    lines = ["flowchart LR"]
    lines.extend(_render_node(node) for node in sorted(graph.nodes, key=lambda item: item.id))
    lines.extend(_render_edge(edge) for edge in sorted(graph.edges, key=lambda item: item.id))
    return "\n".join(lines) + "\n"
