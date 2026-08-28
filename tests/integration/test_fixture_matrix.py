"""End-to-end deterministic fixture matrix for the local reconciliation loop."""

import pytest

from intent_engineering.sync.models import SyncRunStatus
from tests.helpers.fixtures import (
    FIXTURES,
    run_fixture,
    run_fixture_twice,
    run_fixture_with_sources,
)


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("aligned_project", set()),
        ("code_lag", {"CODE_LAG"}),
        ("requirement_lag", {"REQUIREMENT_LAG"}),
        ("test_lag", {"TEST_LAG"}),
        ("undocumented_code", {"UNDOCUMENTED_CODE"}),
        ("ambiguous_divergence", {"AMBIGUOUS_DIVERGENCE"}),
        ("cross_author_conflict", {"CONFLICTING_SOURCES"}),
    ],
)
def test_fixture_classification(fixture_name: str, expected: set[str]) -> None:
    """Each fixture exercises exactly one intentional classification outcome."""
    result = run_fixture(FIXTURES / fixture_name)

    assert result.sync.status is SyncRunStatus.SUCCESS
    assert result.sync.connectors["markdown"].status is SyncRunStatus.SUCCESS
    assert result.sync.connectors["git"].status is SyncRunStatus.SUCCESS
    assert {record.connector_type for record in result.evidence} == {"markdown", "git"}
    assert {item.case_type.value for item in result.cases} == expected
    evidence_by_id = {record.id: record for record in result.evidence}
    for case in result.cases:
        assert case.subject_ref in {node.id for node in result.graph.nodes}
        assert set(case.affected_refs) <= {node.id for node in result.graph.nodes}
        referenced = {
            reference for side in case.evidence_sides for reference in side.evidence_refs
        }
        assert referenced <= set(evidence_by_id)
        assert "git" in {evidence_by_id[reference].connector_type for reference in referenced}
        if len(case.evidence_sides) > 1:
            assert len({evidence_by_id[reference].external_version for reference in referenced}) > 1
    if fixture_name == "cross_author_conflict":
        authors = {author for side in result.cases[0].evidence_sides for author in side.authors}
        assert authors == {"local", "Decision Author <decision@example.test>"}


@pytest.mark.parametrize(
    "fixture_name",
    [
        "code_lag",
        "requirement_lag",
        "test_lag",
        "undocumented_code",
        "ambiguous_divergence",
        "cross_author_conflict",
    ],
)
def test_fixture_classification_fails_closed_without_git_causal_evidence(
    fixture_name: str,
) -> None:
    """Removing commits/code/tests makes every Git-dependent declaration unresolved."""
    result = run_fixture_with_sources(FIXTURES / fixture_name, "markdown")

    assert result.sync.status is SyncRunStatus.SUCCESS
    assert result.cases == ()


def test_second_sync_is_a_zero_mutation_noop() -> None:
    """A committed fixture manifest does not recreate evidence, graph changes, or cases."""
    first, second, cases = run_fixture_twice(FIXTURES / "idempotent_sync")

    assert first.status.value == "success"
    assert second.status.value == "success"
    assert (second.evidence_added, second.changes_applied, second.cases_created) == (0, 0, 0)
    assert {case.case_type.value for case in cases} == {"CODE_LAG"}
