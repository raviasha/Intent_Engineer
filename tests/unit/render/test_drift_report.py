"""Deterministic, injection-safe Markdown drift reports."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations

import pytest

from intent_engineering.core.models import (
    EvidenceSide,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    ResolutionAction,
)
from intent_engineering.render.drift_report import (
    EMPTY_DRIFT_REPORT,
    recommended_action,
    render_drift_report,
)

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def _case(
    case_type: ReconciliationCaseType,
    case_id: str,
    *,
    status: ReconciliationStatus = ReconciliationStatus.OPEN,
    label: str = "requirement",
    claim: str = "expected behavior",
    authors: tuple[str, ...] = ("product", "engineer", "product"),
    evidence_refs: tuple[str, ...] = ("evidence:z", "evidence:a", "evidence:z"),
    affected_refs: tuple[str, ...] = ("node:z", "node:a", "node:z"),
    impact: str = "Review behavior",
) -> ReconciliationCase:
    case = ReconciliationCase(
        id=case_id,
        subject_ref="requirement:export",
        case_type=case_type,
        affected_refs=affected_refs,
        evidence_sides=(
            EvidenceSide(
                label=label,
                claim=claim,
                evidence_refs=evidence_refs,
                observed_at=NOW,
                authors=authors,
                confidence=0.875,
            ),
        ),
        detector_id="fixture",
        fingerprint=(case_id[-1].encode().hex()[0] if case_id else "a") * 64,
        created_at=NOW,
        created_by="detector:fixture",
        impact=impact,
    )
    return (
        case if status is ReconciliationStatus.OPEN else case.model_copy(update={"status": status})
    )


def test_report_is_permutation_invariant_and_sorted_by_type_then_stable_id() -> None:
    cases = (
        _case(ReconciliationCaseType.TEST_LAG, "case:z"),
        _case(ReconciliationCaseType.CODE_LAG, "case:b"),
        _case(ReconciliationCaseType.CODE_LAG, "case:a"),
    )

    reports = {render_drift_report(order) for order in permutations(cases)}

    assert len(reports) == 1
    report = reports.pop()
    assert report.index("case:a") < report.index("case:b") < report.index("case:z")
    assert report.endswith("\n") and not report.endswith("\n\n")


def test_report_has_one_stable_empty_form_and_excludes_every_terminal_status() -> None:
    terminal = tuple(
        _case(ReconciliationCaseType.CODE_LAG, f"case:{status.value}", status=status)
        for status in (
            ReconciliationStatus.RESOLVED,
            ReconciliationStatus.DEFERRED,
            ReconciliationStatus.FALSE_POSITIVE,
        )
    )

    assert render_drift_report(()) == EMPTY_DRIFT_REPORT
    assert render_drift_report(terminal) == EMPTY_DRIFT_REPORT
    assert EMPTY_DRIFT_REPORT == (
        "# Intent Engineering drift report\n\n_No authorized open reconciliation cases._\n"
    )


def test_report_deduplicates_and_sorts_refs_and_authors() -> None:
    report = render_drift_report((_case(ReconciliationCaseType.CODE_LAG, "case:a"),))

    assert report.count("`node:a`") == 1
    assert report.count("`node:z`") == 1
    assert report.count("`evidence:a`") == 1
    assert report.count("`evidence:z`") == 1
    assert report.count("`product`") == 1
    assert report.index("`node:a`") < report.index("`node:z`")
    assert report.index("`evidence:a`") < report.index("`evidence:z`")
    assert report.index("`engineer`") < report.index("`product`")


def test_every_case_type_has_one_explicit_recommended_action() -> None:
    expected = {
        ReconciliationCaseType.CODE_LAG: ResolutionAction.UPDATE_IMPLEMENTATION,
        ReconciliationCaseType.REQUIREMENT_LAG: ResolutionAction.UPDATE_REQUIREMENT,
        ReconciliationCaseType.INTENT_LAG: ResolutionAction.UPDATE_INTENT,
        ReconciliationCaseType.TEST_LAG: ResolutionAction.UPDATE_TESTS,
        ReconciliationCaseType.DOC_LAG: ResolutionAction.UPDATE_DOCUMENTATION,
        ReconciliationCaseType.UNDOCUMENTED_CODE: ResolutionAction.BACKFILL_DESIGN_DECISION,
        ReconciliationCaseType.ORPHAN_REQUIREMENT: ResolutionAction.UPDATE_IMPLEMENTATION,
        ReconciliationCaseType.CONFLICTING_SOURCES: ResolutionAction.PRESERVE_DISAGREEMENT,
        ReconciliationCaseType.AMBIGUOUS_DIVERGENCE: ResolutionAction.PRESERVE_DISAGREEMENT,
        ReconciliationCaseType.POSSIBLE_INTENT_CHANGE: ResolutionAction.UPDATE_INTENT,
    }

    assert set(expected) == set(ReconciliationCaseType)
    assert {
        case_type: recommended_action(case_type) for case_type in ReconciliationCaseType
    } == expected


@pytest.mark.parametrize(
    "malicious",
    [
        "line\n# injected heading\r\n```code```",
        "[click](https://evil.example) ![image](https://evil.example/x)",
        "<script>alert(1)</script><table><tr><td>x</td></tr></table>",
        "column | injected | table\x00\u2028next",
        "/Users/private/project/secret.txt",
        "ghp_report-secret-token",
        "Bearer authorization-secret",
    ],
)
def test_every_evidence_derived_scalar_is_inert_and_sensitive_patterns_are_redacted(
    malicious: str,
) -> None:
    case = _case(
        ReconciliationCaseType.CODE_LAG,
        malicious,
        label=malicious,
        claim=malicious,
        authors=(malicious,),
        evidence_refs=(malicious,),
        affected_refs=(malicious,),
        impact=malicious,
    ).model_copy(update={"subject_ref": malicious})

    report = render_drift_report((case,))

    assert "\n# injected heading" not in report
    assert "```" not in report
    assert "](https://evil.example)" not in report
    assert "<script>" not in report
    assert "<table>" not in report
    assert "\x00" not in report and "\u2028" not in report
    assert "/Users/private" not in report
    assert "ghp_report-secret-token" not in report
    assert "Bearer authorization-secret" not in report


@pytest.mark.parametrize(
    "local_path",
    [
        "/etc/intent-secret",
        "/opt/private/config",
        "file:///etc/shadow",
        "/Volumes/private/report.md",
        "/usr/local/share/intent",
        "/srv/intent/private",
        "/",
    ],
)
def test_report_redacts_any_absolute_local_path(local_path: str) -> None:
    case = _case(
        ReconciliationCaseType.CODE_LAG,
        "case:absolute-path",
        claim=f"provider returned {local_path}",
    )

    report = render_drift_report((case,))

    assert local_path not in report
    assert "[redacted-local-path]" in report


def test_report_preserves_a_nonlocal_https_provenance_url() -> None:
    url = "https://github.example/acme/demo/issues/17"
    case = _case(
        ReconciliationCaseType.CODE_LAG,
        "case:provider-url",
        claim=f"provider source {url}",
    )

    report = render_drift_report((case,))

    assert url in report
    assert "[redacted-local-path]" not in report
