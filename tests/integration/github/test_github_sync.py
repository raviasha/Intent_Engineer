"""Successful GitHub ingestion through the production orchestrator."""

from __future__ import annotations

import pytest

from intent_engineering.capture.github.connector import GitHubCheckpoint
from intent_engineering.sync.models import SyncRunStatus


@pytest.mark.anyio
async def test_success_persists_all_five_kinds_and_exact_consumed_prefix(
    github_sync_harness,
) -> None:  # type: ignore[no-untyped-def]
    result = await github_sync_harness.run()
    checkpoint = github_sync_harness.checkpoint_store.get("github:acme/demo")
    ledger = github_sync_harness.evidence_store.ledger("github:acme/demo", connector_type="github")

    assert result.status is SyncRunStatus.SUCCESS
    assert result.evidence_added == 5
    assert checkpoint is not None
    assert checkpoint.consumed_evidence_ids == tuple(item.evidence.id for item in ledger)
    assert {item.evidence.model_dump(mode="json")["payload"]["kind"] for item in ledger} == {
        "issue",
        "pull_request",
        "commit",
        "issue_comment",
        "review_comment",
    }
    issue_comment = next(
        item.evidence
        for item in ledger
        if item.evidence.model_dump(mode="json")["payload"]["kind"] == "issue_comment"
    )
    assert issue_comment.source_locator == ("https://github.com/acme/demo/pull/7#issuecomment-3001")
    assert issue_comment.model_dump(mode="json")["payload"]["issue_number"] == 7
    assert (
        GitHubCheckpoint.decode(checkpoint.cursor, expected_repository="acme/demo").repository
        == "acme/demo"
    )
    await github_sync_harness.close()


@pytest.mark.anyio
async def test_second_identical_sync_is_zero_mutation_and_does_not_advance_checkpoint(
    github_sync_harness,
) -> None:  # type: ignore[no-untyped-def]
    first = await github_sync_harness.run("first")
    checkpoint_bytes = github_sync_harness.checkpoint_path.read_bytes()
    second = await github_sync_harness.run("second")

    assert first.evidence_added == 5
    assert second.status is SyncRunStatus.SUCCESS
    assert second.evidence_added == 0
    assert second.changes_applied == 0
    assert second.cases_created == 0
    assert second.connectors["github:acme/demo"].checkpoint_advanced is False
    assert github_sync_harness.checkpoint_path.read_bytes() == checkpoint_bytes
    await github_sync_harness.close()


@pytest.mark.anyio
async def test_two_repositories_use_isolated_connector_ledgers_and_checkpoints(
    github_sync_harness,
) -> None:  # type: ignore[no-untyped-def]
    other_client, other = github_sync_harness.build_repository_connector("acme", "other")
    result = await github_sync_harness.orchestrator.run(
        "multi-repository",
        (github_sync_harness.connector, other),
    )
    demo_ledger = github_sync_harness.evidence_store.ledger(
        "github:acme/demo", connector_type="github"
    )
    other_ledger = github_sync_harness.evidence_store.ledger(
        "github:acme/other", connector_type="github"
    )

    assert result.status is SyncRunStatus.SUCCESS
    assert other.connector_type == github_sync_harness.connector.connector_type == "github"
    assert other.connector_id == "github:acme/other"
    assert len(demo_ledger) == len(other_ledger) == 5
    assert {item.evidence.id for item in demo_ledger}.isdisjoint(
        item.evidence.id for item in other_ledger
    )
    assert github_sync_harness.checkpoint_store.get("github:acme/demo") is not None
    assert github_sync_harness.checkpoint_store.get("github:acme/other") is not None
    await other_client.aclose()
    await github_sync_harness.close()
