"""Fail-closed local-actor authorization for evidence-derived projections."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from intent_engineering.core.models import EvidenceRecord


def evidence_allowed(record: EvidenceRecord, actor: str) -> bool:
    """Allow public evidence and ACL evidence explicitly granted to the local actor."""
    return not record.acl or actor in record.acl


def refs_allowed(references: Sequence[str], records: Iterable[EvidenceRecord], actor: str) -> bool:
    """Fail closed: every nonempty reference must resolve and be readable."""
    index = {record.id: record for record in records}
    return all(
        reference in index and evidence_allowed(index[reference], actor) for reference in references
    )
