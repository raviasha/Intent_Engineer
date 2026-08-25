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
) -> SyncCheckpoint:
    """Build the typed checkpoint that follows a successfully persisted discovery batch."""
    return SyncCheckpoint(
        connector_id=connector.connector_id,
        cursor=connector.next_checkpoint(discovered),
        committed_at=committed_at,
    )
