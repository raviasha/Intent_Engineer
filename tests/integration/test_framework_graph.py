"""Dogfood coverage for the starter framework graph."""

from datetime import UTC, datetime
from pathlib import Path

from intent_engineering.core.policy.dogfood import import_foundational_evidence
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


def test_framework_graph_spec_references_import_as_versioned_foundational_evidence(
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
    assert {record.id for record in imported} == references
    assert all(record.external_version.startswith("sha256:") for record in imported)
    assert all(record.content_hash.startswith("sha256:") for record in imported)
    assert {record.id for record in store.versions("spec:1")} == {"spec:1"}
