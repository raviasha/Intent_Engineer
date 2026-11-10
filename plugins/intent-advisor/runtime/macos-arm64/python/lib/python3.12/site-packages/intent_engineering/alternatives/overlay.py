"""Apply validated alternative effects to a detached graph projection only."""

from __future__ import annotations

from datetime import UTC, datetime

from intent_engineering.alternatives.activation import build_alternative_changeset
from intent_engineering.alternatives.models import RequirementAlternative
from intent_engineering.core.graph import apply_changeset
from intent_engineering.core.models import Graph


class AlternativeProjectionError(ValueError):
    """Fixed public failure for a rejected detached projection."""

    def __init__(self) -> None:
        super().__init__("alternative projection unavailable")


def _apply(graph: Graph, alternative: RequirementAlternative) -> Graph:
    if type(graph) is not Graph or type(alternative) is not RequirementAlternative:
        raise TypeError("invalid projection input")
    timestamp = max(
        (node.last_modified_at for node in graph.nodes),
        default=datetime(1970, 1, 1, tzinfo=UTC),
    )
    changeset = build_alternative_changeset(
        graph,
        alternative,
        actor="alternative-projection",
        timestamp=timestamp,
    )
    projected = apply_changeset(graph, changeset).model_copy(update={"version": graph.version})
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
