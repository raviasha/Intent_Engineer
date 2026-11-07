"""Apply validated alternative effects to a detached graph projection only."""

from __future__ import annotations

from intent_engineering.alternatives.models import (
    GraphEffectKind,
    RequirementAlternative,
)
from intent_engineering.core.models import Graph, Node


class AlternativeProjectionError(ValueError):
    """Fixed public failure for a rejected detached projection."""

    def __init__(self) -> None:
        super().__init__("alternative projection unavailable")


def _apply(graph: Graph, alternative: RequirementAlternative) -> Graph:
    if type(graph) is not Graph or type(alternative) is not RequirementAlternative:
        raise TypeError("invalid projection input")
    if alternative.graph_effect_digest != alternative.candidate.graph_effect_digest:
        raise ValueError("effect digest mismatch")

    nodes: dict[str, Node] = {node.id: node for node in graph.nodes}
    removed: set[str] = set()
    for effect in alternative.candidate.graph_effects:
        if effect.effect is GraphEffectKind.UPDATE_NODE:
            node = nodes.get(effect.target_ref)
            if (
                node is None
                or effect.field not in {"label", "statement"}
                or effect.before != node.label
                or effect.after is None
            ):
                raise ValueError("invalid node update")
            nodes[node.id] = node.model_copy(update={"label": effect.after})
        elif effect.effect is GraphEffectKind.REMOVE_NODE:
            if effect.target_ref not in nodes:
                raise ValueError("invalid node removal")
            removed.add(effect.target_ref)
        else:
            raise ValueError("unsupported graph effect")

    projected = graph.model_copy(
        update={
            "nodes": tuple(nodes[node_id] for node_id in sorted(nodes) if node_id not in removed),
            "edges": tuple(
                edge
                for edge in graph.edges
                if edge.from_id not in removed and edge.to_id not in removed
            ),
        }
    )
    projected.assert_invariants()
    return projected


def apply_alternative_overlay(
    graph: Graph,
    alternative: RequirementAlternative,
) -> Graph:
    """Return a fresh graph with validated effects or one fixed failure."""
    projected: Graph | None = None
    failed = False
    try:
        projected = _apply(graph, alternative)
    except (KeyError, TypeError, ValueError):
        failed = True
    if failed or projected is None:
        raise AlternativeProjectionError()
    return projected


__all__ = ["AlternativeProjectionError", "apply_alternative_overlay"]
