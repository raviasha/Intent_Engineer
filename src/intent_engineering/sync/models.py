"""Immutable, aggregated outcomes for a source-sync run."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SyncRunStatus(StrEnum):
    """Overall or per-connector execution status."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ConnectorRunResult(BaseModel):
    """The durable outcome for one connector within a run."""

    model_config = ConfigDict(frozen=True)

    status: SyncRunStatus
    evidence_added: int
    changes_applied: int
    cases_created: int
    checkpoint_advanced: bool
    redacted_error: str | None = None

    @classmethod
    def succeeded(
        cls,
        evidence_added: int,
        changes_applied: int,
        cases_created: int,
        checkpoint_advanced: bool,
    ) -> ConnectorRunResult:
        """Build the result for one fully completed connector."""
        return cls(
            status=SyncRunStatus.SUCCESS,
            evidence_added=evidence_added,
            changes_applied=changes_applied,
            cases_created=cases_created,
            checkpoint_advanced=checkpoint_advanced,
        )

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        evidence_added: int = 0,
        changes_applied: int = 0,
        cases_created: int = 0,
    ) -> ConnectorRunResult:
        """Build a non-sensitive result for one isolated connector failure."""
        return cls(
            status=SyncRunStatus.FAILED,
            evidence_added=evidence_added,
            changes_applied=changes_applied,
            cases_created=cases_created,
            checkpoint_advanced=False,
            redacted_error=error,
        )


class SyncRunResult(BaseModel):
    """A deterministic aggregate over the connector results in one run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    status: SyncRunStatus
    connectors: dict[str, ConnectorRunResult]
    evidence_added: int
    changes_applied: int
    cases_created: int
    duration_ms: int

    @classmethod
    def from_connector_results(
        cls,
        run_id: str,
        connectors: dict[str, ConnectorRunResult],
        duration_ms: int,
    ) -> SyncRunResult:
        """Sum durable outcomes and classify success, partial, or failed execution."""
        connector_results = tuple(connectors.values())
        successful = sum(result.status is SyncRunStatus.SUCCESS for result in connector_results)
        failed = sum(result.status is SyncRunStatus.FAILED for result in connector_results)
        if failed == 0:
            status = SyncRunStatus.SUCCESS
        elif successful > 0:
            status = SyncRunStatus.PARTIAL
        else:
            status = SyncRunStatus.FAILED
        return cls(
            run_id=run_id,
            status=status,
            connectors=dict(connectors),
            evidence_added=sum(result.evidence_added for result in connector_results),
            changes_applied=sum(result.changes_applied for result in connector_results),
            cases_created=sum(result.cases_created for result in connector_results),
            duration_ms=duration_ms,
        )
