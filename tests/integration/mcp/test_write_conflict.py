"""Changed-target and expiry contracts for approved writes."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from intent_engineering.core.models import ReconciliationStatus
from intent_engineering.mutations.executor import ExecutionUnavailable
from tests.integration.mcp.test_write_execution import (
    WriteHarness,
    fake_write_harness,
    local_write_harness,
    write_harness,
)
from tests.unit.mutations.test_planner import NOW, jira_binding, jira_profile

__all__ = ["write_harness"]


@pytest.mark.anyio
@pytest.mark.parametrize("changed_field", ["version", "content"])
async def test_target_change_invalidates_approval_without_a_write(
    write_harness: WriteHarness,
    changed_field: str,
) -> None:
    current = write_harness.gateway.current
    changed = (
        "2026-08-26T11:00:01Z"
        if changed_field == "version"
        else {**dict(current.content), "summary": "changed behind the plan"}
    )
    write_harness.gateway.current = current.model_copy(update={changed_field: changed})

    receipt = await write_harness.executor.execute(
        write_harness.plan_id,
        write_harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=2),
    )

    assert receipt.status == "rejected"
    assert receipt.redacted_error == "target_changed"
    assert write_harness.gateway.write_calls == []
    assert write_harness.committer.committed == []


@pytest.mark.anyio
async def test_expired_approval_is_rejected_before_durable_claim_or_fetch(
    write_harness: WriteHarness,
) -> None:
    with pytest.raises(ExecutionUnavailable):
        await write_harness.executor.execute(
            write_harness.plan_id,
            write_harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=12),
        )

    assert write_harness.receipts.list() == ()
    assert write_harness.gateway.fetch_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "drift",
    ("contributor_policy", "approver_policy", "executor_policy", "profile", "binding"),
)
async def test_policy_profile_or_binding_drift_fails_before_claim_or_fetch(
    tmp_path: Path,
    drift: str,
) -> None:
    options: dict[str, object] = {}
    if drift == "contributor_policy":
        options["authorized_contributors"] = frozenset({"local:different"})
    elif drift == "approver_policy":
        options["authorized_approvers"] = frozenset({"local:different"})
    elif drift == "executor_policy":
        options["authorized_executors"] = frozenset({"local:different"})
    elif drift == "profile":
        options["profile"] = jira_profile().model_copy(update={"version": "2"})
    else:
        binding = jira_binding()
        options["binding"] = binding.model_copy(
            update={"tools": {**dict(binding.tools), "update_issue": "other_tool"}}
        )
    harness = fake_write_harness(tmp_path, **options)  # type: ignore[arg-type]

    with pytest.raises(ExecutionUnavailable):
        await harness.executor.execute(
            harness.plan_id,
            harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    assert not harness.receipts.is_claimed(harness.plan_id, harness.approval_id)
    assert harness.gateway.fetch_calls == 0
    assert harness.gateway.write_calls == []


@pytest.mark.anyio
async def test_changed_target_receipt_never_resolves_real_local_case_or_graph(
    tmp_path: Path,
) -> None:
    harness = local_write_harness(tmp_path)
    harness.gateway.current = harness.gateway.current.model_copy(
        update={"version": "changed-after-approval"}
    )

    receipt = await harness.executor.execute(
        harness.plan_id,
        harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=2),
    )

    assert receipt.status == "rejected"
    assert harness.case_store.get("case-write-1").status is not ReconciliationStatus.RESOLVED
    assert harness.graph_store.load().version == 0
    assert len(harness.evidence_store.list()) == 2
    assert harness.graph_store.history("case-write-1") == ()
