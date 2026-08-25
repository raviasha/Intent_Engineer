"""Deep, redacted validation of one recovered cross-store workspace snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import (
    ChangeSet,
    EvidenceRecord,
    EvidenceSide,
    Node,
    NodeType,
    ReconciliationCase,
    SourceMode,
)
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import serialize_case
from intent_engineering.storage.jsonl.history_store import serialize_changeset
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import serialize_graph
from intent_engineering.validation import validate_project

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


@dataclass(frozen=True)
class SeededProject:
    root: Path
    evidence: EvidenceRecord
    node: Node
    changeset: ChangeSet


def _record(
    *,
    content: str = "# Durable requirement\n",
    external_version: str | None = None,
    external_object_id: str = "path:requirement.md",
) -> EvidenceRecord:
    content_hash = f"sha256:{sha256(content.encode('utf-8')).hexdigest()}"
    version = external_version or content_hash
    return normalize_raw_source(
        RawSourceObject(
            connector_type="markdown",
            external_object_id=external_object_id,
            external_version=version,
            author="author@example.test",
            observed_at=NOW,
            source_locator=external_object_id.removeprefix("path:"),
            content_hash=content_hash,
            payload={"path": external_object_id.removeprefix("path:"), "content": content},
        )
    )


def _changeset(
    evidence: EvidenceRecord,
    node: Node,
    *,
    baseline: int = 0,
) -> ChangeSet:
    return ChangeSet(
        id="changeset:seed",
        actor="author@example.test",
        timestamp=NOW,
        baseline_graph_version=baseline,
        evidence_refs=(evidence.id,),
        nodes_added=(node,),
        nodes_updated=(),
        nodes_superseded=(),
        edges_added=(),
        edges_updated=(),
        edges_superseded=(),
        confidence_changes=(),
        implementation_status_changes=(),
        reconciliation_cases_created=(),
        reconciliation_cases_resolved=(),
        validation_status="approved",
    )


def _seed(tmp_path: Path) -> SeededProject:
    root = tmp_path / "project"
    root.mkdir()
    initialize_project(root)
    runtime = load_runtime(root)
    evidence = _record()
    runtime.evidence_store.put(evidence)
    node = Node(
        id="requirement:seed",
        type=NodeType.REQUIREMENT,
        label="Durable requirement",
        status="active",
        created_by="author@example.test",
        created_at=NOW,
        last_modified_by="author@example.test",
        last_modified_at=NOW,
        source_mode=SourceMode.EXPLICIT,
        evidence_refs=(evidence.id,),
    )
    changeset = _changeset(evidence, node)
    LocalChangeSetExecutor(
        runtime.graph_store,
        runtime.case_store,
        runtime.transactions,
    ).apply(changeset)
    return SeededProject(root, evidence, node, changeset)


def _codes(root: Path) -> tuple[str, ...]:
    return tuple(item.code for item in validate_project(root).diagnostics)


def test_valid_workspace_reports_a_versioned_empty_diagnostic_set(tmp_path: Path) -> None:
    seeded = _seed(tmp_path)

    report = validate_project(seeded.root)

    assert report.valid is True
    assert report.schema_version == "1"
    assert report.graph_id == f"graph:{seeded.root.name}"
    assert report.graph_version == 1
    assert report.diagnostics == ()


def test_graph_and_case_references_are_resolved_against_the_same_snapshot(
    tmp_path: Path,
) -> None:
    seeded = _seed(tmp_path)
    runtime = load_runtime(seeded.root)
    graph = runtime.graph_store.load()
    broken_node = seeded.node.model_copy(update={"evidence_refs": ("missing:graph",)})
    broken_graph = graph.model_copy(update={"nodes": (broken_node,)})
    (seeded.root / ".intent/graph.yaml").write_bytes(serialize_graph(broken_graph))
    side = EvidenceSide(
        label="requirement",
        claim="Different",
        evidence_refs=("missing:case",),
        observed_at=NOW,
        authors=("author@example.test",),
        confidence=0.9,
        source_mode=SourceMode.EXPLICIT,
    )
    case = ReconciliationCase(
        id="case:broken",
        subject_ref="missing:subject",
        affected_refs=("missing:affected",),
        case_type="CODE_LAG",
        evidence_sides=(side,),
        detector_id="fixture",
        fingerprint="a" * 64,
        created_at=NOW,
        created_by="detector:fixture",
    )
    case_path = seeded.root / ".intent/reconciliation/cases.jsonl"
    case_path.write_bytes(serialize_case(case))

    assert set(_codes(seeded.root)) >= {
        "graph.evidence_ref_missing",
        "case.evidence_ref_missing",
        "case.subject_ref_missing",
        "case.affected_ref_missing",
        "history.case_creation_missing",
    }


def test_evidence_ids_versions_hashes_and_parent_links_are_consistent(tmp_path: Path) -> None:
    seeded = _seed(tmp_path)
    second = _record(content="# Changed requirement\n")
    invalid_id = second.model_copy(update={"id": "evidence:not-derived"})
    conflicting_version = _record(
        content="# Conflicting bytes\n",
        external_version=seeded.evidence.external_version,
    )
    missing_parent = _record(
        content="# Child\n",
        external_object_id="path:child.md",
    ).model_copy(update={"parent_ref": "evidence:missing"})
    evidence_path = seeded.root / ".intent/evidence/evidence.jsonl"
    evidence_path.write_text(
        "".join(
            record.model_dump_json() + "\n"
            for record in (seeded.evidence, invalid_id, conflicting_version, missing_parent)
        ),
        encoding="utf-8",
    )

    assert set(_codes(seeded.root)) >= {
        "evidence.id_mismatch",
        "evidence.version_conflict",
        "evidence.parent_ref_missing",
    }


def test_changeset_history_evidence_baselines_ids_and_graph_version_are_consistent(
    tmp_path: Path,
) -> None:
    seeded = _seed(tmp_path)
    invalid = seeded.changeset.model_copy(
        update={
            "baseline_graph_version": 1,
            "evidence_refs": ("missing:history",),
        }
    )
    history = seeded.root / ".intent/history/changesets.jsonl"
    history.write_bytes(serialize_changeset(invalid) + serialize_changeset(invalid))

    assert set(_codes(seeded.root)) >= {
        "history.evidence_ref_missing",
        "history.baseline_mismatch",
        "history.id_duplicate",
        "history.graph_version_mismatch",
    }


def test_case_lifecycle_and_resolution_must_match_changeset_history(tmp_path: Path) -> None:
    seeded = _seed(tmp_path)
    case_path = seeded.root / ".intent/reconciliation/cases.jsonl"
    payload = {
        "id": "case:torn",
        "subject_ref": seeded.node.id,
        "case_type": "CODE_LAG",
        "affected_refs": [seeded.node.id],
        "evidence_sides": [],
        "detector_id": "fixture",
        "fingerprint": "b" * 64,
        "created_at": NOW.isoformat(),
        "created_by": "detector:fixture",
        "status": "resolved",
        "requires_human": True,
        "alternatives": [],
        "impact": "",
        "resolution": "update_implementation",
        "resolved_by_changeset": "changeset:missing",
        "history": [],
    }
    case_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert _codes(seeded.root) == ("cases.invalid",)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"checkpoints": {"unknown": {"connector_id": "unknown", "cursor": None, "committed_at": NOW.isoformat()}}}, "checkpoint.connector_unknown"),
        ({"checkpoints": {"git": {"connector_id": "git", "cursor": "not-a-sha", "committed_at": NOW.isoformat()}}}, "checkpoint.cursor_invalid"),
        ({"checkpoints": {"markdown": {"connector_id": "git", "cursor": None, "committed_at": NOW.isoformat()}}}, "checkpoints.invalid"),
    ],
)
def test_checkpoint_structure_connector_and_cursor_consistency(
    tmp_path: Path,
    payload: dict[str, object],
    expected: str,
) -> None:
    seeded = _seed(tmp_path)
    checkpoint_path = seeded.root / ".intent/cache/checkpoints.yaml"
    checkpoint_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")

    assert expected in _codes(seeded.root)


def test_checkpoint_consumption_boundary_rejects_missing_or_foreign_associations(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    missing = _seed(missing_root)
    checkpoint_path = missing.root / ".intent/cache/checkpoints.yaml"
    checkpoint_path.write_text(
        yaml.safe_dump(
            {
                "checkpoints": {
                    "markdown": {
                        "connector_id": "markdown",
                        "cursor": None,
                        "committed_at": NOW.isoformat(),
                        "consumed_evidence_ids": ["evidence:missing"],
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert "checkpoint.consumed_evidence_missing" in _codes(missing.root)

    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_project = foreign_root / "project"
    foreign_project.mkdir()
    initialize_project(foreign_project)
    runtime = load_runtime(foreign_project)
    evidence = _record()
    assert hasattr(runtime.evidence_store, "associate")
    runtime.evidence_store.associate("other", evidence)  # type: ignore[attr-defined]
    checkpoint_path = foreign_project / ".intent/cache/checkpoints.yaml"
    checkpoint_path.write_text(
        yaml.safe_dump(
            {
                "checkpoints": {
                    "markdown": {
                        "connector_id": "markdown",
                        "cursor": None,
                        "committed_at": NOW.isoformat(),
                            "consumed_evidence_ids": [evidence.id],
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert "checkpoint.consumed_evidence_foreign" in _codes(foreign_project)


@pytest.mark.parametrize("kind", ["missing", "skipped", "reordered", "valid"])
def test_checkpoint_consumption_must_be_an_exact_ledger_prefix(
    tmp_path: Path,
    kind: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    initialize_project(root)
    runtime = load_runtime(root)
    records = tuple(
        _record(content=f"# Version {version}\n") for version in (1, 2, 3)
    )
    for record in records:
        runtime.evidence_store.associate("markdown", record)  # type: ignore[attr-defined]
    ids = tuple(record.id for record in records)
    consumed = {
        "missing": (ids[0], "evidence:missing"),
        "skipped": (ids[0], ids[2]),
        "reordered": (ids[1], ids[0]),
        "valid": ids[:2],
    }[kind]
    checkpoint_path = root / ".intent/cache/checkpoints.yaml"
    checkpoint_path.write_text(
        yaml.safe_dump(
            {
                "checkpoints": {
                    "markdown": {
                        "connector_id": "markdown",
                        "cursor": None,
                        "committed_at": NOW.isoformat(),
                        "consumed_evidence_ids": list(consumed),
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    codes = _codes(root)
    if kind == "valid":
        assert "checkpoint.consumed_evidence_prefix_invalid" not in codes
    else:
        assert "checkpoint.consumed_evidence_prefix_invalid" in codes


def test_custom_legacy_evidence_requires_an_explicit_association_diagnostic(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    initialize_project(root)
    runtime = load_runtime(root)
    base = _record()
    custom = normalize_raw_source(
        RawSourceObject(
            connector_type="shared",
            external_object_id=base.external_object_id,
            external_version=base.external_version,
            author=base.author,
            observed_at=base.observed_at,
            source_locator=base.source_locator,
            content_hash=base.content_hash,
            payload=base.payload,
        )
    )
    runtime.evidence_store.put(custom)

    assert "evidence.legacy_association_ambiguous" in _codes(root)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_sequence",
        "invalid_constant",
        "empty_connector",
        "string_sequence",
        "boolean_schema",
    ],
)
def test_malformed_ingestion_envelopes_are_strictly_parsed_and_redacted(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    initialize_project(root)
    runtime = load_runtime(root)
    runtime.evidence_store.associate("markdown", _record())  # type: ignore[attr-defined]
    evidence_path = root / ".intent/evidence/evidence.jsonl"
    envelope = json.loads(evidence_path.read_text(encoding="utf-8"))
    if mutation == "invalid_constant":
        envelope["evidence"]["payload"]["invalid"] = float("nan")
    elif mutation == "empty_connector":
        envelope["connector_id"] = "   "
    elif mutation == "string_sequence":
        envelope["sequence"] = "1"
    elif mutation == "boolean_schema":
        envelope["storage_schema_version"] = True
    serialized = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
    if mutation == "duplicate_sequence":
        serialized = serialized.replace('"sequence":1', '"sequence":1,"sequence":1')
    evidence_path.write_text(serialized + "\n", encoding="utf-8")

    assert _codes(root) == ("evidence.invalid",)


def test_prepared_transaction_is_recovered_before_validation_parses_targets(
    tmp_path: Path,
) -> None:
    seeded = _seed(tmp_path)
    workspace = SecureDirectory.open(seeded.root / ".intent")

    def crash(stage: str) -> None:
        if stage == "target:history":
            raise SystemExit()

    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        {
            "graph": workspace.file("graph.yaml"),
            "history": workspace.file("history/changesets.jsonl"),
            "cases": workspace.file("reconciliation/cases.jsonl"),
        },
        fault_hook=crash,
    )
    with pytest.raises(SystemExit), coordinator.transaction() as transaction:
        transaction.write("graph", b"torn: [")
        transaction.write("history", b'{"torn":')

    report = validate_project(seeded.root)

    assert report.valid is True
    assert _codes(seeded.root) == ()
    assert tuple(item.code for item in report.diagnostics) == ("transaction.recovered",)
    assert report.diagnostics[0].severity == "notice"
    assert not coordinator.journal_path.exists()


def test_corrupt_transaction_is_a_redacted_diagnostic_and_is_not_removed(tmp_path: Path) -> None:
    seeded = _seed(tmp_path)
    journal = seeded.root / ".intent/history/.local-transaction.json"
    journal.write_text('{"preimages":"private-content"}', encoding="utf-8")

    report = validate_project(seeded.root)

    assert report.valid is False
    assert _codes(seeded.root) == ("transaction.corrupt",)
    assert "private-content" not in report.model_dump_json()
    assert str(seeded.root) not in report.model_dump_json()
    assert journal.exists()
