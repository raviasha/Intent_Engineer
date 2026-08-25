"""End-to-end deterministic fixture matrix for the local reconciliation loop."""

import pytest

from tests.helpers.fixtures import FIXTURES, run_fixture, run_fixture_twice


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

    assert {item.case_type.value for item in result.cases} == expected


def test_second_sync_is_a_zero_mutation_noop() -> None:
    """A committed fixture manifest does not recreate evidence, graph changes, or cases."""
    first, second, cases = run_fixture_twice(FIXTURES / "idempotent_sync")

    assert first.status.value == "success"
    assert second.status.value == "success"
    assert (second.evidence_added, second.changes_applied, second.cases_created) == (0, 0, 0)
    assert {case.case_type.value for case in cases} == {"CODE_LAG"}
