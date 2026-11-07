"""Detached alternative overlays never mutate approved graph state."""

import pytest

from intent_engineering.alternatives.grounding import AlternativeRequestBuilder
from intent_engineering.alternatives.models import AlternativeMode
from intent_engineering.alternatives.overlay import (
    AlternativeProjectionError,
    apply_alternative_overlay,
)
from intent_engineering.alternatives.validation import AlternativeValidator
from tests.unit.alternatives.fixtures import assessment_pair
from tests.unit.alternatives.test_validation import _candidate


def test_overlay_updates_only_the_detached_requirement() -> None:
    snapshot, report = assessment_pair()
    request = AlternativeRequestBuilder().build(
        snapshot=snapshot,
        report=report,
        subject_ref="req:export",
        requested_modes=(AlternativeMode.CHANGE_APPROACH,),
    )
    alternative = AlternativeValidator().validate(request, _candidate())
    approved_bytes = snapshot.graph.model_dump_json()

    projected = apply_alternative_overlay(snapshot.graph, alternative)

    projected_requirement = next(node for node in projected.nodes if node.id == "req:export")
    projected_constraint = next(node for node in projected.nodes if node.id == "constraint:weekly")
    assert projected_requirement.label == "An owner exports the report on request."
    assert projected_constraint.label == "Make the report available on request."
    assert projected_requirement.evidence_refs == ("evidence:visible",)
    assert snapshot.graph.model_dump_json() == approved_bytes
    assert projected.id == snapshot.graph.id
    assert projected.version == snapshot.graph.version


def test_overlay_rejects_a_tampered_effect_digest_without_partial_output() -> None:
    snapshot, report = assessment_pair()
    request = AlternativeRequestBuilder().build(
        snapshot=snapshot,
        report=report,
        subject_ref="req:export",
        requested_modes=(AlternativeMode.CHANGE_APPROACH,),
    )
    alternative = AlternativeValidator().validate(request, _candidate())
    tampered = alternative.model_copy(update={"graph_effect_digest": "sha256:" + "f" * 64})

    with pytest.raises(AlternativeProjectionError, match="alternative projection unavailable"):
        apply_alternative_overlay(snapshot.graph, tampered)
