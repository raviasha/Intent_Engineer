"""Explain structural outcome/constraint coverage without claiming semantic satisfaction."""

from __future__ import annotations

from collections import deque

from intent_engineering.core.models import (
    Graph,
    NodeType,
    ReconciliationCase,
    ReconciliationStatus,
    RelationType,
)
from intent_engineering.core.models.reconciliation import is_intent_requirement_case

_FORWARD = frozenset(
    {
        RelationType.SPECIFIED_BY,
        RelationType.REALIZED_BY,
        RelationType.REFINES,
        RelationType.HAS_ACCEPTANCE_CRITERION,
        RelationType.CONSTRAINS,
    }
)
_TRAVERSABLE = frozenset(
    {
        NodeType.PRODUCT_INTENT,
        NodeType.DESIRED_OUTCOME,
        NodeType.CONSTRAINT,
        NodeType.CAPABILITY,
        NodeType.REQUIREMENT,
        NodeType.ACCEPTANCE_CRITERION,
    }
)
_OPEN = frozenset(
    {ReconciliationStatus.OPEN, ReconciliationStatus.NEEDS_HUMAN, ReconciliationStatus.PROPOSED}
)


def build_coverage(
    graph: Graph,
    cases: tuple[ReconciliationCase, ...],
    *,
    focus_ids: frozenset[str] | None = None,
) -> dict[str, object]:
    """Build bounded deterministic rows from a permission-filtered graph and visible cases."""
    nodes = {node.id: node for node in graph.nodes if node.status == "active"}
    cases = tuple(case for case in cases if is_intent_requirement_case(case.case_type))
    edges = {edge.id: edge for edge in graph.edges}
    adjacency: dict[str, list[tuple[str, str]]] = {}
    contradictions: list[tuple[str, str]] = []
    for edge in sorted(graph.edges, key=lambda item: item.id):
        if (
            edge.status != "active"
            or edge.external
            or not {edge.from_id, edge.to_id} <= nodes.keys()
        ):
            continue
        if edge.relation == RelationType.CONTRADICTS:
            contradictions.append((edge.from_id, edge.to_id))
        if edge.relation in _FORWARD:
            adjacency.setdefault(edge.from_id, []).append((edge.to_id, edge.id))
        elif (
            edge.relation == RelationType.SEEKS_OUTCOME
            and nodes[edge.to_id].type == NodeType.DESIRED_OUTCOME
        ):
            # A requirement or parent intent explicitly seeks this outcome.
            adjacency.setdefault(edge.to_id, []).append((edge.from_id, edge.id))
    rows: list[dict[str, object]] = []
    truncated = False
    for subject in sorted(nodes.values(), key=lambda item: item.id):
        if subject.type not in {NodeType.DESIRED_OUTCOME, NodeType.CONSTRAINT}:
            continue
        seen: set[str] = set()
        pending: deque[tuple[str, tuple[str, ...]]] = deque([(subject.id, ())])
        requirements: dict[str, tuple[str, ...]] = {}
        limited = False
        while pending:
            current, path = pending.popleft()
            if current in seen:
                continue
            if len(seen) >= 512:
                limited = True
                break
            seen.add(current)
            if nodes[current].type == NodeType.REQUIREMENT:
                requirements[current] = path
                continue
            if len(path) >= 8:
                limited = limited or bool(adjacency.get(current))
                continue
            for target, relation_id in adjacency.get(current, ()):
                if target not in seen and nodes[target].type in _TRAVERSABLE:
                    pending.append((target, (*path, relation_id)))
        if focus_ids is not None and not ({subject.id, *requirements} & focus_ids):
            continue
        if len(rows) == 128:
            truncated = True
            break
        supporting_edges = {ref for path in requirements.values() for ref in path}
        affected = {subject.id, *requirements, *supporting_edges}
        for ref in supporting_edges:
            affected.update((edges[ref].from_id, edges[ref].to_id))
        related_cases = sorted(
            (case for case in cases if affected & {case.subject_ref, *case.affected_refs}),
            key=lambda item: item.id,
        )
        disputed = any(case.status in _OPEN for case in related_cases) or any(
            source in affected or target in affected for source, target in contradictions
        )
        deferred = any(
            case.status == ReconciliationStatus.DEFERRED
            and subject.id in {case.subject_ref, *case.affected_refs}
            for case in related_cases
        )
        if disputed:
            status, health = "disputed", "red"
            reason = "Unresolved disagreement affects this outcome or its requirements."
        elif deferred:
            status, health = "deferred", "orange"
            reason = "A recorded reconciliation decision defers this topic; it is not satisfied."
        elif requirements:
            status, health = "linked_unreviewed", "orange"
            reason = "Explicit graph paths connect requirements; review whether their meaning meets the intent."
        elif limited:
            status, health = "incomplete", "orange"
            reason = "The bounded traversal is incomplete; missing coverage cannot be concluded."
        else:
            status, health = "missing", "red"
            reason = "No active requirement path is recorded for this outcome or constraint."
        rows.append(
            {
                "subject_id": subject.id,
                "label": subject.label[:1000],
                "type": str(subject.type),
                "requirement_ids": sorted(requirements)[:64],
                "relationship_ids": sorted({ref for path in requirements.values() for ref in path})[
                    :128
                ],
                "case_ids": [
                    case.id
                    for case in related_cases
                    if case.status in _OPEN or case.status == ReconciliationStatus.DEFERRED
                ][:32],
                "status": status,
                "health": health,
                "reason": reason,
                "truncated": limited or len(requirements) > 64,
            }
        )
    return {
        "rows": rows,
        "truncated": truncated,
        "semantic_alignment": "requires_human_review",
        "note": "Coverage is structural evidence, not proof that requirements satisfy intent.",
    }
