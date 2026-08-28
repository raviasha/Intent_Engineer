"""Provider-neutral connector lifecycle boundaries in the real sync orchestrator."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intent_engineering.capture.base import (
    ConnectorError,
    RawSourceObject,
    SourceObject,
    normalize_raw_source,
)
from intent_engineering.core.models import CandidateAssertion, EvidenceDelta, EvidenceRecord
from intent_engineering.extract.deterministic import DeterministicReasoner
from intent_engineering.sync.models import SyncRunStatus

from .conftest import SyncHarness

NOW = datetime(2026, 8, 25, tzinfo=UTC)


class LifecycleConnector:
    """Reject overlap and expose lifecycle calls without provider-specific semantics."""

    connector_id = "lifecycle"
    connector_type = "lifecycle"

    def __init__(
        self,
        *,
        connector_id: str = "lifecycle",
        fail_fetch_once: bool = False,
        block_fetch: bool = False,
        cancel_finalize_once: bool = False,
    ) -> None:
        self.connector_id = connector_id
        self.active = False
        self.fail_fetch_once = fail_fetch_once
        self.abort_count = 0
        self.finalized_ids: tuple[str, ...] = ()
        self._discovered: tuple[SourceObject, ...] = ()
        self.block_fetch = block_fetch
        self.fetch_started = asyncio.Event()
        self.release_fetch = asyncio.Event()
        self.cancel_finalize_once = cancel_finalize_once

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        del cursor
        if self.active:
            raise ConnectorError("overlapping generation")
        self.active = True
        self._discovered = (
            SourceObject(
                external_object_id="fixture:lifecycle",
                external_version="v1",
                locator="lifecycle.fixture",
            ),
        )
        return self._discovered

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        if self.block_fetch:
            self.fetch_started.set()
            await self.release_fetch.wait()
        if self.fail_fetch_once:
            self.fail_fetch_once = False
            raise ConnectorError("fetch failed")
        return RawSourceObject(
            connector_type=self.connector_type,
            external_object_id=object_id,
            external_version=version,
            author=None,
            observed_at=NOW,
            source_locator="lifecycle.fixture",
            content_hash="sha256:lifecycle-v1",
            payload={},
        )

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        return normalize_raw_source(raw)

    def next_checkpoint(self, discovered: tuple[SourceObject, ...]) -> str | None:
        del discovered
        raise AssertionError("evidence-aware finalization must be preferred")

    def finalize_checkpoint(
        self,
        discovered: tuple[SourceObject, ...],
        consumed_evidence: tuple[EvidenceRecord, ...],
    ) -> str | None:
        if not self.active or discovered != self._discovered:
            raise ConnectorError("stale generation")
        if self.cancel_finalize_once:
            self.cancel_finalize_once = False
            raise asyncio.CancelledError
        self.finalized_ids = tuple(record.id for record in consumed_evidence)
        self.active = False
        self._discovered = ()
        return "v1"

    def abort_sync(self) -> None:
        self.abort_count += 1
        self.active = False
        self._discovered = ()


@pytest.mark.anyio
async def test_fetch_failure_aborts_generation_and_retry_finalizes_exact_durable_records(
    tmp_path: Path,
) -> None:
    connector = LifecycleConnector(fail_fetch_once=True)
    harness = SyncHarness(tmp_path, (connector,))

    failed = await harness.run()
    recovered = await harness.run()
    checkpoint = harness.checkpoint_store.get("lifecycle")

    assert failed.status is SyncRunStatus.FAILED
    assert connector.abort_count == 1
    assert recovered.status is SyncRunStatus.SUCCESS
    assert checkpoint is not None
    assert connector.finalized_ids == checkpoint.consumed_evidence_ids
    assert connector.finalized_ids == tuple(
        item.evidence.id
        for item in harness.evidence_store.ledger("lifecycle", connector_type="lifecycle")
    )


class FailOnceDetector:
    def __init__(self) -> None:
        self.failed = False

    def __call__(self, delta, graph):  # type: ignore[no-untyped-def]
        del delta, graph
        if not self.failed:
            self.failed = True
            raise RuntimeError("detector failed")
        return ()


class FailOnceReasoner(DeterministicReasoner):
    def __init__(self) -> None:
        super().__init__(actor="lifecycle-fixture")
        self.failed = False

    def extract_assertions(self, delta: EvidenceDelta) -> tuple[CandidateAssertion, ...]:
        if not self.failed:
            self.failed = True
            raise RuntimeError("reasoner failed")
        return super().extract_assertions(delta)


@pytest.mark.anyio
async def test_reasoner_failure_aborts_generation_and_allows_durable_replay(
    tmp_path: Path,
) -> None:
    connector = LifecycleConnector()
    harness = SyncHarness(tmp_path, (connector,), reasoner=FailOnceReasoner())

    failed = await harness.run()
    recovered = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert connector.abort_count == 1
    assert recovered.status is SyncRunStatus.SUCCESS
    assert connector.active is False


@pytest.mark.anyio
async def test_detector_failure_aborts_generation_and_allows_legitimate_retry(
    tmp_path: Path,
) -> None:
    connector = LifecycleConnector()
    harness = SyncHarness(tmp_path, (connector,), case_detector=FailOnceDetector())

    failed = await harness.run()
    recovered = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert connector.abort_count == 1
    assert recovered.status is SyncRunStatus.SUCCESS
    assert connector.active is False


@pytest.mark.anyio
async def test_rejected_overlapping_run_cannot_abort_the_accepted_generation(
    tmp_path: Path,
) -> None:
    connector = LifecycleConnector(block_fetch=True)
    harness = SyncHarness(tmp_path, (connector,))
    accepted_task = asyncio.create_task(harness.run())
    await connector.fetch_started.wait()

    rejected = await harness.run()
    assert rejected.status is SyncRunStatus.FAILED
    assert connector.abort_count == 0
    assert connector.active is True

    connector.release_fetch.set()
    accepted = await accepted_task

    assert accepted.status is SyncRunStatus.SUCCESS
    assert connector.active is False


@pytest.mark.anyio
async def test_fetch_cancellation_aborts_owned_generation_and_is_re_raised(
    tmp_path: Path,
) -> None:
    connector = LifecycleConnector(block_fetch=True)
    harness = SyncHarness(tmp_path, (connector,))
    cancelled_task = asyncio.create_task(harness.run())
    await connector.fetch_started.wait()

    cancelled_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task

    assert connector.abort_count == 1
    assert connector.active is False
    connector.block_fetch = False
    recovered = await harness.run()
    assert recovered.status is SyncRunStatus.SUCCESS


@pytest.mark.anyio
async def test_later_fetch_cancellation_aborts_every_generation_owned_by_the_run(
    tmp_path: Path,
) -> None:
    first = LifecycleConnector(connector_id="lifecycle:first")
    second = LifecycleConnector(connector_id="lifecycle:second", block_fetch=True)
    harness = SyncHarness(tmp_path, (first, second))
    cancelled_task = asyncio.create_task(harness.run())
    await second.fetch_started.wait()

    cancelled_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task

    assert (first.abort_count, second.abort_count) == (1, 1)
    assert (first.active, second.active) == (False, False)
    second.block_fetch = False
    recovered = await harness.run()
    assert recovered.status is SyncRunStatus.SUCCESS


@pytest.mark.anyio
async def test_checkpoint_cancellation_aborts_later_unfinalized_sibling_generation(
    tmp_path: Path,
) -> None:
    first = LifecycleConnector(
        connector_id="lifecycle:first",
        cancel_finalize_once=True,
    )
    second = LifecycleConnector(connector_id="lifecycle:second")
    harness = SyncHarness(tmp_path, (first, second))

    with pytest.raises(asyncio.CancelledError):
        await harness.run()

    assert (first.abort_count, second.abort_count) == (1, 1)
    assert (first.active, second.active) == (False, False)
    recovered = await harness.run()
    assert recovered.status is SyncRunStatus.SUCCESS
