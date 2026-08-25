"""Dogfood coverage for the starter framework graph."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from intent_engineering.core.policy.dogfood import (
    import_foundational_evidence,
    resolve_foundational_reference,
)
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore


def test_framework_graph_is_valid_and_provenance_backed() -> None:
    """The shipped starter graph loads through the production graph store."""
    graph = YamlGraphStore(Path("graph/framework-intent-graph.yaml")).load()

    assert graph.id == "intent-engineering-framework"
    assert graph.schema_version == "0.1.0"
    assert graph.version == 0
    assert all(node.evidence_refs for node in graph.nodes if node.source_mode is not None)
    graph.assert_invariants()


def test_framework_graph_spec_references_import_as_section_backed_versioned_evidence(
    tmp_path: Path,
) -> None:
    """Every starter ``spec:<section>`` reference has an immutable foundational evidence row."""
    graph_path = Path("graph/framework-intent-graph.yaml")
    graph = YamlGraphStore(graph_path).load()
    store = JsonlEvidenceStore(tmp_path / "framework-evidence.jsonl")

    imported = import_foundational_evidence(
        graph,
        Path("INTENT_ENGINEERING.md"),
        store,
        observed_at=datetime(2026, 8, 25, tzinfo=UTC),
    )

    references = {
        reference
        for node in graph.nodes
        for reference in node.evidence_refs
        if reference.startswith("spec:")
    }
    assert {record.external_object_id for record in imported} == references
    assert all(record.id.startswith(f"{record.external_object_id}@sha256:") for record in imported)
    assert all(record.external_version.startswith("sha256:") for record in imported)
    assert all(record.content_hash.startswith("sha256:") for record in imported)
    assert all(record.parent_ref == record.external_object_id for record in imported)
    assert all(record.payload["content"] for record in imported)
    assert all(record.payload["section_hash"] == record.content_hash for record in imported)
    assert all(
        record.content_hash == f"sha256:{sha256(record.payload['content'].encode('utf-8')).hexdigest()}"
        for record in imported
    )
    assert resolve_foundational_reference(store, "spec:1").payload["content"].startswith("## 1.")


def test_foundational_evidence_retains_changed_section_versions_and_dedupes_unchanged(
    tmp_path: Path,
) -> None:
    """Changing one numbered section adds only its new immutable version."""
    graph = YamlGraphStore(Path("graph/framework-intent-graph.yaml")).load()
    original = Path("INTENT_ENGINEERING.md").read_bytes()
    spec_path = tmp_path / "INTENT_ENGINEERING.md"
    spec_path.write_bytes(original)
    store = JsonlEvidenceStore(tmp_path / "evidence.jsonl")
    first = import_foundational_evidence(
        graph, spec_path, store, observed_at=datetime(2026, 8, 25, tzinfo=UTC)
    )
    spec_path.write_bytes(
        original.replace(
            b"Intent Engineering is an open framework",
            b"Intent Engineering evolves as an open framework",
            1,
        )
    )
    second = import_foundational_evidence(
        graph, spec_path, store, observed_at=datetime(2026, 8, 26, tzinfo=UTC)
    )

    assert len(store.versions("spec:1")) == 2
    assert len(store.versions("spec:2")) == 1
    assert resolve_foundational_reference(store, "spec:1") == next(
        record for record in second if record.external_object_id == "spec:1"
    )
    assert {record.id for record in first} != {record.id for record in second}
    assert (
        len(
            {record.id for record in first if record.external_object_id != "spec:1"}
            & {record.id for record in second if record.external_object_id != "spec:1"}
        )
        == len(second) - 1
    )


@pytest.mark.parametrize(
    "replacement",
    [
        (b"## 1. Purpose", b"## Purpose"),
        (b"## 2. Core problem", b"## 1. Duplicate purpose"),
    ],
)
def test_foundational_evidence_rejects_missing_or_duplicate_numbered_sections_safely(
    tmp_path: Path, replacement: tuple[bytes, bytes]
) -> None:
    """Malformed foundations fail before writing partial versioned evidence."""
    graph = YamlGraphStore(Path("graph/framework-intent-graph.yaml")).load()
    spec_path = tmp_path / "INTENT_ENGINEERING.md"
    spec_path.write_bytes(Path("INTENT_ENGINEERING.md").read_bytes().replace(*replacement, 1))
    store = JsonlEvidenceStore(tmp_path / "evidence.jsonl")

    with pytest.raises(ValueError, match="numbered section"):
        import_foundational_evidence(
            graph, spec_path, store, observed_at=datetime(2026, 8, 25, tzinfo=UTC)
        )

    assert not store.path.exists()
