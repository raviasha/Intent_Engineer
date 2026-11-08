"""Reading pages must explain authorized graph assertions without inventing meaning."""

from datetime import UTC, datetime

import pytest

from intent_engineering.assessment.models import AssessmentSnapshot
from intent_engineering.core.models import Edge, Graph, Node, NodeType, RelationType, SourceMode

NOW = datetime(2026, 11, 8, tzinfo=UTC)
META = {
    "status": "active",
    "created_by": "local:reader",
    "created_at": NOW,
    "last_modified_by": "local:reader",
    "last_modified_at": NOW,
}


def node(identifier, kind, label, **kwargs):
    return Node(
        id=identifier, type=kind, label=label, evidence_refs=("evidence:source",), **(META | kwargs)
    )


def edge(identifier, source, relation, target, **kwargs):
    return Edge(id=identifier, from_id=source, relation=relation, to_id=target, **(META | kwargs))


def snapshot(graph):
    digest = "sha256:" + "1" * 64
    return AssessmentSnapshot(
        project_id="reading",
        graph=graph,
        evidence=(),
        ingestions=(),
        cases=(),
        clarifications=(),
        history=(),
        **{
            key: digest
            for key in (
                "graph_digest",
                "evidence_digest",
                "ingestion_digest",
                "case_digest",
                "clarification_digest",
                "history_digest",
                "config_digest",
                "principal_projection_digest",
                "aggregate_digest",
            )
        },
    )


def graph():
    return Graph(
        id="reading",
        version=1,
        nodes=(
            node(
                "intent",
                NodeType.PRODUCT_INTENT,
                "Help finance close each month",
                source_mode=SourceMode.EXPLICIT,
            ),
            node("req", NodeType.REQUIREMENT, "Monthly export"),
            node(
                "assumption",
                NodeType.ASSUMPTION,
                "Finance prefers CSV",
                source_mode=SourceMode.INFERRED,
            ),
            node("unrelated", NodeType.PRODUCT_INTENT, "Unrelated confidential topic"),
        ),
        edges=(
            edge("link", "intent", RelationType.SPECIFIED_BY, "req"),
            edge("assumes", "req", RelationType.ASSUMES, "assumption"),
        ),
    )


def test_reading_page_contains_related_assertions_and_labels_assumptions():
    from intent_engineering.intent_workflow.reading import build_reading_page

    source = snapshot(graph())
    before = source.graph.model_dump_json()
    page = build_reading_page(source, "req")
    assert page["title"] == "Monthly export"
    assert page["snapshot_digest"] == source.aggregate_digest
    assertions = {item["id"]: item for item in page["assertions"]}
    assert set(assertions) == {"intent", "req", "assumption"}
    assert assertions["assumption"]["interpretation"] == "assumption"
    assert assertions["intent"]["interpretation"] == "explicit"
    assert source.graph.model_dump_json() == before
    assert "confidential" not in str(page)


def test_reading_page_rejects_unknown_and_inactive_links_do_not_expand_topic():
    from intent_engineering.intent_workflow.reading import build_reading_page

    original = graph()
    source = snapshot(
        original.model_copy(
            update={
                "edges": tuple(
                    item.model_copy(update={"status": "superseded"}) for item in original.edges
                )
            }
        )
    )
    assert len(build_reading_page(source, "req")["assertions"]) == 1
    with pytest.raises(LookupError):
        build_reading_page(source, "unknown")
