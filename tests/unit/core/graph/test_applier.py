"""Tests for pure, deterministic ChangeSet graph application."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from intent_engineering.core.graph.applier import (
    DuplicateIdentity,
    StaleGraphVersion,
    UnknownIdentity,
    apply_changeset,
)
from intent_engineering.core.models import ChangeSet, Graph, Node, NodeType, SourceMode

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def node(node_id: str) -> Node:
    return Node(
        id=node_id,
        type=NodeType.REQUIREMENT,
        label="Export is local-first",
        status="active",
        created_by="tester",
        created_at=NOW,
        last_modified_by="tester",
        last_modified_at=NOW,
        source_mode=SourceMode.EXPLICIT,
        evidence_refs=("ev-1",),
    )


def graph() -> Graph:
    return Graph(id="graph-1", version=4, nodes=(node("req-1"),), edges=())


def changeset(**changes: object) -> ChangeSet:
    payload: dict[str, object] = {
        "id": "cs-1",
        "actor": "tester",
        "timestamp": NOW,
        "baseline_graph_version": 4,
        "evidence_refs": ("ev-1",),
        "nodes_added": (),
        "nodes_updated": (),
        "nodes_superseded": (),
        "edges_added": (),
        "edges_updated": (),
        "edges_superseded": (),
        "confidence_changes": (),
        "implementation_status_changes": (),
        "reconciliation_cases_created": (),
        "reconciliation_cases_resolved": (),
        "validation_status": "approved",
    }
    payload.update(changes)
    return ChangeSet(**payload)


def test_apply_changeset_adds_a_node_and_increments_version_once() -> None:
    result = apply_changeset(graph(), changeset(nodes_added=(node("req-2"),)))

    assert result.version == 5
    assert tuple(item.id for item in result.nodes) == ("req-1", "req-2")


def test_apply_changeset_rejects_a_stale_baseline() -> None:
    with pytest.raises(StaleGraphVersion, match="expected 3, found 4"):
        apply_changeset(graph(), changeset(baseline_graph_version=3))


def test_apply_changeset_rejects_duplicate_added_identity() -> None:
    with pytest.raises(DuplicateIdentity, match="req-1"):
        apply_changeset(graph(), changeset(nodes_added=(node("req-1"),)))


def test_apply_changeset_rejects_unknown_update_identity() -> None:
    with pytest.raises(UnknownIdentity, match="missing-node"):
        apply_changeset(graph(), changeset(nodes_superseded=("missing-node",)))
