"""A fixture-oriented semantic reasoner with no model-provider dependency."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

from intent_engineering.core.models import (
    CandidateAssertion,
    ChangeSet,
    EvidenceDelta,
    Graph,
    JsonValue,
    Node,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DeterministicReasoner:
    """Map explicit fixture assertions to new graph nodes deterministically."""

    def __init__(
        self,
        *,
        actor: str = "deterministic-reasoner",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._actor = actor
        self._clock = clock

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        """Read typed assertions only from the stable fixture payload field."""
        return tuple(
            CandidateAssertion.model_validate(cast(dict[str, JsonValue], item.payload["intent_assertion"]))
            for item in delta.added
            if "intent_assertion" in item.payload
        )

    def map_to_graph(self, assertions: Sequence[CandidateAssertion], graph: Graph) -> ChangeSet:
        """Create only absent subject nodes, leaving already mapped semantics unchanged."""
        existing_ids = {node.id for node in graph.nodes}
        nodes_added = tuple(
            self._node_from_assertion(assertion)
            for assertion in sorted(assertions, key=lambda item: item.id)
            if assertion.subject_id not in existing_ids
        )
        evidence_refs = tuple(
            sorted({reference for assertion in assertions for reference in assertion.evidence_refs})
        )
        return ChangeSet(
            id=self._changeset_id(graph, assertions),
            actor=self._actor,
            timestamp=self._clock(),
            baseline_graph_version=graph.version,
            evidence_refs=evidence_refs if nodes_added else (),
            nodes_added=nodes_added,
            nodes_updated=(),
            nodes_superseded=(),
            edges_added=(),
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(),
            validation_status="validated",
        )

    def _node_from_assertion(self, assertion: CandidateAssertion) -> Node:
        """Translate the explicit fixture assertion fields into one graph node."""
        timestamp = self._clock()
        return Node(
            id=assertion.subject_id,
            type=assertion.node_type,
            label=assertion.label,
            status="active",
            created_by=self._actor,
            created_at=timestamp,
            last_modified_by=self._actor,
            last_modified_at=timestamp,
            source_mode=assertion.source_mode,
            intent_fidelity_confidence=assertion.confidence,
            confidence_basis="deterministic fixture assertion",
            last_reassessed_at=timestamp,
            evidence_refs=assertion.evidence_refs,
        )

    @staticmethod
    def _changeset_id(graph: Graph, assertions: Sequence[CandidateAssertion]) -> str:
        material = "\x00".join(
            (str(graph.version), *(assertion.id for assertion in sorted(assertions, key=lambda item: item.id)))
        )
        return f"changeset:deterministic:{sha256(material.encode('utf-8')).hexdigest()}"
