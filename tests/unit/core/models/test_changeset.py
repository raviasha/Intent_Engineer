"""Tests for validated, immutable semantic ChangeSets."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import (
    CandidateAssertion,
    ChangeKind,
    ChangeSet,
    ConfidenceChange,
    Edge,
    EdgeUpdate,
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


def edge(edge_id: str) -> Edge:
    return Edge(
        id=edge_id,
        from_id="req-1",
        relation=RelationType.VERIFIED_BY,
        to_id="test-1",
        status="active",
        created_by="tester",
        created_at=NOW,
        last_modified_by="tester",
        last_modified_at=NOW,
    )


def confidence_change(subject_id: str) -> ConfidenceChange:
    return ConfidenceChange(
        change_id=f"confidence-{subject_id}",
        timestamp=NOW,
        actor="tester",
        subject_ref=subject_id,
        change_kind=ChangeKind.REFINE,
        prior_confidence=0.5,
        new_confidence=0.8,
        evidence_refs=("ev-1",),
        reason="Reviewed evidence.",
    )


def changeset(**changes: object) -> ChangeSet:
    payload: dict[str, object] = {
        "id": "cs-1",
        "actor": "tester",
        "timestamp": NOW,
        "baseline_graph_version": 0,
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
        "validation_status": "pending",
    }
    payload.update(changes)
    return ChangeSet(**payload)


def test_empty_changeset_is_valid_and_not_semantic() -> None:
    result = changeset(evidence_refs=())

    assert result.is_empty is True
    assert result.is_semantic is False


def test_semantic_changeset_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="semantic ChangeSet requires evidence"):
        changeset(evidence_refs=(), nodes_superseded=("req-1",))


@pytest.mark.parametrize(
    ("update_type", "identifier", "replacement"),
    [
        (NodeUpdate, "req-1", node("req-2")),
        (EdgeUpdate, "edge-1", edge("edge-2")),
    ],
)
def test_update_replacement_must_keep_its_stable_id(
    update_type: type[NodeUpdate | EdgeUpdate], identifier: str, replacement: Node | Edge
) -> None:
    with pytest.raises(ValidationError, match="replacement id must match"):
        update_type(**{"node_id" if update_type is NodeUpdate else "edge_id": identifier, "replacement": replacement})


@pytest.mark.parametrize(
    ("group", "value"),
    [
        ("nodes_added", (node("req-1"), node("req-1"))),
        ("nodes_updated", (NodeUpdate(node_id="req-1", replacement=node("req-1")),) * 2),
        ("nodes_superseded", ("req-1", "req-1")),
        ("edges_added", (edge("edge-1"), edge("edge-1"))),
        ("edges_updated", (EdgeUpdate(edge_id="edge-1", replacement=edge("edge-1")),) * 2),
        ("edges_superseded", ("edge-1", "edge-1")),
        ("confidence_changes", (confidence_change("req-1"), confidence_change("req-1"))),
        (
            "implementation_status_changes",
            (
                ImplementationStatusChange(
                    claim_id="claim-1",
                    prior=ImplementationStatus.UNKNOWN,
                    new=ImplementationStatus.PARTIAL,
                    evidence_refs=("ev-1",),
                ),
            )
            * 2,
        ),
        ("reconciliation_cases_created", ("case-1", "case-1")),
        ("reconciliation_cases_resolved", ("case-1", "case-1")),
    ],
)
def test_changeset_rejects_duplicate_subjects_within_a_mutation_group(
    group: str, value: object
) -> None:
    with pytest.raises(ValidationError, match=f"duplicate subject in {group}"):
        changeset(**{group: value})


@pytest.mark.parametrize(
    ("update_group", "supersede_group", "update_value", "supersede_value"),
    [
        ("nodes_updated", "nodes_superseded", (NodeUpdate(node_id="req-1", replacement=node("req-1")),), ("req-1",)),
        ("edges_updated", "edges_superseded", (EdgeUpdate(edge_id="edge-1", replacement=edge("edge-1")),), ("edge-1",)),
    ],
)
def test_changeset_rejects_subjects_updated_and_superseded(
    update_group: str, supersede_group: str, update_value: object, supersede_value: object
) -> None:
    with pytest.raises(ValidationError, match="cannot both update and supersede"):
        changeset(**{update_group: update_value, supersede_group: supersede_value})


@pytest.mark.parametrize(
    ("first_group", "second_group", "first_value", "second_value", "message"),
    [
        (
            "nodes_added",
            "nodes_updated",
            (node("req-1"),),
            (NodeUpdate(node_id="req-1", replacement=node("req-1")),),
            "cannot both add and update node",
        ),
        (
            "nodes_added",
            "nodes_superseded",
            (node("req-1"),),
            ("req-1",),
            "cannot both add and supersede node",
        ),
        (
            "edges_added",
            "edges_updated",
            (edge("edge-1"),),
            (EdgeUpdate(edge_id="edge-1", replacement=edge("edge-1")),),
            "cannot both add and update edge",
        ),
        (
            "edges_added",
            "edges_superseded",
            (edge("edge-1"),),
            ("edge-1",),
            "cannot both add and supersede edge",
        ),
        (
            "reconciliation_cases_created",
            "reconciliation_cases_resolved",
            ("case-1",),
            ("case-1",),
            "cannot both create and resolve reconciliation case",
        ),
    ],
)
def test_changeset_rejects_incompatible_subject_overlaps(
    first_group: str,
    second_group: str,
    first_value: object,
    second_value: object,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        changeset(**{first_group: first_value, second_group: second_value})


def test_changeset_normalizes_all_mutation_groups_to_tuples() -> None:
    result = changeset(nodes_added=[node("req-1")])

    assert result.nodes_added == (node("req-1"),)
    assert result.is_empty is False
    assert result.is_semantic is True


def test_candidate_assertion_default_attributes_are_immutable() -> None:
    assertion = CandidateAssertion(
        id="candidate-1",
        subject_id="req-1",
        change_kind=ChangeKind.CONFIRM,
        node_type=NodeType.REQUIREMENT,
        label="Export is local-first",
        source_mode=SourceMode.EXPLICIT,
        evidence_refs=("ev-1",),
        confidence=0.8,
    )

    assert assertion.attributes == {}
    with pytest.raises(TypeError):
        assertion.attributes["reviewed"] = True  # type: ignore[index]
