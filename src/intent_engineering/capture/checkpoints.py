"""Helpers for committing a connector's next durable checkpoint."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from intent_engineering.capture.base import Connector, ConnectorSyncLifecycle, SourceObject
from intent_engineering.core.models import EvidenceRecord, SyncCheckpoint


def checkpoint_after_discovery(
    connector: Connector,
    discovered: Sequence[SourceObject],
    committed_at: datetime,
    prior: SyncCheckpoint | None = None,
    consumed_evidence_ids: Sequence[str] = (),
    consumed_evidence: Sequence[EvidenceRecord] = (),
) -> SyncCheckpoint:
    """Build a checkpoint, retaining the prior cursor for an empty successful batch."""
    if isinstance(connector, ConnectorSyncLifecycle):
        if tuple(record.id for record in consumed_evidence) != tuple(consumed_evidence_ids):
            raise ValueError("checkpoint evidence inputs are inconsistent")
        cursor = connector.finalize_checkpoint(discovered, consumed_evidence)
    else:
        cursor = connector.next_checkpoint(discovered)
    if not discovered and prior is not None and cursor is None:
        cursor = prior.cursor
    return SyncCheckpoint(
        connector_id=connector.connector_id,
        cursor=cursor,
        committed_at=committed_at,
        consumed_evidence_ids=tuple(consumed_evidence_ids),
    )
