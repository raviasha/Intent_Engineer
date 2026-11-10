"""Pure application of validated ChangeSets to an immutable graph."""

from __future__ import annotations

from intent_engineering.core.models import ChangeSet, Edge, Graph, ImplementationStatus, Node


class GraphApplicationError(ValueError):
    """Base class for deterministic ChangeSet application failures."""


class StaleGraphVersion(GraphApplicationError):
    """Raised when a ChangeSet was prepared against an older graph version."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"stale graph version: expected {expected}, found {actual}")


class DuplicateIdentity(GraphApplicationError):
    """Raised when a ChangeSet adds a graph identity that already exists."""

    def __init__(self, identity: str) -> None:
        self.identity = identity
        super().__init__(f"duplicate identity: {identity}")


class UnknownIdentity(GraphApplicationError):
    """Raised when a ChangeSet mutates an identity absent from the graph."""

    def __init__(self, identity: str) -> None:
        self.identity = identity
        super().__init__(f"unknown identity: {identity}")


class ContradictoryChangeSet(GraphApplicationError):
    """Raised when a declared prior state disagrees with canonical state."""


class CaseEffectsRequireExecutor(GraphApplicationError):
    """Raised when reconciliation effects reach the graph-only boundary."""

    def __init__(self) -> None:
        super().__init__("reconciliation effects require transaction-level executor")


def apply_changeset(graph: Graph, changeset: ChangeSet) -> Graph:
    """Apply graph-only groups and reject reconciliation effects explicitly."""
    if changeset.reconciliation_cases_created or changeset.reconciliation_cases_resolved:
        raise CaseEffectsRequireExecutor()
    return apply_changeset_with_case_effects(graph, changeset)


def apply_changeset_with_case_effects(graph: Graph, changeset: ChangeSet) -> Graph:
    """Apply graph groups when a transaction executor owns the declared case effects."""
    if changeset.baseline_graph_version != graph.version:
        raise StaleGraphVersion(changeset.baseline_graph_version, graph.version)

    nodes: dict[str, Node] = {item.id: item for item in graph.nodes}
    edges: dict[str, Edge] = {item.id: item for item in graph.edges}

    # Validate state-dependent groups before constructing any replacement record.
    for confidence_change in changeset.confidence_changes:
        current = nodes.get(confidence_change.subject_ref)
        if current is None:
            raise UnknownIdentity(confidence_change.subject_ref)
        if current.intent_fidelity_confidence != confidence_change.prior_confidence:
            raise ContradictoryChangeSet(
                f"confidence prior mismatch: {confidence_change.subject_ref}"
            )
    for status_change in changeset.implementation_status_changes:
        current = nodes.get(status_change.claim_id)
        if current is None:
            raise UnknownIdentity(status_change.claim_id)
        current_status = current.implementation_status or ImplementationStatus.UNKNOWN
        if current_status is not status_change.prior:
            raise ContradictoryChangeSet(
                f"implementation prior mismatch: {status_change.claim_id}"
            )
    for node_to_add in changeset.nodes_added:
        if node_to_add.id in nodes:
            raise DuplicateIdentity(node_to_add.id)
        nodes[node_to_add.id] = node_to_add
    for node_update in changeset.nodes_updated:
        if node_update.node_id not in nodes:
            raise UnknownIdentity(node_update.node_id)
        nodes[node_update.node_id] = node_update.replacement
    for node_id in changeset.nodes_superseded:
        if node_id not in nodes:
            raise UnknownIdentity(node_id)
        current_node = nodes[node_id]
        nodes[node_id] = current_node.model_copy(update={"status": "superseded"})
    for edge_to_add in changeset.edges_added:
        if edge_to_add.id in edges:
            raise DuplicateIdentity(edge_to_add.id)
        edges[edge_to_add.id] = edge_to_add
    for edge_update in changeset.edges_updated:
        if edge_update.edge_id not in edges:
            raise UnknownIdentity(edge_update.edge_id)
        edges[edge_update.edge_id] = edge_update.replacement
    for edge_id in changeset.edges_superseded:
        if edge_id not in edges:
            raise UnknownIdentity(edge_id)
        current_edge = edges[edge_id]
        edges[edge_id] = current_edge.model_copy(update={"status": "superseded"})
    for confidence_change in changeset.confidence_changes:
        current = nodes[confidence_change.subject_ref]
        nodes[confidence_change.subject_ref] = Node.model_validate(
            {
                **current.model_dump(mode="python"),
                "intent_fidelity_confidence": confidence_change.new_confidence,
                "confidence_basis": confidence_change.reason,
                "last_reassessed_at": confidence_change.timestamp,
                "last_modified_by": confidence_change.actor,
                "last_modified_at": confidence_change.timestamp,
                "evidence_refs": tuple(
                    dict.fromkeys(
                        (*current.evidence_refs, *confidence_change.evidence_refs)
                    )
                ),
            }
        )
    for status_change in changeset.implementation_status_changes:
        current = nodes[status_change.claim_id]
        nodes[status_change.claim_id] = Node.model_validate(
            {
                **current.model_dump(mode="python"),
                "implementation_status": status_change.new,
                "last_modified_by": changeset.actor,
                "last_modified_at": changeset.timestamp,
                "evidence_refs": tuple(
                    dict.fromkeys((*current.evidence_refs, *status_change.evidence_refs))
                ),
            }
        )
    return Graph.model_validate(
        {
            **graph.model_dump(mode="python"),
            "version": graph.version + 1,
            "nodes": tuple(nodes[key] for key in sorted(nodes)),
            "edges": tuple(edges[key] for key in sorted(edges)),
        }
    )
