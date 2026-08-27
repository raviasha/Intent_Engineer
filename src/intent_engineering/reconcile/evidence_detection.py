"""Resolve semantic drift declarations against durable evidence and graph state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ConfigDict, Field, ValidationError

from intent_engineering.core.models import (
    DriftObservation,
    EvidenceIngestion,
    EvidenceRecord,
    EvidenceSide,
    Graph,
    SourceMode,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.reconcile.detectors import (
    DetectionInput,
    EvidenceOrder,
    detect_drift,
)


class _SideDeclaration(StrictModel):
    """Semantic side fields plus untrusted references requiring durable resolution."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    evidence_refs: tuple[str, ...]
    confidence: float = Field(ge=0.0, le=1.0)
    source_mode: SourceMode = SourceMode.EXPLICIT
    current: bool | None = None
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


@dataclass(frozen=True)
class _ResolvedSide:
    """A public evidence side plus the immutable records used to derive it."""

    side: EvidenceSide
    records: tuple[EvidenceRecord, ...]


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
    ingestions: Sequence[EvidenceIngestion],
    actor: str,
) -> _ResolvedSide | None:
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
    current = _records_are_current(tuple(resolved), ingestions)
    if side.current is not None and side.current is not current:
        return None
    return _ResolvedSide(
        side=EvidenceSide(
            label=side.label,
            claim=side.claim,
            evidence_refs=evidence_ids,
            observed_at=max(record.observed_at for record in resolved),
            authors=authors,
            confidence=side.confidence,
            source_mode=side.source_mode,
            current=current,
        ),
        records=tuple(resolved),
    )


def _records_are_current(
    records: tuple[EvidenceRecord, ...],
    ingestions: Sequence[EvidenceIngestion],
) -> bool:
    for record in records:
        associations = tuple(item for item in ingestions if item.evidence.id == record.id)
        if not associations:
            return False
        for association in associations:
            if any(
                item.connector_id == association.connector_id
                and item.evidence.connector_type == record.connector_type
                and item.evidence.external_object_id == record.external_object_id
                and item.sequence > association.sequence
                for item in ingestions
            ):
                return False
    return True


def _record_order(
    left: EvidenceRecord,
    right: EvidenceRecord,
    ingestions: Sequence[EvidenceIngestion],
) -> EvidenceOrder:
    if (
        left.connector_type == right.connector_type
        and left.external_object_id == right.external_object_id
    ):
        left_by_connector = {
            item.connector_id: item.sequence for item in ingestions if item.evidence.id == left.id
        }
        right_by_connector = {
            item.connector_id: item.sequence for item in ingestions if item.evidence.id == right.id
        }
        shared = set(left_by_connector) & set(right_by_connector)
        orders = {
            EvidenceOrder.BEFORE
            if left_by_connector[connector_id] < right_by_connector[connector_id]
            else EvidenceOrder.AFTER
            if left_by_connector[connector_id] > right_by_connector[connector_id]
            else EvidenceOrder.TIED
            for connector_id in shared
        }
        return next(iter(orders)) if len(orders) == 1 else EvidenceOrder.UNKNOWN
    if left.observed_at < right.observed_at:
        return EvidenceOrder.BEFORE
    if left.observed_at > right.observed_at:
        return EvidenceOrder.AFTER
    return EvidenceOrder.TIED


def _side_order(
    left: _ResolvedSide,
    right: _ResolvedSide,
    ingestions: Sequence[EvidenceIngestion],
) -> EvidenceOrder:
    orders = {
        _record_order(left_record, right_record, ingestions)
        for left_record in left.records
        for right_record in right.records
    }
    return next(iter(orders)) if len(orders) == 1 else EvidenceOrder.UNKNOWN


def _declared_order(left: int, right: int) -> EvidenceOrder:
    if left < right:
        return EvidenceOrder.BEFORE
    if left > right:
        return EvidenceOrder.AFTER
    return EvidenceOrder.TIED


def _input_from_record(
    declaring_record: EvidenceRecord,
    records: Sequence[EvidenceRecord],
    ingestions: Sequence[EvidenceIngestion],
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
    resolved_sides: dict[str, _ResolvedSide | None] = {}
    side_evidence: list[set[str]] = []
    for name in ("requirement", "implementation", "test", "decision"):
        declared_side = getattr(declaration, name)
        if declared_side is None:
            resolved_sides[name] = None
            continue
        resolved = _resolve_side(declared_side, declaring_record, records, ingestions, actor)
        if resolved is None:
            return None
        current_refs = set(resolved.side.evidence_refs)
        if any(current_refs & previous for previous in side_evidence):
            return None
        side_evidence.append(current_refs)
        resolved_sides[name] = resolved
    comparisons = {
        "requirement_implementation_order": ("requirement", "implementation"),
        "decision_requirement_order": ("decision", "requirement"),
        "implementation_decision_order": ("implementation", "decision"),
        "test_decision_order": ("test", "decision"),
        "implementation_test_order": ("implementation", "test"),
    }
    chronology: dict[str, EvidenceOrder] = {}
    for field, (left_name, right_name) in comparisons.items():
        left = resolved_sides[left_name]
        right = resolved_sides[right_name]
        if left is None or right is None:
            continue
        derived = _side_order(left, right, ingestions)
        left_version = getattr(declaration, f"{left_name}_version")
        right_version = getattr(declaration, f"{right_name}_version")
        if (
            left_version is not None
            and right_version is not None
            and _declared_order(left_version, right_version) is not derived
        ):
            return None
        chronology[field] = derived
    payload = declaration.model_dump(
        exclude={
            "schema_version",
            "requirement",
            "implementation",
            "test",
            "decision",
            "requirement_version",
            "implementation_version",
            "test_version",
            "decision_version",
        }
    )
    payload.update(
        {
            name: resolved.side if resolved is not None else None
            for name, resolved in resolved_sides.items()
        }
    )
    payload.update(chronology)
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
    "intent_without_requirement": 5,
    "requirement_without_intent": 6,
    "relevant_provisional_intent": 7,
    "stale_source_evidence": 8,
    "ambiguous_divergence": 9,
}


def select_drift_observations(
    candidates: Sequence[DriftObservation],
) -> tuple[DriftObservation, ...]:
    """Apply the established precedence and stable one-case-per-subject rule."""
    selected: dict[str, DriftObservation] = {}
    for observation in sorted(
        candidates,
        key=lambda item: (_DETECTOR_ORDER.get(item.detector_id, 100), item.fingerprint),
    ):
        selected.setdefault(observation.subject_ref, observation)
    return tuple(sorted(selected.values(), key=lambda item: item.fingerprint))


def detect_evidence_drift(
    records: Sequence[EvidenceRecord],
    graph: Graph,
    actor: str,
    ingestions: Sequence[EvidenceIngestion] = (),
) -> tuple[DriftObservation, ...]:
    """Detect cases from a combined authorized run without trusting packet provenance."""
    by_id: dict[str, EvidenceRecord] = {}
    for record in records:
        previous = by_id.get(record.id)
        if previous is not None and previous != record:
            return ()
        by_id[record.id] = record
    stable_records = tuple(by_id.values())
    candidates: list[DriftObservation] = []
    for record in sorted(stable_records, key=lambda item: item.id):
        detection_input = _input_from_record(
            record,
            stable_records,
            ingestions,
            graph,
            actor,
        )
        if detection_input is not None:
            candidates.extend(detect_drift(detection_input))
    return select_drift_observations(candidates)
