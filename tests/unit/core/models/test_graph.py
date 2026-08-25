from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import Edge, Graph, Node, NodeType, SourceMode, TypeRegistry

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def node(node_id: str, *, node_type: NodeType | str = NodeType.REQUIREMENT) -> Node:
    return Node(
        id=node_id,
        type=node_type,
        label="Export is local-first",
        status="active",
        created_by="tester",
        created_at=NOW,
        last_modified_by="tester",
        last_modified_at=NOW,
        source_mode=SourceMode.EXPLICIT,
        intent_fidelity_confidence=0.9,
        evidence_refs=["ev-1"],
    )


def edge(edge_id: str, *, to_id: str = "req-2", external: bool = False) -> Edge:
    return Edge(
        id=edge_id,
        from_id="req-1",
        relation="VERIFIED_BY",
        to_id=to_id,
        status="active",
        created_by="tester",
        created_at=NOW,
        last_modified_by="tester",
        last_modified_at=NOW,
        external=external,
    )


def test_confidence_range_is_validated() -> None:
    payload = node("req-1").model_dump()
    payload["intent_fidelity_confidence"] = 1.1
    with pytest.raises(ValidationError):
        Node.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "payload", "unknown_field"),
    [
        (Node, lambda: node("req-1").model_dump(), "labell"),
        (Edge, lambda: edge("e-1", external=True).model_dump(by_alias=True), "externall"),
        (
            Graph,
            lambda: Graph(id="g", version=0, nodes=(), edges=()).model_dump(),
            "nodez",
        ),
        (TypeRegistry, lambda: TypeRegistry().model_dump(), "extensionz"),
    ],
)
def test_public_graph_models_reject_unknown_fields(
    model: type[Node | Edge | Graph | TypeRegistry],
    payload: object,
    unknown_field: str,
) -> None:
    """A misspelled canonical field must fail rather than vanish on rewrite."""
    raw = payload()
    assert isinstance(raw, dict)
    raw[unknown_field] = "unexpected"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(raw)


def test_duplicate_node_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate node id: req-1"):
        Graph(id="g", version=0, nodes=[node("req-1"), node("req-1")], edges=[])


def test_duplicate_edge_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate edge id: e-1"):
        Graph(
            id="g",
            version=0,
            nodes=[node("req-1"), node("req-2")],
            edges=[edge("e-1"), edge("e-1")],
        )


def test_edge_to_missing_node_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing node: test-missing"):
        Graph(id="g", version=0, nodes=[node("req-1")], edges=[edge("e-1", to_id="test-missing")])


def test_unknown_relation_is_rejected() -> None:
    payload = edge("e-1").model_dump()
    payload["relation"] = "UNKNOWN_RELATION"

    with pytest.raises(ValidationError):
        Edge.model_validate(payload)


def test_external_edge_can_target_an_external_node() -> None:
    graph = Graph(
        id="g",
        version=0,
        nodes=[node("req-1")],
        edges=[edge("e-1", to_id="external-test", external=True)],
    )

    assert graph.edges[0].to_id == "external-test"


def test_provenance_backed_node_requires_evidence() -> None:
    payload = node("req-1").model_dump()
    payload["evidence_refs"] = ()
    with pytest.raises(ValueError, match="provenance-backed node requires evidence: req-1"):
        Graph(id="g", version=0, nodes=[Node.model_validate(payload)], edges=[])


def test_registered_namespaced_extension_is_accepted() -> None:
    graph = Graph(
        id="g",
        version=0,
        nodes=[node("req-1", node_type="acme:service_level_objective")],
        edges=[],
        type_registry=TypeRegistry(extensions={"acme:service_level_objective"}),
    )

    assert graph.nodes[0].type == "acme:service_level_objective"


def test_unregistered_or_unnamespaced_node_types_are_rejected() -> None:
    with pytest.raises(ValueError, match="unregistered node type: acme:service_level_objective"):
        Graph(
            id="g",
            version=0,
            nodes=[node("req-1", node_type="acme:service_level_objective")],
            edges=[],
        )
    with pytest.raises(ValueError, match="namespaced"):
        Graph(id="g", version=0, nodes=[node("req-1", node_type="CUSTOM")], edges=[])


def test_registry_only_accepts_namespaced_extension_names() -> None:
    with pytest.raises(ValidationError, match="namespaced"):
        TypeRegistry(extensions={"CUSTOM"})


def test_node_type_covers_the_starter_meta_model() -> None:
    assert {item.value for item in NodeType} == {
        "SOURCE_ARTIFACT",
        "SOURCE_REVISION",
        "EVIDENCE_SPAN",
        "ACTOR",
        "SOURCE_SYSTEM",
        "CONTEXT",
        "NEED",
        "PRODUCT_INTENT",
        "DESIRED_OUTCOME",
        "ASSUMPTION",
        "PRINCIPLE",
        "CONSTRAINT",
        "REQUIREMENT",
        "CAPABILITY",
        "ACCEPTANCE_CRITERION",
        "DECISION",
        "ARCHITECTURE",
        "INTERFACE",
        "DATA_CONTRACT",
        "POLICY",
        "REPOSITORY",
        "MODULE",
        "FILE",
        "SYMBOL",
        "ENDPOINT",
        "SCHEMA",
        "TEST",
        "BUILD_ARTIFACT",
        "DEPLOYMENT",
        "METRIC",
        "OBSERVED_OUTCOME",
        "INCIDENT",
        "USER_FEEDBACK",
        "EXPERIMENT_RESULT",
        "CHANGESET",
        "RECONCILIATION_CASE",
        "REVIEW_DECISION",
        "OWNER",
        "MILESTONE",
    }
