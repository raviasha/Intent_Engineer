"""Failure isolation and durable replay at the GitHub connector boundary."""

from __future__ import annotations

import pytest

from intent_engineering.capture.github.connector import GitHubCheckpoint
from intent_engineering.sync.models import SyncRunStatus
from intent_engineering.validation.service import _checkpoint_diagnostics


def _checkpoint_codes(github_sync_harness) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    checkpoint = github_sync_harness.checkpoint_store.get("github:acme/demo")
    ledger = github_sync_harness.evidence_store.ledger(
        "github:acme/demo",
        connector_type="github",
    )
    return tuple(
        diagnostic.code
        for diagnostic in _checkpoint_diagnostics(
            {"github:acme/demo": checkpoint} if checkpoint is not None else {},
            tuple(item.evidence for item in ledger),
            tuple(ledger),
            (),
        )
    )


@pytest.mark.anyio
async def test_later_endpoint_rate_limit_preserves_earlier_durable_work_and_replays_after_source_deletion(
    github_sync_harness,  # type: ignore[no-untyped-def]
) -> None:
    github_sync_harness.api.fail_endpoint = "pulls"
    failed = await github_sync_harness.run("failed")

    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    assert failed.connectors["github:acme/demo"].redacted_error == "connector failed"
    assert github_sync_harness.checkpoint_path.exists() is False
    assert len(github_sync_harness.reasoner.deltas) == 1
    pending_id = github_sync_harness.reasoner.deltas[0].added[0].id

    github_sync_harness.api.fail_endpoint = None
    github_sync_harness.api.payloads["issues"] = []
    github_sync_harness.api.etags["issues"] = '"issues-2"'
    recovered = await github_sync_harness.run("recovered")
    checkpoint = github_sync_harness.checkpoint_store.get("github:acme/demo")

    assert recovered.status is SyncRunStatus.SUCCESS
    assert recovered.evidence_added == 4
    assert pending_id in tuple(record.id for record in github_sync_harness.reasoner.deltas[1].added)
    assert checkpoint is not None
    assert checkpoint.consumed_evidence_ids[0] == pending_id
    assert len(checkpoint.consumed_evidence_ids) == 5

    checkpoint_bytes = github_sync_harness.checkpoint_path.read_bytes()
    noop = await github_sync_harness.run("noop")
    assert noop.connectors["github:acme/demo"].checkpoint_advanced is False
    assert github_sync_harness.checkpoint_path.read_bytes() == checkpoint_bytes
    await github_sync_harness.close()


@pytest.mark.anyio
async def test_malformed_provider_data_is_redacted_and_never_persisted_or_checkpointed(
    github_sync_harness,  # type: ignore[no-untyped-def]
) -> None:
    secret_body = "PRIVATE_PROVIDER_BODY"
    github_sync_harness.api.payloads["issues"] = [{"id": 1, "number": 42, "title": secret_body}]

    result = await github_sync_harness.run("malformed")

    assert result.status is SyncRunStatus.FAILED
    rendered = result.model_dump_json()
    assert rendered.count("connector failed") == 1
    assert secret_body not in rendered
    assert "p_fake-only-never-persist" not in rendered
    assert (
        github_sync_harness.evidence_store.ledger("github:acme/demo", connector_type="github") == ()
    )
    assert github_sync_harness.checkpoint_path.exists() is False
    await github_sync_harness.close()


@pytest.mark.anyio
async def test_later_endpoint_failure_leaves_existing_checkpoint_exact_bytes_unchanged(
    github_sync_harness,  # type: ignore[no-untyped-def]
) -> None:
    baseline = await github_sync_harness.run("baseline")
    assert baseline.status is SyncRunStatus.SUCCESS
    prior_bytes = github_sync_harness.checkpoint_path.read_bytes()

    github_sync_harness.api.payloads["issues"] = [
        {
            **github_sync_harness.api.payloads["issues"][0],
            "title": "Changed after baseline",
            "updated_at": "2026-08-25T11:00:00Z",
        }
    ]
    github_sync_harness.api.etags["issues"] = '"issues-2"'
    github_sync_harness.api.fail_endpoint = "pulls"

    failed = await github_sync_harness.run("failed-after-baseline")

    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    assert github_sync_harness.checkpoint_path.read_bytes() == prior_bytes
    assert (
        len(github_sync_harness.evidence_store.ledger("github:acme/demo", connector_type="github"))
        == 6
    )
    await github_sync_harness.close()


@pytest.mark.anyio
async def test_new_connector_retry_cursors_deleted_newer_mutable_durable_evidence(
    github_sync_harness,  # type: ignore[no-untyped-def]
) -> None:
    baseline = await github_sync_harness.run("baseline")
    assert baseline.status is SyncRunStatus.SUCCESS

    github_sync_harness.api.payloads["issues"] = [
        {
            **github_sync_harness.api.payloads["issues"][0],
            "title": "Durable but deleted before retry",
            "updated_at": "2026-08-26T11:00:00Z",
        }
    ]
    github_sync_harness.api.etags["issues"] = '"issues-2"'
    github_sync_harness.api.fail_endpoint = "pulls"
    failed = await github_sync_harness.run("partial-newer-mutable")

    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    await github_sync_harness.close()
    github_sync_harness.api.fail_endpoint = None
    github_sync_harness.api.payloads["issues"] = []
    github_sync_harness.api.etags["issues"] = '"issues-3"'
    github_sync_harness.restart_connector()

    recovered = await github_sync_harness.run("new-process-retry")
    checkpoint = github_sync_harness.checkpoint_store.get("github:acme/demo")

    assert recovered.status is SyncRunStatus.SUCCESS
    assert checkpoint is not None and checkpoint.cursor is not None
    cursor = GitHubCheckpoint.decode(checkpoint.cursor, expected_repository="acme/demo")
    assert cursor.newest_updated_at.isoformat() == "2026-08-26T11:00:00+00:00"
    assert _checkpoint_codes(github_sync_harness) == ()
    await github_sync_harness.close()


@pytest.mark.anyio
async def test_new_connector_retry_cursors_deleted_first_commit_durable_evidence(
    github_sync_harness,  # type: ignore[no-untyped-def]
) -> None:
    github_sync_harness.api.payloads["issues"] = []
    github_sync_harness.api.payloads["pulls"] = []
    github_sync_harness.api.fail_endpoint = "issues/comments"
    failed = await github_sync_harness.run("partial-first-commit")

    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    await github_sync_harness.close()
    github_sync_harness.api.fail_endpoint = None
    github_sync_harness.api.payloads["commits"] = []
    github_sync_harness.api.payloads["issues/comments"] = []
    github_sync_harness.api.payloads["pulls/comments"] = []
    github_sync_harness.api.etags["commits"] = '"commits-2"'
    github_sync_harness.restart_connector()

    recovered = await github_sync_harness.run("new-process-retry")
    checkpoint = github_sync_harness.checkpoint_store.get("github:acme/demo")

    assert recovered.status is SyncRunStatus.SUCCESS
    assert checkpoint is not None and checkpoint.cursor is not None
    cursor = GitHubCheckpoint.decode(checkpoint.cursor, expected_repository="acme/demo")
    assert cursor.newest_commit_sha == "d" * 40
    assert _checkpoint_codes(github_sync_harness) == ()
    await github_sync_harness.close()
