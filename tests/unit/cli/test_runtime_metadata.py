"""Direct production metadata-validator coverage."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from intent_engineering.cli.runtime import _detect_cases, _FrontMatterReasoner
from intent_engineering.core.models import EvidenceDelta, EvidenceRecord, Graph


def _record(payload: dict[str, object], acl: tuple[str, ...] = ()) -> EvidenceRecord:
    return EvidenceRecord(
        id="evidence:one",
        connector_type="markdown",
        external_object_id="one",
        external_version="1",
        author="a",
        observed_at=datetime(2026, 8, 25, tzinfo=UTC),
        source_locator="one.md",
        content_hash="sha256:one",
        payload=payload,
        acl=acl,
    )  # type: ignore[arg-type]


def _assertion(ref: str) -> dict[str, object]:
    return {
        "id": "a:one",
        "subject_id": "requirement:one",
        "change_kind": "initialize",
        "node_type": "requirement",
        "label": "one",
        "source_mode": "explicit",
        "evidence_refs": [ref],
        "confidence": 0.9,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"intent_assertion": _assertion("$self")},
        {"intent_assertion": _assertion("evidence:one")},
        {
            "content": "---\nintent_engineering:\n  intent_assertion:\n    id: a:one\n    subject_id: requirement:one\n    change_kind: initialize\n    node_type: requirement\n    label: one\n    source_mode: explicit\n    evidence_refs: ['$self']\n    confidence: 0.9\n---\n"
        },
        {
            "content": "---\nintent_engineering:\n  intent_assertion:\n    id: a:one\n    subject_id: requirement:one\n    change_kind: initialize\n    node_type: requirement\n    label: one\n    source_mode: explicit\n    evidence_refs: ['evidence:one']\n    confidence: 0.9\n---\n"
        },
    ],
)
def test_current_authorized_assertion_forms_project(payload: dict[str, object]) -> None:
    delta = EvidenceDelta(added=(_record(payload),), prior_versions={})
    assert len(_FrontMatterReasoner(actor="local").extract_assertions(delta)) == 1


@pytest.mark.parametrize(
    "payload,acl",
    [
        ({"intent_assertion": _assertion("missing")}, ()),
        ({"intent_assertion": _assertion("$self")}, ("other",)),
        ({"intent_assertion": {**_assertion("$self"), "evidence_refs": []}}, ()),
        ({"intent_assertion": {**_assertion("$self"), "evidence_refs": ["$self", "$self"]}}, ()),
        ({"intent_assertion": {**_assertion("$self"), "evidence_refs": [["$self"]]}}, ()),
        ({"intent_assertion": "not-a-mapping"}, ()),
        ({"content": "---\nintent_engineering: [\n---\n"}, ()),
    ],
)
def test_invalid_or_unreadable_assertions_do_not_project(
    payload: dict[str, object], acl: tuple[str, ...]
) -> None:
    delta = EvidenceDelta(added=(_record(payload, acl),), prior_versions={})
    assert _FrontMatterReasoner(actor="local").extract_assertions(delta) == ()


def test_top_level_assertion_takes_precedence_over_front_matter() -> None:
    """A malformed front-matter assertion cannot override the valid explicit top-level form."""
    payload = {
        "intent_assertion": _assertion("$self"),
        "content": "---\nintent_engineering:\n  intent_assertion: not-a-mapping\n---\n",
    }

    assertions = _FrontMatterReasoner(actor="local").extract_assertions(
        EvidenceDelta(added=(_record(payload),), prior_versions={})
    )

    assert len(assertions) == 1


def test_detector_rejects_unreadable_and_noncurrent_references() -> None:
    payload = {
        "detection_input": {
            "subject_ref": "r",
            "affected_refs": ["r"],
            "compatibility": "aligns",
            "requirement_version": 2,
            "implementation_version": 1,
            "requirement": {
                "label": "r",
                "claim": "r",
                "evidence_refs": ["missing"],
                "observed_at": "2026-08-25T00:00:00Z",
                "authors": ["a"],
                "confidence": 0.9,
            },
            "implementation": {
                "label": "i",
                "claim": "i",
                "evidence_refs": ["missing"],
                "observed_at": "2026-08-25T00:00:00Z",
                "authors": ["a"],
                "confidence": 0.9,
            },
        }
    }
    graph = Graph(id="g", version=0, nodes=(), edges=())
    assert (
        _detect_cases(
            EvidenceDelta(added=(_record(payload, ("other",)),), prior_versions={}), graph, "local"
        )
        == ()
    )
    assert (
        _detect_cases(EvidenceDelta(added=(_record(payload),), prior_versions={}), graph, "local")
        == ()
    )


def test_detector_accepts_current_self_but_rejects_acl_and_noncurrent_packets() -> None:
    """The detector only consumes the current authorized evidence record, never a stale reference."""
    payload = {
        "detection_input": {
            "subject_ref": "r",
            "affected_refs": ["r"],
            "compatibility": "aligns",
            "requirement_version": 2,
            "implementation_version": 1,
            "requirement": _side_payload("$self"),
            "implementation": _side_payload("$self"),
        }
    }
    graph = Graph(id="g", version=0, nodes=(), edges=())
    current = _record(payload)
    assert (
        len(_detect_cases(EvidenceDelta(added=(current,), prior_versions={}), graph, "local")) == 1
    )
    assert (
        _detect_cases(
            EvidenceDelta(added=(_record(payload, ("other",)),), prior_versions={}), graph, "local"
        )
        == ()
    )
    stale = {
        "detection_input": {**payload["detection_input"], "requirement": _side_payload("missing")}
    }
    assert (
        _detect_cases(EvidenceDelta(added=(_record(stale),), prior_versions={}), graph, "local")
        == ()
    )


def _side_payload(reference: str) -> dict[str, object]:
    return {
        "label": "side",
        "claim": "claim",
        "evidence_refs": [reference],
        "observed_at": "2026-08-25T00:00:00Z",
        "authors": ["a"],
        "confidence": 0.9,
    }
