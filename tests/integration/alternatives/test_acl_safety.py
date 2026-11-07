"""Alternative grounding must not disclose filtered graph or evidence content."""

from intent_engineering.alternatives.grounding import AlternativeRequestBuilder
from intent_engineering.alternatives.models import AlternativeMode
from tests.unit.alternatives.fixtures import assessment_pair


def test_grounding_never_projects_unrelated_or_private_payload_content() -> None:
    snapshot, report = assessment_pair(include_hidden=True)

    request = AlternativeRequestBuilder().build(
        snapshot=snapshot,
        report=report,
        subject_ref="req:export",
        requested_modes=(AlternativeMode.SIMPLIFY,),
    )
    encoded = request.model_dump_json()

    assert "req:hidden" not in encoded
    assert "TOP-SECRET-HIDDEN" not in encoded
    assert "PRIVATE-SENTINEL" not in encoded
    assert "/private/" not in encoded
