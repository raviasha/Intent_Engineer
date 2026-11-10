"""Deterministic scheduled assurance over one detached semantic snapshot."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Protocol

from pydantic import ConfigDict

from intent_engineering.core.models import (
    DriftObservation,
    EvidenceIngestion,
    EvidenceRecord,
    EvidenceSide,
    Graph,
    Node,
    NodeType,
    ReconciliationCase,
    ReconciliationCaseType,
    RelationType,
    SourceMode,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy.access import evidence_allowed
from intent_engineering.reconcile.detectors import DetectionInput, detect_drift
from intent_engineering.reconcile.evidence_detection import select_drift_observations

_MAX_REASONED_INPUTS = 256
_INTENT_TYPES = frozenset(
    {
        NodeType.CONTEXT,
        NodeType.NEED,
        NodeType.PRODUCT_INTENT,
        NodeType.DESIRED_OUTCOME,
        NodeType.ASSUMPTION,
        NodeType.PRINCIPLE,
        NodeType.CONSTRAINT,
    }
)
_IMPLEMENTATION_TYPES = frozenset(
    {
        NodeType.MODULE,
        NodeType.FILE,
        NodeType.SYMBOL,
        NodeType.ENDPOINT,
        NodeType.SCHEMA,
        NodeType.BUILD_ARTIFACT,
        NodeType.DEPLOYMENT,
    }
)


class AssuranceSnapshot(StrictModel):
    """One deeply detached input packet supplied to optional semantic reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    graph: Graph
    records: tuple[EvidenceRecord, ...]
    ingestions: tuple[EvidenceIngestion, ...]
    existing_cases: tuple[ReconciliationCase, ...]


class AssuranceReasoner(Protocol):
    """Optional provider-neutral source of grounded candidate comparisons."""

    def detect(self, snapshot: AssuranceSnapshot) -> tuple[DetectionInput, ...]: ...


def _type(node: Node) -> NodeType | None:
    return node.type if isinstance(node.type, NodeType) else None


def _current(record: EvidenceRecord, ingestions: Sequence[EvidenceIngestion]) -> bool:
    associations = tuple(item for item in ingestions if item.evidence.id == record.id)
    if not associations:
        return False
    return all(
        not any(
            item.connector_id == association.connector_id
            and item.evidence.connector_type == record.connector_type
            and item.evidence.external_object_id == record.external_object_id
            and item.sequence > association.sequence
            for item in ingestions
        )
        for association in associations
    )


def _fingerprint(
    detector_id: str,
    subject_ref: str,
    affected_refs: tuple[str, ...],
    sides: tuple[EvidenceSide, ...],
) -> str:
    payload = {
        "detector_id": detector_id,
        "subject_ref": subject_ref,
        "affected_refs": sorted(set(affected_refs)),
        "evidence_refs": sorted({ref for side in sides for ref in side.evidence_refs}),
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _observation(
    *,
    subject: Node,
    case_type: ReconciliationCaseType,
    detector_id: str,
    affected: tuple[Node, ...],
    sides: tuple[EvidenceSide, ...],
) -> DriftObservation:
    affected_refs = tuple(sorted({node.id for node in affected}))
    return DriftObservation(
        subject_ref=subject.id,
        case_type=case_type,
        affected_refs=affected_refs,
        evidence_sides=sides,
        detector_id=detector_id,
        fingerprint=_fingerprint(detector_id, subject.id, affected_refs, sides),
    )


class AssuranceService:
    """Detect eight approved assurance gaps without changing canonical truth."""

    def __init__(
        self,
        *,
        actor: str,
        reasoner: AssuranceReasoner | None = None,
    ) -> None:
        if type(actor) is not str or not actor:
            raise ValueError("invalid assurance actor")
        self._actor = actor
        self._reasoner = reasoner

    def _side(
        self,
        node: Node,
        by_id: dict[str, EvidenceRecord],
        records: tuple[EvidenceRecord, ...],
        ingestions: tuple[EvidenceIngestion, ...],
    ) -> EvidenceSide | None:
        if (
            not node.evidence_refs
            or node.source_mode is None
            or node.intent_fidelity_confidence is None
        ):
            return None
        try:
            resolved = tuple(by_id[ref] for ref in node.evidence_refs)
        except KeyError:
            return None
        chains = {
            (record.connector_type, record.external_object_id) for record in resolved
        }
        for connector_type, external_id in chains:
            chain = tuple(
                record
                for record in records
                if record.connector_type == connector_type
                and record.external_object_id == external_id
            )
            if not chain or any(not evidence_allowed(record, self._actor) for record in chain):
                return None
        if any(not evidence_allowed(record, self._actor) for record in resolved):
            return None
        authors = tuple(sorted({record.author for record in resolved if record.author}))
        if not authors:
            return None
        return EvidenceSide(
            label=node.type.value if isinstance(node.type, NodeType) else str(node.type),
            claim=node.label,
            evidence_refs=tuple(sorted(node.evidence_refs)),
            observed_at=max(record.observed_at for record in resolved),
            authors=authors,
            confidence=node.intent_fidelity_confidence,
            source_mode=node.source_mode,
            current=all(_current(record, ingestions) for record in resolved),
        )

    def detect(
        self,
        *,
        graph: Graph,
        records: Sequence[EvidenceRecord],
        ingestions: Sequence[EvidenceIngestion],
        existing_cases: Sequence[ReconciliationCase],
    ) -> tuple[DriftObservation, ...]:
        """Return stable detached observations from one complete visible snapshot."""
        detached_graph = Graph.model_validate_json(graph.model_dump_json())
        detached_records = tuple(
            EvidenceRecord.model_validate_json(record.model_dump_json()) for record in records
        )
        detached_ingestions = tuple(
            EvidenceIngestion.model_validate_json(item.model_dump_json()) for item in ingestions
        )
        detached_cases = tuple(
            ReconciliationCase.model_validate_json(case.model_dump_json()) for case in existing_cases
        )
        if (
            len({record.id for record in detached_records}) != len(detached_records)
            or len({(item.connector_id, item.sequence) for item in detached_ingestions})
            != len(detached_ingestions)
        ):
            return ()
        by_id = {record.id: record for record in detached_records}
        all_nodes = {
            node.id: node
            for node in detached_graph.nodes
            if node.status == "active" or node.status == "provisional"
        }
        sides = {
            node_id: self._side(
                node,
                by_id,
                detached_records,
                detached_ingestions,
            )
            for node_id, node in all_nodes.items()
        }
        typed_sides = {node_id: side for node_id, side in sides.items() if side is not None}
        nodes = {node_id: all_nodes[node_id] for node_id in typed_sides}
        all_active_edges = tuple(
            edge
            for edge in detached_graph.edges
            if edge.status == "active"
            and edge.from_id in all_nodes
            and edge.to_id in all_nodes
        )
        obscured = {
            visible_id
            for edge in all_active_edges
            for visible_id, other_id in (
                (edge.from_id, edge.to_id),
                (edge.to_id, edge.from_id),
            )
            if visible_id in nodes and other_id not in nodes
        }
        active_edges = tuple(
            edge
            for edge in all_active_edges
            if edge.from_id in nodes and edge.to_id in nodes
        )
        linked: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        for edge in active_edges:
            linked[edge.from_id].add(edge.to_id)
            linked[edge.to_id].add(edge.from_id)
        candidates: list[DriftObservation] = []

        for node in sorted(nodes.values(), key=lambda item: item.id):
            if node.id in obscured:
                continue
            node_type = _type(node)
            if (
                node_type in _INTENT_TYPES
                and typed_sides[node.id].current
                and not any(
                _type(nodes[ref]) is NodeType.REQUIREMENT for ref in linked[node.id]
                )
            ):
                candidates.append(
                    _observation(
                        subject=node,
                        case_type=ReconciliationCaseType.INTENT_LAG,
                        detector_id="intent_without_requirement",
                        affected=(node,),
                        sides=(typed_sides[node.id],),
                    )
                )
            if (
                node_type is NodeType.REQUIREMENT
                and typed_sides[node.id].current
                and not any(
                _type(nodes[ref]) in _INTENT_TYPES for ref in linked[node.id]
                )
            ):
                candidates.append(
                    _observation(
                        subject=node,
                        case_type=ReconciliationCaseType.ORPHAN_REQUIREMENT,
                        detector_id="requirement_without_intent",
                        affected=(node,),
                        sides=(typed_sides[node.id],),
                    )
                )
            if (
                node_type in _IMPLEMENTATION_TYPES
                and typed_sides[node.id].current
                and not any(
                _type(nodes[ref]) in (_INTENT_TYPES | {NodeType.REQUIREMENT, NodeType.DECISION})
                for ref in linked[node.id]
                )
            ):
                candidates.append(
                    _observation(
                        subject=node,
                        case_type=ReconciliationCaseType.UNDOCUMENTED_CODE,
                        detector_id="undocumented_code",
                        affected=(node,),
                        sides=(typed_sides[node.id],),
                    )
                )

        for edge in sorted(active_edges, key=lambda item: item.id):
            left, right = nodes[edge.from_id], nodes[edge.to_id]
            if left.id in obscured or right.id in obscured:
                continue
            if (
                edge.relation is RelationType.IMPLEMENTED_BY
                and _type(left) is NodeType.REQUIREMENT
                and _type(right) in _IMPLEMENTATION_TYPES
                and left.last_modified_at > right.last_modified_at
                and typed_sides[left.id].current
                and typed_sides[right.id].current
            ):
                candidates.append(
                    _observation(
                        subject=left,
                        case_type=ReconciliationCaseType.CODE_LAG,
                        detector_id="code_lag",
                        affected=(left, right),
                        sides=(typed_sides[left.id], typed_sides[right.id]),
                    )
                )
            if (
                edge.relation is RelationType.CONTRADICTS
                and left.source_mode is SourceMode.EXPLICIT
                and right.source_mode is SourceMode.EXPLICIT
                and typed_sides[left.id].current
                and typed_sides[right.id].current
                and set(typed_sides[left.id].authors) != set(typed_sides[right.id].authors)
            ):
                candidates.append(
                    _observation(
                        subject=left,
                        case_type=ReconciliationCaseType.CONFLICTING_SOURCES,
                        detector_id="conflicting_sources",
                        affected=(left, right),
                        sides=(typed_sides[left.id], typed_sides[right.id]),
                    )
                )

        for requirement in sorted(
            (
                node
                for node in nodes.values()
                if _type(node) is NodeType.REQUIREMENT and node.id not in obscured
            ),
            key=lambda item: item.id,
        ):
            implementations = tuple(
                nodes[edge.to_id]
                for edge in active_edges
                if edge.from_id == requirement.id
                and edge.relation is RelationType.IMPLEMENTED_BY
                and _type(nodes[edge.to_id]) in _IMPLEMENTATION_TYPES
            )
            tests = tuple(
                nodes[edge.to_id]
                for edge in active_edges
                if edge.from_id == requirement.id
                and edge.relation is RelationType.VERIFIED_BY
                and _type(nodes[edge.to_id]) is NodeType.TEST
            )
            if (
                implementations
                and tests
                and all(
                    typed_sides[node.id].current
                    for node in (requirement, *implementations, *tests)
                )
                and max(node.last_modified_at for node in implementations)
                > max(node.last_modified_at for node in tests)
            ):
                affected = (requirement, *implementations, *tests)
                candidates.append(
                    _observation(
                        subject=requirement,
                        case_type=ReconciliationCaseType.TEST_LAG,
                        detector_id="test_lag",
                        affected=affected,
                        sides=tuple(typed_sides[node.id] for node in affected),
                    )
                )

        for node in sorted(nodes.values(), key=lambda item: item.id):
            if node.id in obscured:
                continue
            if (
                _type(node) in _INTENT_TYPES
                and (node.status == "provisional" or node.source_mode is SourceMode.INFERRED)
                and typed_sides[node.id].current
                and self._reaches_implementation(node.id, linked, nodes)
            ):
                candidates.append(
                    _observation(
                        subject=node,
                        case_type=ReconciliationCaseType.POSSIBLE_INTENT_CHANGE,
                        detector_id="relevant_provisional_intent",
                        affected=(node,),
                        sides=(typed_sides[node.id],),
                    )
                )
            if not typed_sides[node.id].current:
                candidates.append(
                    _observation(
                        subject=node,
                        case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
                        detector_id="stale_source_evidence",
                        affected=(node,),
                        sides=(typed_sides[node.id],),
                    )
                )

        if self._reasoner is not None:
            visible_record_by_id = {
                record.id: record
                for record in detached_records
                if evidence_allowed(record, self._actor)
            }
            visible_node_ids = frozenset(nodes)
            visible_edges = tuple(
                sorted(
                    (
                        edge
                        for edge in detached_graph.edges
                        if edge.from_id in visible_node_ids
                        and edge.to_id in visible_node_ids
                    ),
                    key=lambda item: item.id,
                )
            )
            visible_cases = tuple(
                sorted(
                    (
                        case
                        for case in detached_cases
                        if case.subject_ref in visible_node_ids
                        and set(case.affected_refs).issubset(visible_node_ids)
                        and case.evidence_sides
                        and all(
                            side.evidence_refs
                            and set(side.evidence_refs).issubset(visible_record_by_id)
                            for side in case.evidence_sides
                        )
                    ),
                    key=lambda item: item.id,
                )
            )
            snapshot = AssuranceSnapshot(
                graph=detached_graph.model_copy(
                    update={
                        "nodes": tuple(sorted(nodes.values(), key=lambda item: item.id)),
                        "edges": visible_edges,
                    }
                ),
                records=tuple(
                    visible_record_by_id[key] for key in sorted(visible_record_by_id)
                ),
                ingestions=tuple(
                    sorted(
                        (
                            ingestion
                            for ingestion in detached_ingestions
                            if ingestion.evidence.id in visible_record_by_id
                            and ingestion.evidence
                            == visible_record_by_id[ingestion.evidence.id]
                        ),
                        key=lambda item: (item.connector_id, item.sequence),
                    )
                ),
                existing_cases=visible_cases,
            )
            reasoned = self._reasoner.detect(snapshot)
            if type(reasoned) is not tuple or len(reasoned) > _MAX_REASONED_INPUTS or any(
                type(item) is not DetectionInput for item in reasoned
            ):
                raise ValueError("invalid assurance reasoner output")
            grounded_nodes = {
                node_id: node for node_id, node in nodes.items() if node_id not in obscured
            }
            grounded_sides = {
                node_id: side
                for node_id, side in typed_sides.items()
                if node_id in grounded_nodes
            }
            grounded_linked = {
                node_id: {
                    target for target in linked[node_id] if target in grounded_nodes
                }
                for node_id in grounded_nodes
            }
            for item in reasoned:
                if not self._grounded_reasoning(
                    item,
                    grounded_nodes,
                    grounded_sides,
                    grounded_linked,
                ):
                    raise ValueError("ungrounded assurance reasoner output")
                candidates.extend(detect_drift(item))

        existing_fingerprints = {case.fingerprint for case in detached_cases}
        existing_subjects = {case.subject_ref for case in detached_cases}
        selected = select_drift_observations(candidates)
        return tuple(
            DriftObservation.model_validate_json(observation.model_dump_json())
            for observation in selected
            if observation.fingerprint not in existing_fingerprints
            and observation.subject_ref not in existing_subjects
        )

    @staticmethod
    def _reaches_implementation(
        start: str,
        linked: dict[str, set[str]],
        nodes: dict[str, Node],
    ) -> bool:
        frontier = {start}
        seen = {start}
        for _depth in range(2):
            next_frontier = {target for source in frontier for target in linked[source]} - seen
            if any(_type(nodes[target]) in _IMPLEMENTATION_TYPES for target in next_frontier):
                return True
            seen.update(next_frontier)
            frontier = next_frontier
        return False

    @staticmethod
    def _grounded_reasoning(
        item: DetectionInput,
        nodes: dict[str, Node],
        sides: dict[str, EvidenceSide],
        linked: dict[str, set[str]],
    ) -> bool:
        if item.subject_ref not in nodes or any(ref not in nodes for ref in item.affected_refs):
            return False
        referenced = {item.subject_ref, *item.affected_refs}
        role_types = (
            (item.requirement, {NodeType.REQUIREMENT}),
            (item.implementation, _IMPLEMENTATION_TYPES),
            (item.test, {NodeType.TEST}),
            (item.decision, {NodeType.DECISION}),
        )
        matched: list[tuple[EvidenceSide, set[str]]] = []
        for side, expected_types in role_types:
            if side is None:
                continue
            node_ids = {
                node_id
                for node_id, grounded_side in sides.items()
                if grounded_side == side
                and _type(nodes[node_id]) in expected_types
                and node_id in referenced
            }
            if not node_ids:
                return False
            matched.append((side, node_ids))
        if item.implementation is not None:
            implementation_ids = next(
                node_ids for side, node_ids in matched if side == item.implementation
            )
            has_mapped_semantics = any(
                _type(nodes[target])
                in (_INTENT_TYPES | {NodeType.REQUIREMENT, NodeType.DECISION})
                for node_id in implementation_ids
                for target in linked[node_id]
            )
            if item.has_mapped_semantics is not has_mapped_semantics:
                return False
        grounded_sides = tuple(sides.values())
        for side in (item.requirement, item.implementation, item.test, item.decision):
            if side is not None and (
                side not in grounded_sides
                or not side.current
            ):
                return False
        return True


__all__ = ["AssuranceReasoner", "AssuranceService", "AssuranceSnapshot"]
