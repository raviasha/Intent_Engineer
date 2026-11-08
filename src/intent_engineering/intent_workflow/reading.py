"""Bounded, non-canonical reading projections of an authorized graph snapshot."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from intent_engineering.assessment.models import AssessmentReport, AssessmentSnapshot
from intent_engineering.core.models import NodeType, RelationType, SourceMode
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.coverage import build_coverage

_TOPIC_TYPES = frozenset(
    {
        NodeType.CONTEXT,
        NodeType.NEED,
        NodeType.PRODUCT_INTENT,
        NodeType.DESIRED_OUTCOME,
        NodeType.CONSTRAINT,
        NodeType.PRINCIPLE,
        NodeType.ASSUMPTION,
        NodeType.REQUIREMENT,
        NodeType.ACCEPTANCE_CRITERION,
        NodeType.CAPABILITY,
        NodeType.DECISION,
    }
)
_TOPIC_RELATIONS = frozenset(
    {
        RelationType.CREATES_NEED,
        RelationType.MOTIVATES,
        RelationType.SEEKS_OUTCOME,
        RelationType.CONSTRAINS,
        RelationType.ASSUMES,
        RelationType.JUSTIFIES,
        RelationType.REFINES,
        RelationType.REALIZED_BY,
        RelationType.SPECIFIED_BY,
        RelationType.HAS_ACCEPTANCE_CRITERION,
        RelationType.DECIDED_BY,
    }
)


class ContextAssertion(StrictModel):
    """A cited existing assertion, never an automatically confirmed interpretation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = Field(max_length=512)
    type: str = Field(max_length=256)
    label: str = Field(max_length=1000)
    interpretation: Literal["explicit", "inferred", "derived", "unspecified", "assumption"]
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=8)


def topic_context(snapshot: AssessmentSnapshot, node_id: str) -> tuple[ContextAssertion, ...]:
    """Read at most two semantic hops; cycles, inactive and implementation edges cannot expand it."""
    nodes = {node.id: node for node in snapshot.graph.nodes}
    if node_id not in nodes:
        raise LookupError("reading page unavailable")
    adjacency: dict[str, set[str]] = {}
    for edge in snapshot.graph.edges:
        if (
            edge.status != "active"
            or edge.external
            or edge.relation not in _TOPIC_RELATIONS
            or edge.from_id not in nodes
            or edge.to_id not in nodes
        ):
            continue
        if any(
            nodes[ref].type not in _TOPIC_TYPES or nodes[ref].status != "active"
            for ref in (edge.from_id, edge.to_id)
        ):
            continue
        adjacency.setdefault(edge.from_id, set()).add(edge.to_id)
        adjacency.setdefault(edge.to_id, set()).add(edge.from_id)
    selected = {node_id}
    frontier = {node_id}
    for _ in range(2):
        candidates = sorted(
            {ref for current in frontier for ref in adjacency.get(current, ())} - selected
        )
        frontier = set(candidates[: 24 - len(selected)])
        selected.update(frontier)
    visible_evidence = {record.id for record in snapshot.evidence}
    assertions: list[ContextAssertion] = []
    for ref in [node_id, *sorted(selected - {node_id})]:
        node = nodes[ref]
        interpretation: Literal["explicit", "inferred", "derived", "unspecified", "assumption"]
        if node.type == NodeType.ASSUMPTION:
            interpretation = "assumption"
        elif node.source_mode == SourceMode.EXPLICIT:
            interpretation = "explicit"
        elif node.source_mode == SourceMode.INFERRED:
            interpretation = "inferred"
        elif node.source_mode == SourceMode.DERIVED:
            interpretation = "derived"
        else:
            interpretation = "unspecified"
        assertions.append(
            ContextAssertion(
                id=ref,
                type=str(node.type),
                label=node.label[:1000],
                interpretation=interpretation,
                evidence_refs=tuple(sorted(set(node.evidence_refs) & visible_evidence))[:8],
            )
        )
    return tuple(assertions)


def build_reading_page(
    snapshot: AssessmentSnapshot,
    node_id: str,
    report: AssessmentReport | None = None,
) -> dict[str, object]:
    """Return a readable topic from exactly the caller's authorized snapshot, without writes."""
    context = topic_context(snapshot, node_id)
    selected = {item.id for item in context}
    references = {ref for item in context for ref in item.evidence_refs}
    if report is not None and report.snapshot_digest != snapshot.aggregate_digest:
        raise ValueError("reading assessment changed")
    return {
        "schema_version": 1,
        "node_id": node_id,
        "title": context[0].label,
        "snapshot_digest": snapshot.aggregate_digest,
        "graph_version": snapshot.graph.version,
        "non_canonical": True,
        "coverage": build_coverage(snapshot.graph, snapshot.cases, focus_ids=frozenset(selected)),
        "scope_note": "Up to 24 assertions within two semantic links; this is a topic view, not the whole graph.",
        "assertions": [item.model_dump(mode="json") for item in context],
        "relationships": [
            {"id": edge.id, "from": edge.from_id, "relation": str(edge.relation), "to": edge.to_id}
            for edge in sorted(snapshot.graph.edges, key=lambda edge: edge.id)
            if edge.status == "active"
            and not edge.external
            and edge.from_id in selected
            and edge.to_id in selected
            and edge.relation in _TOPIC_RELATIONS
        ][:128],
        "evidence": [
            {
                "id": record.id,
                "source": record.source_locator[:1000],
                "version": record.external_version,
                "author": record.author,
            }
            for record in snapshot.evidence
            if record.id in references
        ][:64],
        "open_questions": []
        if report is None
        else [
            {
                "node_id": node.node_id,
                "reason": check.explanation,
                "dimension": result.dimension.value,
                "health": check.severity.value,
            }
            for node in report.nodes
            if node.node_id in selected
            for result in node.dimensions
            for check in result.failed
        ][:64],
        "score_note": "Scores measure structural completeness. Links do not prove that requirements satisfy intent.",
    }
