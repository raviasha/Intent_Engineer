"""Resolve semantic drift declarations against durable evidence and graph state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ConfigDict, Field, ValidationError

from intent_engineering.core.models import (
    DriftObservation,
    EvidenceRecord,
    EvidenceSide,
    Graph,
    SourceMode,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.reconcile.detectors import DetectionInput, detect_drift


class _SideDeclaration(StrictModel):
    """Semantic side fields plus untrusted references requiring durable resolution."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    evidence_refs: tuple[str, ...]
    confidence: float = Field(ge=0.0, le=1.0)
    source_mode: SourceMode = SourceMode.EXPLICIT
    current: bool = True
    # Legacy metadata is parsed only so it can be deliberately ignored and replaced.
    authors: tuple[str, ...] = ()
    observed_at: datetime | None = None


class _DetectionDeclaration(StrictModel):
    """Versioned semantic claims that intentionally contain no trusted provenance."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1]
    subject_ref: str
    affected_refs: tuple[str, ...]
    requirement: _SideDeclaration | None = None
    implementation: _SideDeclaration | None = None
    test: _SideDeclaration | None = None
    decision: _SideDeclaration | None = None
    requirement_version: int | None = None
    implementation_version: int | None = None
    test_version: int | None = None
    decision_version: int | None = None
    compatibility: Literal["aligns", "contradicts", "unknown"]
    requirement_active: bool = True
    has_mapped_semantics: bool = True
    material_code_change: bool = False


def _front_matter(content: str) -> Mapping[str, Any] | None:
    if not content.startswith("---\n"):
        return None
    closing = content.find("\n---\n", 4)
    if closing < 0:
        return None
    try:
        loaded = yaml.safe_load(content[4:closing])
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, Mapping):
        return None
    metadata = loaded.get("intent_engineering")
    return cast(Mapping[str, Any], metadata) if isinstance(metadata, Mapping) else None


def _raw_declaration(record: EvidenceRecord) -> object:
    raw = record.payload.get("detection_input")
    if raw is not None:
        return raw
    content = record.payload.get("content")
    metadata = _front_matter(content) if isinstance(content, str) else None
    return metadata.get("detection_input") if metadata is not None else None


def _declaration(record: EvidenceRecord) -> _DetectionDeclaration | None:
    raw = _raw_declaration(record)
    if not isinstance(raw, Mapping):
        return None
    payload = dict(cast(Mapping[str, Any], raw))
    # This is the sole explicit migration for the pre-versioned alpha fixture format.
    payload.setdefault("schema_version", 1)
    try:
        return _DetectionDeclaration.model_validate(payload)
    except ValidationError:
        return None


def _git_path_matches(record: EvidenceRecord, path: str) -> bool:
    if record.connector_type != "git":
        return False
    changed_paths = record.payload.get("changed_paths")
    return (
        isinstance(changed_paths, Sequence)
        and not isinstance(changed_paths, str)
        and path in changed_paths
    )


def _resolve_reference(
    reference: str,
    declaring_record: EvidenceRecord,
    records: Sequence[EvidenceRecord],
) -> EvidenceRecord | None:
    matches: tuple[EvidenceRecord, ...]
    if reference == "$self":
        matches = (declaring_record,)
    elif reference.startswith("source:"):
        connector, separator, external_id = reference.removeprefix("source:").partition(":")
        if not separator or not connector or not external_id:
            return None
        matches = tuple(
            record
            for record in records
            if record.connector_type == connector
            and record.external_object_id == external_id
        )
    elif reference.startswith("git-path:"):
        path = reference.removeprefix("git-path:")
        if not path:
            return None
        matches = tuple(record for record in records if _git_path_matches(record, path))
    else:
        matches = tuple(record for record in records if record.id == reference)
    return matches[0] if len(matches) == 1 else None


def _resolve_side(
    side: _SideDeclaration,
    declaring_record: EvidenceRecord,
    records: Sequence[EvidenceRecord],
    actor: str,
) -> EvidenceSide | None:
    if not side.evidence_refs or len(side.evidence_refs) != len(set(side.evidence_refs)):
        return None
    resolved: list[EvidenceRecord] = []
    for reference in side.evidence_refs:
        record = _resolve_reference(reference, declaring_record, records)
        if record is None:
            return None
        resolved.append(record)
    evidence_ids = tuple(sorted(record.id for record in resolved))
    if len(evidence_ids) != len(set(evidence_ids)) or not refs_allowed(
        evidence_ids,
        tuple(resolved),
        actor,
    ):
        return None
    authors = tuple(sorted({record.author for record in resolved if record.author}))
    if not authors:
        return None
    return EvidenceSide(
        label=side.label,
        claim=side.claim,
        evidence_refs=evidence_ids,
        observed_at=max(record.observed_at for record in resolved),
        authors=authors,
        confidence=side.confidence,
        source_mode=side.source_mode,
        current=side.current,
    )


def _input_from_record(
    declaring_record: EvidenceRecord,
    records: Sequence[EvidenceRecord],
    graph: Graph,
    actor: str,
) -> DetectionInput | None:
    declaration = _declaration(declaring_record)
    if declaration is None or not refs_allowed(
        (declaring_record.id,),
        (declaring_record,),
        actor,
    ):
        return None
    graph_ids = {node.id for node in graph.nodes}
    if declaration.subject_ref not in graph_ids or any(
        reference not in graph_ids for reference in declaration.affected_refs
    ):
        return None
    resolved_sides: dict[str, EvidenceSide | None] = {}
    side_evidence: list[set[str]] = []
    for name in ("requirement", "implementation", "test", "decision"):
        declared_side = getattr(declaration, name)
        if declared_side is None:
            resolved_sides[name] = None
            continue
        resolved = _resolve_side(declared_side, declaring_record, records, actor)
        if resolved is None:
            return None
        current_refs = set(resolved.evidence_refs)
        if any(current_refs & previous for previous in side_evidence):
            return None
        side_evidence.append(current_refs)
        resolved_sides[name] = resolved
    payload = declaration.model_dump(
        exclude={"schema_version", "requirement", "implementation", "test", "decision"}
    )
    payload.update(resolved_sides)
    try:
        return DetectionInput.model_validate(payload)
    except ValidationError:
        return None


_DETECTOR_ORDER = {
    "conflicting_sources": 0,
    "requirement_lag": 1,
    "code_lag": 2,
    "test_lag": 3,
    "undocumented_code": 4,
    "ambiguous_divergence": 5,
}


def detect_evidence_drift(
    records: Sequence[EvidenceRecord],
    graph: Graph,
    actor: str,
) -> tuple[DriftObservation, ...]:
    """Detect cases from a combined authorized run without trusting packet provenance."""
    by_id: dict[str, EvidenceRecord] = {}
    for record in records:
        previous = by_id.get(record.id)
        if previous is not None and previous != record:
            return ()
        by_id[record.id] = record
    stable_records = tuple(sorted(by_id.values(), key=lambda item: item.id))
    candidates: list[DriftObservation] = []
    for record in stable_records:
        detection_input = _input_from_record(record, stable_records, graph, actor)
        if detection_input is not None:
            candidates.extend(detect_drift(detection_input))
    selected: dict[str, DriftObservation] = {}
    for observation in sorted(
        candidates,
        key=lambda item: (_DETECTOR_ORDER[item.detector_id], item.fingerprint),
    ):
        selected.setdefault(observation.subject_ref, observation)
    return tuple(sorted(selected.values(), key=lambda item: item.fingerprint))
