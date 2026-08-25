"""Helpers for committing a connector's next durable checkpoint."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from intent_engineering.capture.base import Connector, SourceObject
from intent_engineering.core.models import SyncCheckpoint


def checkpoint_after_discovery(
    connector: Connector,
    discovered: Sequence[SourceObject],
    committed_at: datetime,
    prior: SyncCheckpoint | None = None,
) -> SyncCheckpoint:
    """Build a checkpoint, retaining the prior cursor for an empty successful batch."""
    cursor = connector.next_checkpoint(discovered)
    if not discovered and prior is not None:
        cursor = prior.cursor
    return SyncCheckpoint(
        connector_id=connector.connector_id,
        cursor=cursor,
        committed_at=committed_at,
    )
