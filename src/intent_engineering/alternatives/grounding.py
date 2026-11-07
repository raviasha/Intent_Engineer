"""Build bounded intent-to-requirement grounding from one safe assessment snapshot."""

from __future__ import annotations

from collections import deque

from intent_engineering.alternatives.models import (
    AlternativeEvidenceSummary,
    AlternativeMode,
    AlternativeRequest,
    AlternativeUnavailable,
)
from intent_engineering.assessment import AssessmentReport, AssessmentSnapshot
from intent_engineering.core.models import Node, NodeType

_MAX_TRAVERSAL_DEPTH = 3
_MAX_VISITED_NODES = 128
_CONTEXT_TYPES = frozenset(
    {
        NodeType.PRODUCT_INTENT,
        NodeType.DESIRED_OUTCOME,
        NodeType.CONSTRAINT,
    }
)


class AlternativeSubjectUnavailable(ValueError):
    """One fixed public failure for hidden, unknown, stale, or malformed subjects."""

    def __init__(self) -> None:
        super().__init__("alternative subject unavailable")


def _snapshot_matches_report(snapshot: AssessmentSnapshot, report: AssessmentReport) -> bool:
    return (
        type(snapshot) is AssessmentSnapshot
        and type(report) is AssessmentReport
        and snapshot.project_id == report.project_id
        and snapshot.graph.id == report.graph_id
        and snapshot.graph.version == report.graph_version
        and snapshot.graph_digest == report.graph_digest
        and snapshot.evidence_digest == report.evidence_digest
        and snapshot.ingestion_digest == report.ingestion_digest
        and snapshot.case_digest == report.case_digest
        and snapshot.clarification_digest == report.clarification_digest
        and snapshot.history_digest == report.history_digest
        and snapshot.aggregate_digest == report.snapshot_digest
        and snapshot.principal_projection_digest == report.principal_projection_digest
    )


def _context_nodes(snapshot: AssessmentSnapshot, subject: Node) -> tuple[Node, ...]:
    nodes = {node.id: node for node in snapshot.graph.nodes}
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for edge in snapshot.graph.edges:
        if edge.from_id in adjacency and edge.to_id in adjacency:
            adjacency[edge.from_id].add(edge.to_id)
            adjacency[edge.to_id].add(edge.from_id)

    found: dict[str, Node] = {}
    queue: deque[tuple[str, int]] = deque(((subject.id, 0),))
    visited: set[str] = set()
    while queue and len(visited) < _MAX_VISITED_NODES:
        node_id, depth = queue.popleft()
        if node_id in visited:
            continue
        visited.add(node_id)
        node = nodes[node_id]
        if node.type in _CONTEXT_TYPES and node.status == "active":
            found[node.id] = node
        if depth >= _MAX_TRAVERSAL_DEPTH:
            continue
        for adjacent in sorted(adjacency[node_id]):
            if adjacent not in visited:
                queue.append((adjacent, depth + 1))
    return tuple(found[node_id] for node_id in sorted(found))


def _evidence_summaries(
    snapshot: AssessmentSnapshot, nodes: tuple[Node, ...]
) -> tuple[AlternativeEvidenceSummary, ...]:
    evidence_by_id = {record.id: record for record in snapshot.evidence}
    references = sorted({reference for node in nodes for reference in node.evidence_refs})
    summaries: list[AlternativeEvidenceSummary] = []
    for reference in references[:64]:
        record = evidence_by_id.get(reference)
        if record is None:
            continue
        summaries.append(
            AlternativeEvidenceSummary(
                evidence_ref=record.id,
                summary=(
                    f"Visible {record.connector_type} evidence observed "
                    f"{record.observed_at.date().isoformat()}."
                ),
            )
        )
    return tuple(summaries)


class AlternativeRequestBuilder:
    """Derive one content-addressed, provider-safe request from visible state only."""

    def build(
        self,
        *,
        snapshot: AssessmentSnapshot,
        report: AssessmentReport,
        subject_ref: str,
        requested_modes: tuple[AlternativeMode, ...] = (AlternativeMode.SIMPLIFY,),
    ) -> AlternativeRequest:
        request: AlternativeRequest | None = None
        failed = False
        try:
            request = self._build(
                snapshot=snapshot,
                report=report,
                subject_ref=subject_ref,
                requested_modes=requested_modes,
            )
        except (KeyError, StopIteration, TypeError, ValueError):
            failed = True
        if failed or request is None:
            raise AlternativeSubjectUnavailable()
        return request

    def _build(
        self,
        *,
        snapshot: AssessmentSnapshot,
        report: AssessmentReport,
        subject_ref: str,
        requested_modes: tuple[AlternativeMode, ...],
    ) -> AlternativeRequest:
        if type(subject_ref) is not str or not subject_ref:
            raise ValueError("invalid subject")
        if not _snapshot_matches_report(snapshot, report):
            raise ValueError("stale assessment")
        subject = next(node for node in snapshot.graph.nodes if node.id == subject_ref)
        if subject.type is not NodeType.REQUIREMENT or subject.status != "active":
            raise ValueError("unsupported subject")
        scorecard = report.node(subject_ref)
        context = _context_nodes(snapshot, subject)
        intent_refs = tuple(node.id for node in context if node.type is NodeType.PRODUCT_INTENT)
        outcome_refs = tuple(node.id for node in context if node.type is NodeType.DESIRED_OUTCOME)
        constraint_refs = tuple(node.id for node in context if node.type is NodeType.CONSTRAINT)
        if not intent_refs or not outcome_refs:
            raise ValueError("incomplete intent grounding")
        evidence = _evidence_summaries(snapshot, (subject, *context))
        if not evidence:
            raise ValueError("missing visible evidence")
        return AlternativeRequest.create(
            subject_ref=subject.id,
            subject_text=subject.label,
            requested_modes=requested_modes,
            intent_refs=intent_refs,
            desired_outcome_refs=outcome_refs,
            constraint_refs=constraint_refs,
            evidence_summaries=evidence,
            current_scorecard=scorecard,
            snapshot_digest=snapshot.aggregate_digest,
            policy_digest=report.policy_digest,
            principal_projection_digest=snapshot.principal_projection_digest,
        )

    def try_build(
        self,
        *,
        snapshot: AssessmentSnapshot,
        report: AssessmentReport,
        subject_ref: str,
        requested_modes: tuple[AlternativeMode, ...] = (AlternativeMode.SIMPLIFY,),
    ) -> AlternativeRequest | AlternativeUnavailable:
        try:
            return self.build(
                snapshot=snapshot,
                report=report,
                subject_ref=subject_ref,
                requested_modes=requested_modes,
            )
        except AlternativeSubjectUnavailable:
            return AlternativeUnavailable.SUBJECT_UNAVAILABLE


__all__ = ["AlternativeRequestBuilder", "AlternativeSubjectUnavailable"]
