"""Deterministic authorization-ready Markdown reconciliation reporting."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from unicodedata import category

from intent_engineering.core.models import (
    EvidenceSide,
    ReconciliationCase,
    ReconciliationCaseType,
    ResolutionAction,
    is_nonterminal_case_status,
)

EMPTY_DRIFT_REPORT = (
    "# Intent Engineering drift report\n\n_No authorized open reconciliation cases._\n"
)

_RECOMMENDED_ACTIONS: dict[ReconciliationCaseType, ResolutionAction] = {
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

if set(_RECOMMENDED_ACTIONS) != set(ReconciliationCaseType):  # pragma: no cover - import guard
    raise RuntimeError("drift report action mapping is incomplete")

_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?:github_pat_|gh[pousr]_[A-Za-z0-9_-]*|bearer\s+|authorization\s*[:=])"
)
_LOCAL_PATH_PATTERN = re.compile(
    r"(?i:file:///)\S*|(?<![A-Za-z0-9:/])/(?!/)\S*|"
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?![\\/])\S+"
)
_MARKDOWN_META = frozenset(r"\\*{}[]()#+!|")


def _safe_scalar(value: str) -> str:
    """Make evidence-derived text one inert Markdown scalar or redact it entirely."""
    normalized = "".join(
        " " if character in "\r\n\u2028\u2029" or category(character).startswith("C") else character
        for character in value
    )
    if _CREDENTIAL_PATTERN.search(normalized):
        return "[redacted-sensitive]"
    if _LOCAL_PATH_PATTERN.search(normalized):
        return "[redacted-local-path]"
    escaped = html.escape(normalized, quote=True).replace("`", "&#96;")
    return "".join(
        f"\\{character}" if character in _MARKDOWN_META else character for character in escaped
    )


def _safe_values(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({_safe_scalar(value) for value in values}))


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def recommended_action(case_type: ReconciliationCaseType) -> ResolutionAction:
    """Return the exhaustive deterministic recommendation for one known case type."""
    try:
        return _RECOMMENDED_ACTIONS[case_type]
    except KeyError:
        raise ValueError("unsupported reconciliation case type") from None


def _side_sort_key(side: EvidenceSide) -> tuple[object, ...]:
    return (
        side.label,
        side.claim,
        side.observed_at,
        tuple(sorted(set(side.authors))),
        tuple(sorted(set(side.evidence_refs))),
    )


def _render_side(side: EvidenceSide) -> list[str]:
    authors = _safe_values(side.authors)
    evidence_refs = _safe_values(side.evidence_refs)
    return [
        f"### Evidence: {_safe_scalar(side.label)}",
        f"- Claim: {_safe_scalar(side.claim)}",
        "- Authors: " + ", ".join(f"`{author}`" for author in authors),
        f"- Observed: `{_timestamp(side.observed_at)}`",
        f"- Confidence: `{format(side.confidence, '.6g')}`",
        "- Evidence refs: " + ", ".join(f"`{reference}`" for reference in evidence_refs),
    ]


def _render_case(case: ReconciliationCase) -> str:
    affected_refs = _safe_values(case.affected_refs)
    lines = [
        f"## {_safe_scalar(case.case_type.value)} — `{_safe_scalar(case.id)}`",
        f"- Subject: {_safe_scalar(case.subject_ref)}",
        f"- Status: `{_safe_scalar(case.status.value)}`",
        f"- Recommended action: `{recommended_action(case.case_type).value}`",
        "- Affected refs: "
        + (", ".join(f"`{reference}`" for reference in affected_refs) if affected_refs else "none"),
    ]
    if case.impact:
        lines.append(f"- Impact: {_safe_scalar(case.impact)}")
    for side in sorted(case.evidence_sides, key=_side_sort_key):
        lines.extend(("", *_render_side(side)))
    return "\n".join(lines)


def render_drift_report(cases: Sequence[ReconciliationCase]) -> str:
    """Render already-authorized nonterminal cases in deterministic Markdown."""
    ordered = sorted(
        (case for case in cases if is_nonterminal_case_status(case.status)),
        key=lambda case: (case.case_type.value, case.id),
    )
    if not ordered:
        return EMPTY_DRIFT_REPORT
    sections = ["# Intent Engineering drift report", *(_render_case(case) for case in ordered)]
    return "\n\n".join(sections) + "\n"
