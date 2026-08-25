"""Tests for pure, deterministic ChangeSet graph application."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from intent_engineering.core.graph.applier import (
    CaseEffectsRequireExecutor,
    ContradictoryChangeSet,
    DuplicateIdentity,
    StaleGraphVersion,
    UnknownIdentity,
    apply_changeset,
)
from intent_engineering.core.models import (
    ChangeKind,
    ChangeSet,
    ConfidenceChange,
    Edge,
    EdgeUpdate,
    Graph,
    ImplementationStatus,
    ImplementationStatusChange,
    Node,
    NodeType,
    NodeUpdate,
    RelationType,
    SourceMode,
)

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


def edge(edge_id: str, *, status: str = "active") -> Edge:
    return Edge(
        id=edge_id,
        **{"from": "req-1", "to": "req-2"},
        relation=RelationType.VERIFIED_BY,
        status=status,
        created_by="tester",
        created_at=NOW,
        last_modified_by="tester",
        last_modified_at=NOW,
    )


def connected_graph() -> Graph:
    return Graph(
        id="graph-1",
        version=4,
        nodes=(node("req-1"), node("req-2")),
        edges=(edge("edge-1"),),
    )


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


def test_apply_changeset_materializes_node_update_and_supersession_groups() -> None:
    replacement = node("req-1").model_copy(update={"label": "Updated"})
    updated = apply_changeset(
        graph(),
        changeset(nodes_updated=(NodeUpdate(node_id="req-1", replacement=replacement),)),
    )
    superseded = apply_changeset(
        updated,
        changeset(
            id="cs-2",
            baseline_graph_version=5,
            nodes_superseded=("req-1",),
        ),
    )

    assert updated.nodes[0].label == "Updated"
    assert superseded.nodes[0].status == "superseded"


def test_apply_changeset_materializes_all_edge_groups() -> None:
    added = apply_changeset(
        connected_graph(),
        changeset(edges_added=(edge("edge-2"),)),
    )
    replacement = edge("edge-1", status="reviewed")
    updated = apply_changeset(
        added,
        changeset(
            id="cs-2",
            baseline_graph_version=5,
            edges_updated=(EdgeUpdate(edge_id="edge-1", replacement=replacement),),
        ),
    )
    superseded = apply_changeset(
        updated,
        changeset(
            id="cs-3",
            baseline_graph_version=6,
            edges_superseded=("edge-2",),
        ),
    )

    assert {item.id for item in added.edges} == {"edge-1", "edge-2"}
    assert next(item for item in updated.edges if item.id == "edge-1").status == "reviewed"
    assert next(item for item in superseded.edges if item.id == "edge-2").status == "superseded"


def test_apply_changeset_materializes_confidence_change_as_canonical_node_update() -> None:
    initial = graph().model_copy(
        update={
            "nodes": (
                node("req-1").model_copy(update={"intent_fidelity_confidence": 0.5}),
            )
        }
    )
    reassessed = datetime(2026, 8, 25, 1, tzinfo=UTC)
    change = ConfidenceChange(
        change_id="confidence:req-1",
        timestamp=reassessed,
        actor="reviewer",
        subject_ref="req-1",
        change_kind=ChangeKind.REFINE,
        prior_confidence=0.5,
        new_confidence=0.8,
        evidence_refs=("ev-2",),
        reason="Reviewed implementation evidence.",
    )

    result = apply_changeset(
        initial,
        changeset(evidence_refs=("ev-2",), confidence_changes=(change,)),
    )

    updated = result.nodes[0]
    assert updated.id == "req-1"
    assert updated.created_by == "tester"
    assert updated.intent_fidelity_confidence == 0.8
    assert updated.confidence_basis == "Reviewed implementation evidence."
    assert updated.last_reassessed_at == reassessed
    assert updated.last_modified_by == "reviewer"
    assert updated.last_modified_at == reassessed
    assert updated.evidence_refs == ("ev-1", "ev-2")


def test_apply_changeset_rejects_confidence_prior_mismatch_before_version_change() -> None:
    initial = graph().model_copy(
        update={
            "nodes": (
                node("req-1").model_copy(update={"intent_fidelity_confidence": 0.4}),
            )
        }
    )
    change = ConfidenceChange(
        change_id="confidence:req-1",
        timestamp=NOW,
        actor="reviewer",
        subject_ref="req-1",
        change_kind=ChangeKind.REFINE,
        prior_confidence=0.5,
        new_confidence=0.8,
        evidence_refs=("ev-1",),
        reason="Reviewed evidence.",
    )

    with pytest.raises(ContradictoryChangeSet, match="confidence prior mismatch"):
        apply_changeset(initial, changeset(confidence_changes=(change,)))

    assert initial.version == 4
    assert initial.nodes[0].intent_fidelity_confidence == 0.4


def test_apply_changeset_materializes_implementation_status_on_node() -> None:
    initial = graph().model_copy(
        update={
            "nodes": (
                node("req-1").model_copy(
                    update={"implementation_status": ImplementationStatus.UNKNOWN}
                ),
            )
        }
    )
    change = ImplementationStatusChange(
        claim_id="req-1",
        prior=ImplementationStatus.UNKNOWN,
        new=ImplementationStatus.PARTIAL,
        evidence_refs=("ev-2",),
    )

    result = apply_changeset(
        initial,
        changeset(
            actor="implementation-reviewer",
            timestamp=datetime(2026, 8, 25, 2, tzinfo=UTC),
            evidence_refs=("ev-2",),
            implementation_status_changes=(change,),
        ),
    )

    updated = result.nodes[0]
    assert updated.id == "req-1"
    assert updated.created_at == NOW
    assert updated.implementation_status is ImplementationStatus.PARTIAL
    assert updated.last_modified_by == "implementation-reviewer"
    assert updated.last_modified_at == datetime(2026, 8, 25, 2, tzinfo=UTC)
    assert updated.evidence_refs == ("ev-1", "ev-2")


@pytest.mark.parametrize(
    ("group", "value"),
    [
        ("reconciliation_cases_created", ("case-1",)),
        ("reconciliation_cases_resolved", ("case-1",)),
    ],
)
def test_graph_only_applier_rejects_reconciliation_effects(
    group: str,
    value: tuple[str, ...],
) -> None:
    with pytest.raises(CaseEffectsRequireExecutor, match="transaction-level executor"):
        apply_changeset(graph(), changeset(**{group: value}))
