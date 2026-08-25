"""Regression coverage for no-op repeated syncs."""

from typing import Any

import pytest

from intent_engineering.sync.models import SyncRunStatus


@pytest.mark.anyio
async def test_second_identical_sync_has_no_semantic_change(sync_harness: Any) -> None:
    """Reprocessing identical source versions must not mutate durable semantic state."""
    first = await sync_harness.run()
    checkpoint_bytes = sync_harness.checkpoint_path.read_bytes()
    second = await sync_harness.run()

    assert first.evidence_added > 0
    assert second.evidence_added == 0
    assert second.changes_applied == 0
    assert second.cases_created == 0
    assert second.status is SyncRunStatus.SUCCESS
    assert sync_harness.checkpoint_path.read_bytes() == checkpoint_bytes
