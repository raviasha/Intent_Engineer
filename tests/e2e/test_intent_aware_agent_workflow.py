"""Complete offline proof of the intent-aware coding-agent operating model."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.intent_aware_agent_harness import IntentAwareAgentHarness


@pytest.fixture
def intent_agent_harness(tmp_path: Path):
    harness = IntentAwareAgentHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def test_existing_repo_prd_to_agent_to_scheduled_assurance(
    intent_agent_harness: IntentAwareAgentHarness,
) -> None:
    bootstrap = intent_agent_harness.bootstrap("docs/prd.md")
    assert bootstrap.core_confirmed
    assert bootstrap.provisional_ids
    assert bootstrap.graph_version == 1
    assert bootstrap.evidence_author == "local:asha"
    assert bootstrap.evidence_version
    assert bootstrap.evidence_acl == ()
    assert bootstrap.replay_byte_stable

    teammate = intent_agent_harness.ingest_teammate_revision()
    assert teammate.authors == ("U123", "U456")
    assert teammate.versions[0] != teammate.versions[1]
    assert teammate.predecessors == (None, teammate.evidence_ids[0])
    assert teammate.graph_version == 2

    aligned = intent_agent_harness.run_aligned_task()
    assert aligned.classification == "aligned"
    assert aligned.mutation.allowed
    assert aligned.mutation.reason == "authorized"
    assert aligned.post_task.status == "recorded"
    assert aligned.post_task.claim is not None
    assert aligned.post_task.claim.requirement_refs == ("requirement:csv-export",)
    assert aligned.post_task.claim.code_evidence
    assert aligned.post_task.claim.test_evidence
    assert aligned.post_task.claim.verified_commit == aligned.final_revision
    assert aligned.graph_version == 3
    assert aligned.detached

    clarified = intent_agent_harness.clarify_new_requirement()
    assert clarified.classification == "new_or_ambiguous"
    assert clarified.question_count == 2
    assert clarified.proposal_id.startswith("proposal:sha256:")
    assert clarified.decision_id is not None
    assert clarified.graph_version == 4
    assert clarified.chronology == ("opened", "answered", "answered", "proposed", "closed")

    conflict = intent_agent_harness.review_conflicting_request()
    assert conflict.classification == "conflicting"
    assert conflict.preflight_case_id.startswith("case:preflight:")
    assert not conflict.mutation.allowed
    assert conflict.mutation.reason == "intent_preflight_required"
    assert conflict.self_review_status == "review_required"
    assert conflict.independent_review_status == "applied"
    assert conflict.review_case_id.startswith("case:sha256:")
    assert "local:ben" in conflict.reviewer_aliases
    assert "local:asha" not in conflict.reviewer_aliases
    assert conflict.graph_version == 5

    host_modes = intent_agent_harness.exercise_host_modes()
    assert host_modes.mandatory_error == "Codex mandatory mutation hook is unavailable"
    assert host_modes.disabled_reason == "plugin_disabled"
    assert host_modes.disabled_workflow_calls == 0
    assert not host_modes.plugin_directory_exists

    assurance = intent_agent_harness.scheduled_assurance()
    assert assurance.first_evidence >= 5
    assert assurance.first_cases >= 1
    assert assurance.fingerprints
    assert assurance.second_evidence == 0
    assert assurance.second_changes == 0
    assert assurance.second_cases == 0
    assert assurance.checkpoint_byte_stable

    views = intent_agent_harness.shared_views()
    assert views.valid
    assert views.cli_graph_version == views.graph_version
    assert views.mcp_graph_version == views.graph_version
    assert "requirement:csv-export" in views.context_requirement_ids

    writes = intent_agent_harness.external_write_governance(conflict.preflight_case_id)
    assert writes.missing_status == "rejected"
    assert writes.missing_reason == "approval_not_found"
    assert writes.missing_provider_calls == 0
    assert writes.changed_status == "rejected"
    assert writes.changed_reason == "target_changed"
    assert writes.changed_provider_mutations == 0
    assert writes.changed_fresh_reads == 1
    assert writes.success_status == "succeeded"
    assert writes.provider_mutations == 1
    assert writes.plan_id.startswith("write-plan:sha256:")
    assert writes.approval_id.startswith("approval:sha256:")
    assert writes.receipt_id.startswith("receipt:sha256:")
    assert writes.resulting_version == "2026-08-20T13:00:00Z"
    assert writes.evidence_author == "local:ben"
    assert writes.evidence_plan_id == writes.plan_id
    assert writes.evidence_approval_id == writes.approval_id
    assert writes.graph_version == views.graph_version + 1
    assert writes.shared_transaction_coordinator
    assert writes.all_services_hold_runtime

    final_views = intent_agent_harness.shared_views()
    assert final_views.valid
    assert final_views.graph_version == writes.graph_version
    assert final_views.cli_graph_version == final_views.graph_version
    assert final_views.mcp_graph_version == final_views.graph_version
    assert intent_agent_harness.secret_leaks() == ()
