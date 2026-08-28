"""Provider-denial and cancellation contracts for external mutations."""

from __future__ import annotations

import asyncio
import traceback
from datetime import timedelta
from pathlib import Path

import pytest

from intent_engineering.capture.mcp import McpPermissionError
from intent_engineering.core.models import ResolutionAction
from intent_engineering.mutations.executor import ExecutionUnavailable
from intent_engineering.mutations.planner import build_write_plan
from tests.integration.mcp.test_write_execution import (
    WriteHarness,
    fake_write_harness,
    write_harness,
)
from tests.unit.mutations.test_planner import (
    IDENTITY_ALIASES,
    NOW,
    jira_binding,
    jira_profile,
    remote_object,
    review_case,
)

__all__ = ["write_harness"]


@pytest.mark.anyio
async def test_permission_denied_persists_only_a_fixed_failure_code(
    write_harness: WriteHarness,
) -> None:
    write_harness.gateway.failure = McpPermissionError()

    receipt = await write_harness.executor.execute(
        write_harness.plan_id,
        write_harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=2),
    )

    assert receipt.status == "failed"
    assert receipt.redacted_error == "permission_denied"
    assert receipt.resulting_version is None
    assert len(write_harness.gateway.write_calls) == 1
    assert write_harness.committer.committed == []


@pytest.mark.anyio
async def test_cancellation_after_claim_leaves_a_fail_closed_manual_recovery_state(
    write_harness: WriteHarness,
) -> None:
    write_harness.gateway.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await write_harness.executor.execute(
            write_harness.plan_id,
            write_harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    with pytest.raises(ExecutionUnavailable):
        await write_harness.executor.execute(
            write_harness.plan_id,
            write_harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=3),
        )
    assert len(write_harness.gateway.write_calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ("fetch", "write"))
async def test_cancellation_preserves_signal_without_retaining_write_arguments(
    tmp_path: Path,
    phase: str,
) -> None:
    sentinel = "PRIVATE-APPROVED-WRITE-ARGUMENT"
    plan = build_write_plan(
        review_case(),
        jira_profile(),
        jira_binding(),
        "update_issue",
        remote_object(),
        {"summary": sentinel},
        actor="local:proposer",
        authorized_contributors=frozenset({"local:proposer"}),
        identity_aliases=IDENTITY_ALIASES,
        resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
        now=NOW,
    )
    harness = fake_write_harness(tmp_path, plan)
    cancellation = asyncio.CancelledError()
    if phase == "fetch":
        harness.gateway.fetch_failure = cancellation
    else:
        harness.gateway.failure = cancellation

    with pytest.raises(asyncio.CancelledError) as caught:
        await harness.executor.execute(
            harness.plan_id,
            harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    rendered = traceback.TracebackException.from_exception(
        caught.value,
        capture_locals=True,
    )
    repository_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value is cancellation
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert sentinel not in repository_locals
    assert harness.receipts.is_claimed(harness.plan_id, harness.approval_id)
    assert harness.receipts.list() == ()


@pytest.mark.anyio
async def test_malformed_provider_target_becomes_a_fixed_failure_receipt(
    write_harness: WriteHarness,
) -> None:
    sentinel = "PRIVATE-MALFORMED-PROVIDER-TARGET"

    class MalformedTarget:
        def model_dump_json(self) -> str:
            raise ValueError(sentinel)

    write_harness.gateway.current = MalformedTarget()  # type: ignore[assignment]

    receipt = await write_harness.executor.execute(
        write_harness.plan_id,
        write_harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=2),
    )

    assert receipt.status == "failed"
    assert receipt.redacted_error == "provider_failure"
    assert write_harness.gateway.write_calls == []
    assert sentinel not in str(receipt)
    assert sentinel.encode() not in write_harness.receipts.path.read_bytes()


@pytest.mark.anyio
async def test_receipt_store_failure_is_fixed_without_retaining_approved_arguments(
    tmp_path: Path,
) -> None:
    sentinel = "PRIVATE-APPROVED-RECEIPT-BOUNDARY"
    plan = build_write_plan(
        review_case(),
        jira_profile(),
        jira_binding(),
        "update_issue",
        remote_object(),
        {"summary": sentinel},
        actor="local:proposer",
        authorized_contributors=frozenset({"local:proposer"}),
        identity_aliases=IDENTITY_ALIASES,
        resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
        now=NOW,
    )
    harness = fake_write_harness(tmp_path, plan)
    harness.receipts.path.write_bytes(b'{"PRIVATE-CORRUPT-RECEIPT":')

    with pytest.raises(ExecutionUnavailable) as caught:
        await harness.executor.execute(
            harness.plan_id,
            harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    rendered = traceback.TracebackException.from_exception(
        caught.value,
        capture_locals=True,
    )
    repository_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value.args == ("external write execution unavailable",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert sentinel not in repository_locals
    assert "PRIVATE-CORRUPT-RECEIPT" not in repository_locals
    assert harness.gateway.fetch_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ("get_for", "claim", "complete"))
async def test_receipt_store_interrupt_preserves_signal_without_approved_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    sentinel = "PRIVATE-APPROVED-RECEIPT-INTERRUPT"
    plan = build_write_plan(
        review_case(),
        jira_profile(),
        jira_binding(),
        "update_issue",
        remote_object(),
        {"summary": sentinel},
        actor="local:proposer",
        authorized_contributors=frozenset({"local:proposer"}),
        identity_aliases=IDENTITY_ALIASES,
        resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
        now=NOW,
    )
    harness = fake_write_harness(tmp_path, plan)
    cancellation = asyncio.CancelledError()

    def abort(*_args: object, **_kwargs: object) -> None:
        raise cancellation

    monkeypatch.setattr(harness.receipts, phase, abort)
    if phase == "complete":
        harness.gateway.failure = McpPermissionError()

    with pytest.raises(asyncio.CancelledError) as caught:
        await harness.executor.execute(
            harness.plan_id,
            harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    rendered = traceback.TracebackException.from_exception(
        caught.value,
        capture_locals=True,
    )
    repository_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value is cancellation
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert sentinel not in repository_locals


@pytest.mark.anyio
async def test_approval_load_interrupt_drops_the_already_loaded_private_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "PRIVATE-PLAN-BEFORE-APPROVAL-INTERRUPT"
    plan = build_write_plan(
        review_case(),
        jira_profile(),
        jira_binding(),
        "update_issue",
        remote_object(),
        {"summary": sentinel},
        actor="local:proposer",
        authorized_contributors=frozenset({"local:proposer"}),
        identity_aliases=IDENTITY_ALIASES,
        resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
        now=NOW,
    )
    harness = fake_write_harness(tmp_path, plan)
    cancellation = asyncio.CancelledError()

    def abort(_approval_id: str) -> None:
        raise cancellation

    monkeypatch.setattr(harness.executor._approvals, "get", abort)

    with pytest.raises(asyncio.CancelledError) as caught:
        await harness.executor.execute(
            harness.plan_id,
            harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    rendered = traceback.TracebackException.from_exception(
        caught.value,
        capture_locals=True,
    )
    repository_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value is cancellation
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert sentinel not in repository_locals
    assert harness.gateway.fetch_calls == 0
