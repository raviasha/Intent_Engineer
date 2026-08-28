"""Local foundational-spec evidence import used by the framework dogfood check."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from intent_engineering.core.models import EvidenceRecord, Graph
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore

_NUMBERED_SECTION = re.compile(r"^## (?P<number>[1-9][0-9]*)\. .+$", re.MULTILINE)


def _normalized_text(spec_path: Path) -> str:
    """Read Markdown with a stable newline representation before section hashing."""
    try:
        return spec_path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as error:
        raise ValueError("foundational spec must be UTF-8") from error


def _numbered_sections(spec_path: Path) -> Mapping[str, str]:
    """Return complete level-two numbered sections delimited by the next numbered heading."""
    text = _normalized_text(spec_path)
    matches = tuple(_NUMBERED_SECTION.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        reference = f"spec:{match.group('number')}"
        if reference in sections:
            raise ValueError(f"duplicate numbered section: {reference}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[reference] = text[match.start() : end]
    if not sections:
        raise ValueError("foundational spec has no numbered sections")
    return sections


def _graph_spec_references(graph: Graph) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                reference
                for node in graph.nodes
                for reference in node.evidence_refs
                if reference.startswith("spec:")
            }
        )
    )


def _record(reference: str, section: str, spec_path: Path, observed_at: datetime) -> EvidenceRecord:
    content = section.encode("utf-8")
    content_hash = f"sha256:{sha256(content).hexdigest()}"
    return EvidenceRecord(
        id=f"{reference}@{content_hash}",
        connector_type="foundational-spec",
        external_object_id=reference,
        external_version=content_hash,
        author="founding-spec",
        observed_at=observed_at,
        source_locator=f"{spec_path.as_posix()}#{reference.removeprefix('spec:')}",
        content_hash=content_hash,
        payload={"section_ref": reference, "section_hash": content_hash, "content": section},
        parent_ref=reference,
    )


def import_foundational_evidence(
    graph: Graph,
    spec_path: Path,
    store: JsonlEvidenceStore,
    *,
    observed_at: datetime,
) -> Sequence[EvidenceRecord]:
    """Persist one immutable content-addressed row for every framework ``spec:<section>`` ref."""
    sections = _numbered_sections(spec_path)
    references = _graph_spec_references(graph)
    missing = tuple(reference for reference in references if reference not in sections)
    if missing:
        raise ValueError(f"missing numbered section: {missing[0]}")
    imported: list[EvidenceRecord] = []
    for reference in references:
        candidate = _record(reference, sections[reference], spec_path, observed_at)
        existing = {record.id: record for record in store.versions(reference)}
        record = existing.get(candidate.id, candidate)
        if record is candidate:
            store.put(record)
        imported.append(record)
    return tuple(imported)


def resolve_foundational_reference(store: JsonlEvidenceStore, reference: str) -> EvidenceRecord:
    """Resolve a stable graph reference to a deterministic current immutable section version."""
    versions = store.versions(reference)
    if not versions:
        raise KeyError(reference)
    return max(
        versions, key=lambda record: (record.observed_at, record.external_version, record.id)
    )
