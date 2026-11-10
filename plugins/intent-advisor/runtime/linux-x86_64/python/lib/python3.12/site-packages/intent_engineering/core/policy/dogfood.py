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


def _graph_references(graph: Graph) -> tuple[str, ...]:
    return tuple(sorted({reference for node in graph.nodes for reference in node.evidence_refs}))


def _record(reference: str, section: str, spec_path: Path, observed_at: datetime) -> EvidenceRecord:
    content = section.encode("utf-8")
    content_hash = f"sha256:{sha256(content).hexdigest()}"
    foundational_spec = reference.startswith("spec:")
    return EvidenceRecord(
        id=f"{reference}@{content_hash}",
        connector_type="foundational-spec" if foundational_spec else "foundational-markdown",
        external_object_id=reference,
        external_version=content_hash,
        author="founding-spec" if foundational_spec else "approved-design",
        observed_at=observed_at,
        source_locator=(
            f"{spec_path.as_posix()}#{reference.removeprefix('spec:')}"
            if foundational_spec
            else spec_path.as_posix()
        ),
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
    referenced_markdown: Mapping[str, Path] | None = None,
) -> Sequence[EvidenceRecord]:
    """Persist one immutable content-addressed row for every framework evidence ref."""
    sections = _numbered_sections(spec_path)
    documents = {
        reference: (_normalized_text(path), path)
        for reference, path in (referenced_markdown or {}).items()
        if not reference.startswith("spec:")
    }
    references = tuple(
        reference
        for reference in _graph_references(graph)
        if referenced_markdown is not None or reference.startswith("spec:")
    )
    missing = tuple(
        reference
        for reference in references
        if reference not in sections and reference not in documents
    )
    if missing:
        kind = "numbered section" if missing[0].startswith("spec:") else "foundational evidence"
        raise ValueError(f"missing {kind}: {missing[0]}")
    imported: list[EvidenceRecord] = []
    for reference in references:
        content, source = (
            (sections[reference], spec_path) if reference in sections else documents[reference]
        )
        candidate = _record(reference, content, source, observed_at)
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
