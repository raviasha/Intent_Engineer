"""Local foundational-spec evidence import used by the framework dogfood check."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from intent_engineering.core.models import EvidenceRecord, Graph
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore


def import_foundational_evidence(
    graph: Graph,
    spec_path: Path,
    store: JsonlEvidenceStore,
    *,
    observed_at: datetime,
) -> Sequence[EvidenceRecord]:
    """Persist one immutable, version-addressed evidence row for each framework spec ref."""
    content = spec_path.read_bytes()
    content_hash = f"sha256:{sha256(content).hexdigest()}"
    references = tuple(
        sorted(
            {
                reference
                for node in graph.nodes
                for reference in node.evidence_refs
                if reference.startswith("spec:")
            }
        )
    )
    records = tuple(
        EvidenceRecord(
            id=reference,
            connector_type="foundational-spec",
            external_object_id=reference,
            external_version=content_hash,
            author="founding-spec",
            observed_at=observed_at,
            source_locator=f"{spec_path.as_posix()}#{reference.removeprefix('spec:')}",
            content_hash=content_hash,
            payload={"section_ref": reference, "spec_version": content_hash},
        )
        for reference in references
    )
    for record in records:
        store.put(record)
    return records
