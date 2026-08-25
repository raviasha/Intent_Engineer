"""Tests for deterministic evidence-backed drift classification."""

from intent_engineering.core.models import ReconciliationCaseType
from intent_engineering.reconcile.detectors import detect_drift
from tests.unit.reconcile import builders


def test_newer_requirement_without_new_code_is_code_lag() -> None:
    result = detect_drift(builders.code_lag_input())

    assert [item.case_type for item in result] == [ReconciliationCaseType.CODE_LAG]


def test_newer_decision_and_code_against_old_requirement_is_requirement_lag() -> None:
    result = detect_drift(builders.requirement_lag_input())

    assert [item.case_type for item in result] == [ReconciliationCaseType.REQUIREMENT_LAG]


def test_implementation_after_last_verifying_test_is_test_lag() -> None:
    result = detect_drift(builders.test_lag_input())

    assert [item.case_type for item in result] == [ReconciliationCaseType.TEST_LAG]


def test_material_unmapped_code_is_undocumented_code() -> None:
    result = detect_drift(builders.undocumented_code_input())

    assert [item.case_type for item in result] == [ReconciliationCaseType.UNDOCUMENTED_CODE]


def test_insufficient_evidence_is_ambiguous() -> None:
    result = detect_drift(builders.ambiguous_input())

    assert [item.case_type for item in result] == [ReconciliationCaseType.AMBIGUOUS_DIVERGENCE]


def test_cross_author_incompatibility_preserves_both_positions() -> None:
    result = detect_drift(builders.cross_author_conflict_input())

    assert [item.case_type for item in result] == [ReconciliationCaseType.CONFLICTING_SOURCES]
    assert len(result[0].evidence_sides) == 2
    assert {side.authors for side in result[0].evidence_sides} == {
        ("product@example.com",),
        ("architecture@example.com",),
    }
    assert result[0].requires_human is True


def test_detector_precedence_retains_only_the_first_classification_for_a_subject() -> None:
    result = detect_drift(builders.precedence_input())

    assert [item.case_type for item in result] == [ReconciliationCaseType.CONFLICTING_SOURCES]


def test_fingerprint_is_stable_over_equivalent_input() -> None:
    first = detect_drift(builders.code_lag_input())[0]
    second = detect_drift(builders.code_lag_input())[0]

    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64
