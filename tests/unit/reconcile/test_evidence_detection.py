"""Drift declarations may claim semantics but never manufacture provenance."""

from __future__ import annotations

from datetime import UTC, datetime

from intent_engineering.core.models import EvidenceRecord, Graph, Node, NodeType, SourceMode
from intent_engineering.reconcile.evidence_detection import detect_evidence_drift

MARKDOWN_AT = datetime(2026, 8, 25, 12, tzinfo=UTC)
GIT_AT = datetime(2026, 8, 24, 12, tzinfo=UTC)


def _node(node_id: str) -> Node:
    return Node(
        id=node_id,
        type=NodeType.REQUIREMENT,
        label="Requirement",
        status="active",
        created_by="product@example.test",
        created_at=MARKDOWN_AT,
        last_modified_by="product@example.test",
        last_modified_at=MARKDOWN_AT,
        source_mode=SourceMode.EXPLICIT,
        evidence_refs=("evidence:markdown",),
    )


def _graph(*node_ids: str) -> Graph:
    return Graph(id="graph", version=1, nodes=tuple(_node(item) for item in node_ids), edges=())


def _declaration() -> EvidenceRecord:
    return EvidenceRecord(
        id="evidence:markdown",
        connector_type="markdown",
        external_object_id="path:requirement.md",
        external_version="sha256:requirement-v2",
        author="product@example.test",
        observed_at=MARKDOWN_AT,
        source_locator="requirement.md",
        content_hash="sha256:requirement-v2",
        payload={
            "detection_input": {
                "schema_version": 1,
                "subject_ref": "requirement:export",
                "affected_refs": ["requirement:export"],
                "compatibility": "aligns",
                "requirement_version": 2,
                "implementation_version": 1,
                "requirement": {
                    "label": "requirement",
                    "claim": "Export locally",
                    "evidence_refs": ["$self"],
                    "authors": ["forged@example.test"],
                    "observed_at": "1999-01-01T00:00:00Z",
                    "confidence": 0.9,
                },
                "implementation": {
                    "label": "implementation",
                    "claim": "Legacy export",
                    "evidence_refs": ["git-path:src/export.py"],
                    "authors": ["forged@example.test"],
                    "observed_at": "1999-01-01T00:00:00Z",
                    "confidence": 0.8,
                },
            }
        },
    )


def _git(
    record_id: str = "evidence:git",
    *,
    acl: tuple[str, ...] = (),
    path: str = "src/export.py",
) -> EvidenceRecord:
    return EvidenceRecord(
        id=record_id,
        connector_type="git",
        external_object_id=f"commit:{record_id}",
        external_version=record_id,
        author="engineer@example.test",
        observed_at=GIT_AT,
        source_locator=f"git:commit:{record_id}",
        content_hash=f"sha256:{record_id}",
        payload={"changed_paths": [path]},
        acl=acl,
    )


def test_detection_derives_every_side_provenance_from_resolved_records() -> None:
    observations = detect_evidence_drift(
        (_declaration(), _git()),
        _graph("requirement:export"),
        "local@example.test",
    )

    assert len(observations) == 1
    assert observations[0].case_type.value == "CODE_LAG"
    requirement, implementation = observations[0].evidence_sides[:2]
    assert requirement.evidence_refs == ("evidence:markdown",)
    assert requirement.authors == ("product@example.test",)
    assert requirement.observed_at == MARKDOWN_AT
    assert implementation.evidence_refs == ("evidence:git",)
    assert implementation.authors == ("engineer@example.test",)
    assert implementation.observed_at == GIT_AT


def test_missing_or_unauthorized_causal_git_evidence_creates_no_case() -> None:
    graph = _graph("requirement:export")

    assert detect_evidence_drift((_declaration(),), graph, "local@example.test") == ()
    assert (
        detect_evidence_drift(
            (_declaration(), _git(acl=("other@example.test",))),
            graph,
            "local@example.test",
        )
        == ()
    )


def test_ambiguous_symbolic_reference_creates_no_case() -> None:
    records = (_declaration(), _git("evidence:git-1"), _git("evidence:git-2"))

    assert (
        detect_evidence_drift(records, _graph("requirement:export"), "local@example.test")
        == ()
    )


def test_disconnected_subject_or_affected_reference_creates_no_case() -> None:
    records = (_declaration(), _git())

    assert detect_evidence_drift(records, _graph(), "local@example.test") == ()
    declaration = _declaration().model_copy(
        update={
            "payload": {
                "detection_input": {
                    **_declaration().payload["detection_input"],  # type: ignore[dict-item]
                    "affected_refs": ["requirement:export", "symbol:missing"],
                }
            }
        }
    )
    assert (
        detect_evidence_drift(
            (declaration, _git()),
            _graph("requirement:export"),
            "local@example.test",
        )
        == ()
    )
