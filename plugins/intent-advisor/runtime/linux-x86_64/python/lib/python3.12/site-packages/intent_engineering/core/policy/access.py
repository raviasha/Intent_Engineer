"""Fail-closed local-actor authorization for evidence-derived projections."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence

from intent_engineering.core.models import EvidenceRecord


def _principals(actor: str | Collection[str]) -> frozenset[str]:
    if type(actor) is str:
        return frozenset({actor})
    if any(type(item) is not str or not item for item in actor):
        return frozenset()
    return frozenset(actor)


def evidence_allowed(record: EvidenceRecord, actor: str | Collection[str]) -> bool:
    """Allow public evidence or ACL evidence granted to an authenticated actor alias."""
    return not record.acl or not frozenset(record.acl).isdisjoint(_principals(actor))


def refs_allowed(
    references: Sequence[str],
    records: Iterable[EvidenceRecord],
    actor: str | Collection[str],
) -> bool:
    """Fail closed: every nonempty reference must resolve and be readable."""
    index = {record.id: record for record in records}
    return all(
        reference in index and evidence_allowed(index[reference], actor) for reference in references
    )
