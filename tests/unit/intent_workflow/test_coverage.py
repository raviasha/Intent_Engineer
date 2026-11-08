"""Coverage is explicit structural evidence, never a semantic truth score."""

from intent_engineering.core.models import (
    ClassificationEvent,
    EvidenceSide,
    Graph,
    NodeType,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    RelationType,
)
from tests.unit.intent_workflow.test_reading import NOW, edge, node


def coverage_graph():
    return Graph(
        id="coverage",
        version=1,
        nodes=(
            node("outcome", NodeType.DESIRED_OUTCOME, "Faster finance close"),
            node("constraint", NodeType.CONSTRAINT, "Local only"),
            node("missing", NodeType.DESIRED_OUTCOME, "Reduce errors"),
            node("cap", NodeType.CAPABILITY, "Exports"),
            node("req", NodeType.REQUIREMENT, "Monthly CSV"),
        ),
        edges=(
            edge("outcome-cap", "outcome", RelationType.REALIZED_BY, "cap"),
            edge("cap-req", "cap", RelationType.SPECIFIED_BY, "req"),
            edge("cycle", "cap", RelationType.REFINES, "outcome"),
            edge("constraint-req", "constraint", RelationType.CONSTRAINS, "req"),
            edge("inactive", "missing", RelationType.SPECIFIED_BY, "req", status="superseded"),
        ),
    )


def test_coverage_follows_active_paths_and_explains_unreviewed_meaning():
    from intent_engineering.intent_workflow.coverage import build_coverage

    result = build_coverage(coverage_graph(), ())
    rows = {row["subject_id"]: row for row in result["rows"]}
    assert rows["outcome"]["requirement_ids"] == ["req"]
    assert rows["constraint"]["requirement_ids"] == ["req"]
    assert rows["outcome"]["status"] == "linked_unreviewed"
    assert rows["missing"]["status"] == "missing"
    assert rows["missing"]["requirement_ids"] == []
    assert result["semantic_alignment"] == "requires_human_review"


def case(status):
    return ReconciliationCase(
        id="case",
        subject_ref="missing",
        affected_refs=("missing",),
        case_type=ReconciliationCaseType.POSSIBLE_INTENT_CHANGE,
        detector_id="test",
        fingerprint="1" * 64,
        created_at=NOW,
        created_by="local:reader",
        status=status,
        history=()
        if status == ReconciliationStatus.OPEN
        else (
            ClassificationEvent(
                actor="local:reader", at=NOW, prior=ReconciliationStatus.OPEN, new=status
            ),
        ),
        evidence_sides=(
            EvidenceSide(
                label="decision",
                claim="Review later",
                evidence_refs=("evidence:source",),
                observed_at=NOW,
                authors=("local:reader",),
                confidence=0.9,
            ),
        ),
    )


def test_coverage_distinguishes_recorded_deferral_and_open_dispute():
    from intent_engineering.intent_workflow.coverage import build_coverage

    for status, expected in [
        (ReconciliationStatus.DEFERRED, "deferred"),
        (ReconciliationStatus.OPEN, "disputed"),
    ]:
        rows = build_coverage(coverage_graph(), (case(status),))["rows"]
        row = next(row for row in rows if row["subject_id"] == "missing")
        assert row["status"] == expected
        assert row["case_ids"] == ["case"]


def test_coverage_does_not_use_implementation_or_reversed_specification_edges():
    from intent_engineering.intent_workflow.coverage import build_coverage

    graph = coverage_graph().model_copy(
        update={
            "edges": (
                edge("wrong", "req", RelationType.SPECIFIED_BY, "outcome"),
                edge("code", "constraint", RelationType.IMPLEMENTED_BY, "req"),
            )
        }
    )
    assert all(row["status"] == "missing" for row in build_coverage(graph, ())["rows"])


def test_coverage_keeps_disputes_on_supporting_nodes_and_relationships():
    from intent_engineering.intent_workflow.coverage import build_coverage

    for subject in ("cap", "outcome-cap"):
        dispute = case(ReconciliationStatus.OPEN).model_copy(update={
            "subject_ref": subject, "affected_refs": (subject,)})
        rows = build_coverage(coverage_graph(), (dispute,))["rows"]
        outcome = next(row for row in rows if row["subject_id"] == "outcome")
        assert outcome["status"] == "disputed"
        assert outcome["case_ids"] == ["case"]


def test_coverage_does_not_call_code_lag_an_intent_dispute():
    from intent_engineering.intent_workflow.coverage import build_coverage

    pending = case(ReconciliationStatus.OPEN).model_copy(update={
        "subject_ref": "req", "affected_refs": ("req",),
        "case_type": ReconciliationCaseType.CODE_LAG})
    rows = build_coverage(coverage_graph(), (pending,))["rows"]
    assert next(row for row in rows if row["subject_id"] == "outcome")["status"] == "linked_unreviewed"
