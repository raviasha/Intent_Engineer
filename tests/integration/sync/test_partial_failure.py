"""Regression coverage for isolated connector failures."""

from typing import Any

import pytest

from intent_engineering.sync.models import SyncRunStatus


@pytest.mark.anyio
async def test_failed_connector_does_not_advance_its_checkpoint(
    partial_failure_harness: Any,
) -> None:
    """A connector failure cannot make its cursor appear durably consumed."""
    result = await partial_failure_harness.run()

    assert result.status is SyncRunStatus.PARTIAL
    assert result.connectors["markdown"].checkpoint_advanced is True
    assert result.connectors["broken"].checkpoint_advanced is False
    assert result.connectors["broken"].redacted_error == "connector failed"
