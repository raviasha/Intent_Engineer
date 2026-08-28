"""Drift declarations may claim semantics but never manufacture provenance."""

from __future__ import annotations

from datetime import UTC, datetime

from intent_engineering.core.models import (
    EvidenceIngestion,
    EvidenceRecord,
    Graph,
    Node,
    NodeType,
    SourceMode,
)
from intent_engineering.reconcile.detectors import EvidenceOrder
from intent_engineering.reconcile.evidence_detection import _record_order
from intent_engineering.reconcile.evidence_detection import (
    detect_evidence_drift as _detect_evidence_drift,
)

MARKDOWN_AT = datetime(2026, 8, 25, 12, tzinfo=UTC)
GIT_AT = datetime(2026, 8, 24, 12, tzinfo=UTC)


def _ingestions(*records: EvidenceRecord) -> tuple[EvidenceIngestion, ...]:
    sequences: dict[str, int] = {}
    predecessors: dict[tuple[str, str, str], str] = {}
    result: list[EvidenceIngestion] = []
    for record in records:
        connector_id = record.connector_type
        sequences[connector_id] = sequences.get(connector_id, 0) + 1
        key = (connector_id, record.connector_type, record.external_object_id)
        result.append(
            EvidenceIngestion(
                connector_id=connector_id,
                sequence=sequences[connector_id],
                predecessor_id=predecessors.get(key),
                evidence=record,
            )
        )
        predecessors[key] = record.id
    return tuple(result)


def detect_evidence_drift(
    records: tuple[EvidenceRecord, ...],
    graph: Graph,
    actor: str,
    ingestions: tuple[EvidenceIngestion, ...] | None = None,
):  # type: ignore[no-untyped-def]
    return _detect_evidence_drift(
        records,
        graph,
        actor,
        ingestions if ingestions is not None else _ingestions(*records),
    )


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
    records = (_declaration(), _git())
    ingestions = _ingestions(*records)
    observations = detect_evidence_drift(
        records,
        _graph("requirement:export"),
        "local@example.test",
        ingestions,
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
    permuted = detect_evidence_drift(
        tuple(reversed(records)),
        _graph("requirement:export"),
        "local@example.test",
        ingestions,
    )
    assert permuted == observations
    assert permuted[0].fingerprint == observations[0].fingerprint


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


def test_changing_consistent_declared_version_integers_cannot_flip_classification() -> None:
    graph = _graph("requirement:export")
    original = _declaration()
    renumbered = original.model_copy(
        update={
            "payload": {
                "detection_input": {
                    **original.payload["detection_input"],  # type: ignore[dict-item]
                    "requirement_version": 200,
                    "implementation_version": 100,
                }
            }
        }
    )

    first = detect_evidence_drift(
        (original, _git()), graph, "local@example.test"
    )
    second = detect_evidence_drift((renumbered, _git()), graph, "local@example.test")

    assert [item.case_type.value for item in first] == ["CODE_LAG"]
    assert second == first


def test_inconsistent_declared_chronology_creates_no_case() -> None:
    graph = _graph("requirement:export")
    original = _declaration()
    inverted = original.model_copy(
        update={
            "payload": {
                "detection_input": {
                    **original.payload["detection_input"],  # type: ignore[dict-item]
                    "requirement_version": 1,
                    "implementation_version": 99,
                }
            }
        }
    )

    assert detect_evidence_drift((inverted, _git()), graph, "local@example.test") == ()


def test_tied_or_non_current_required_chronology_fails_closed() -> None:
    tied_git = _git().model_copy(update={"observed_at": MARKDOWN_AT})
    non_current = _declaration().model_copy(
        update={
            "payload": {
                "detection_input": {
                    **_declaration().payload["detection_input"],  # type: ignore[dict-item]
                    "requirement": {
                        **_declaration().payload["detection_input"]["requirement"],  # type: ignore[index]
                        "current": False,
                    },
                }
            }
        }
    )

    assert detect_evidence_drift(
        (_declaration(), tied_git), _graph("requirement:export"), "local@example.test"
    ) == ()
    assert detect_evidence_drift(
        (non_current, _git()), _graph("requirement:export"), "local@example.test"
    ) == ()


def test_same_object_version_chain_orders_equal_timestamp_evidence_deterministically() -> None:
    earlier = _git("evidence:git-v1").model_copy(
        update={
            "external_object_id": "commit:shared",
            "external_version": "v1",
            "observed_at": GIT_AT,
        }
    )
    later = _git("evidence:git-v2").model_copy(
        update={
            "external_object_id": "commit:shared",
            "external_version": "v2",
            "observed_at": GIT_AT,
        }
    )
    declaration = _declaration().model_copy(
        update={
            "payload": {
                "detection_input": {
                    **_declaration().payload["detection_input"],  # type: ignore[dict-item]
                    "requirement_version": 2,
                    "implementation_version": 1,
                    "requirement": {
                        "label": "requirement",
                        "claim": "new behavior",
                        "evidence_refs": [later.id],
                        "confidence": 0.9,
                    },
                    "implementation": {
                        "label": "implementation",
                        "claim": "old behavior",
                        "evidence_refs": [earlier.id],
                        "confidence": 0.8,
                    },
                }
            }
        }
    )

    observations = detect_evidence_drift(
        (earlier, later, declaration),
        _graph("requirement:export"),
        "local@example.test",
    )

    ingestions = _ingestions(earlier, later, declaration)

    def projected_order(records: tuple[EvidenceRecord, ...]) -> EvidenceOrder:
        by_id = {record.id: record for record in records}
        return _record_order(by_id[later.id], by_id[earlier.id], ingestions)

    records = (earlier, later, declaration)
    assert projected_order(records) is EvidenceOrder.AFTER
    assert projected_order(tuple(reversed(records))) is EvidenceOrder.AFTER

    reversed_observations = detect_evidence_drift(
        (declaration, later, earlier),
        _graph("requirement:export"),
        "local@example.test",
        ingestions,
    )
    assert reversed_observations == observations
    assert observations == ()


def test_old_exact_reference_cannot_declare_itself_current_past_a_durable_successor() -> None:
    old_requirement = _git("evidence:requirement-v1").model_copy(
        update={
            "external_object_id": "requirement:shared",
            "external_version": "v1",
            "observed_at": MARKDOWN_AT,
        }
    )
    new_requirement = _git("evidence:requirement-v2").model_copy(
        update={
            "external_object_id": "requirement:shared",
            "external_version": "v2",
            "observed_at": datetime(2026, 8, 26, tzinfo=UTC),
        }
    )
    implementation = _git("evidence:implementation").model_copy(
        update={"observed_at": GIT_AT}
    )
    declaration = _declaration().model_copy(
        update={
            "payload": {
                "detection_input": {
                    **_declaration().payload["detection_input"],  # type: ignore[dict-item]
                    "requirement_version": None,
                    "implementation_version": None,
                    "requirement": {
                        "label": "requirement",
                        "claim": "stale requirement",
                        "evidence_refs": [old_requirement.id],
                        "current": True,
                        "confidence": 0.9,
                    },
                    "implementation": {
                        "label": "implementation",
                        "claim": "older implementation",
                        "evidence_refs": [implementation.id],
                        "confidence": 0.8,
                    },
                }
            }
        }
    )

    assert detect_evidence_drift(
        (old_requirement, new_requirement, implementation, declaration),
        _graph("requirement:export"),
        "local@example.test",
    ) == ()


def test_mixed_record_chronology_within_one_side_fails_closed() -> None:
    early = _git("evidence:git-early").model_copy(
        update={"observed_at": datetime(2026, 8, 23, tzinfo=UTC)}
    )
    late = _git("evidence:git-late").model_copy(
        update={"observed_at": datetime(2026, 8, 26, tzinfo=UTC)}
    )
    declaration = _declaration().model_copy(
        update={
            "payload": {
                "detection_input": {
                    **_declaration().payload["detection_input"],  # type: ignore[dict-item]
                    "requirement_version": None,
                    "implementation_version": None,
                    "implementation": {
                        "label": "implementation",
                        "claim": "mixed provenance",
                        "evidence_refs": [early.id, late.id],
                        "confidence": 0.8,
                    },
                }
            }
        }
    )

    assert detect_evidence_drift(
        (declaration, early, late),
        _graph("requirement:export"),
        "local@example.test",
    ) == ()
