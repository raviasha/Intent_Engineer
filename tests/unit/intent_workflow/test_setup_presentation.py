"""Setup delivery is a public presentation contract, separate from worker policy."""

from copy import deepcopy

import pytest

from intent_engineering.intent_workflow.setup_presentation import with_setup_chat_update


def assessment(status="running"):
    return {
        "run_id": "run:1",
        "status": status,
        "notify_user": False,
        "scope": "Storage module",
        "completed_comparisons": 1,
        "missing_comparisons": [{"axis": "requirements_code"}],
        "comparisons": [{"status": "aligned"}],
        "findings": [],
        "limitations": ["Selected scope only"],
    }


def test_progress_has_actual_counts_and_stable_retry_id_without_mutation():
    raw = assessment()
    before = deepcopy(raw)
    result = with_setup_chat_update(raw)
    update = result["chat_update"]
    assert update["kind"] == "progress"
    assert update["reviewed"] == 1 and update["total"] == 2
    assert result["notify_user"] is False
    assert with_setup_chat_update(raw)["chat_update"] == update
    assert raw == before
    raw["completed_comparisons"] = 2
    raw["missing_comparisons"] = []
    assert with_setup_chat_update(raw)["chat_update"]["id"] != update["id"]


@pytest.mark.parametrize("status", ["incomplete", "stale", "unavailable", "not_started"])
def test_nonterminal_states_never_present_cached_findings_as_final(status):
    raw = assessment(status)
    raw["findings"] = [{"recommendation": "Private cached detail"}]
    update = with_setup_chat_update(raw)["chat_update"]
    assert update["kind"] == "needs_attention"
    assert update["findings"] == []
    assert update["next_action"]
    if status == "unavailable":
        assert "Storage module" not in str(update)
        assert "Selected scope only" not in str(update)


def test_unknown_comparisons_are_not_complete_even_when_all_reviewed():
    raw = assessment("incomplete")
    raw["missing_comparisons"] = []
    raw["comparisons"] = [{"status": "unknown"}]
    update = with_setup_chat_update(raw)["chat_update"]
    assert update["unknown"] == 1
    assert update["kind"] == "needs_attention"


def test_ready_no_findings_is_explicitly_scoped():
    update = with_setup_chat_update(assessment("ready"))["chat_update"]
    assert update["kind"] == "complete"
    assert "No gaps found within the assessed scope" in update["message"]
    assert update["limitations"] == ["Selected scope only"]


def test_ready_preserves_recommendation_target_and_evidence():
    raw = assessment("ready_with_findings")
    raw["findings"] = [
        {
            "status": "partial",
            "recommendation": "Persist locally",
            "recommendation_target": "code",
            "evidence_refs": ["evidence:1"],
        }
    ]
    update = with_setup_chat_update(raw)["chat_update"]
    assert update["findings"] == raw["findings"]
    assert update["kind"] == "complete"
