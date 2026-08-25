"""Pure application of validated ChangeSets to an immutable graph."""

from __future__ import annotations

from intent_engineering.core.models import ChangeSet, Edge, Graph, Node


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


def apply_changeset(graph: Graph, changeset: ChangeSet) -> Graph:
    """Return the next validated graph without changing the supplied graph."""
    if changeset.baseline_graph_version != graph.version:
        raise StaleGraphVersion(changeset.baseline_graph_version, graph.version)

    nodes: dict[str, Node] = {item.id: item for item in graph.nodes}
    edges: dict[str, Edge] = {item.id: item for item in graph.edges}
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
    return graph.model_copy(
        update={
            "version": graph.version + 1,
            "nodes": tuple(nodes[key] for key in sorted(nodes)),
            "edges": tuple(edges[key] for key in sorted(edges)),
        }
    )
