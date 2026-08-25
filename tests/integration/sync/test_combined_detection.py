"""Drift detection must combine independently captured connector evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from intent_engineering.capture.base import (
    RawSourceObject,
    SourceObject,
    normalize_raw_source,
)
from intent_engineering.core.models import EvidenceRecord
from intent_engineering.reconcile.evidence_detection import detect_evidence_drift
from intent_engineering.sync.models import SyncRunStatus
from sync.conftest import SyncHarness

NOW = datetime(2026, 8, 25, tzinfo=UTC)
IMPLEMENTATION_AT = datetime(2026, 8, 24, tzinfo=UTC)


class DeclarationConnector:
    connector_id = "markdown"

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        return (
            SourceObject(
                external_object_id="path:requirement.md",
                external_version="v2",
                locator="requirement.md",
            ),
        )

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        return RawSourceObject(
            connector_type=self.connector_id,
            external_object_id=object_id,
            external_version=version,
            author="product@example.test",
            observed_at=NOW,
            source_locator="requirement.md",
            content_hash="sha256:requirement-v2",
            payload={
                "intent_assertion": {
                    "id": "assertion:export",
                    "subject_id": "requirement:export",
                    "change_kind": "initialize",
                    "node_type": "REQUIREMENT",
                    "label": "Export locally",
                    "source_mode": "explicit",
                    "evidence_refs": ["$self"],
                    "confidence": 0.9,
                },
                "detection_input": {
                    "schema_version": 1,
                    "subject_ref": "requirement:export",
                    "affected_refs": ["requirement:export"],
                    "compatibility": "aligns",
                    "requirement": {
                        "label": "requirement",
                        "claim": "Export locally",
                        "evidence_refs": ["$self"],
                        "confidence": 0.9,
                    },
                    "implementation": {
                        "label": "implementation",
                        "claim": "Legacy export",
                        "evidence_refs": ["source:git:commit:implementation-v1"],
                        "confidence": 0.8,
                    },
                },
            },
        )

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        record = normalize_raw_source(raw)
        payload = record.model_dump(mode="python")["payload"]
        payload["intent_assertion"]["evidence_refs"] = [record.id]
        return EvidenceRecord.model_validate({**record.model_dump(), "payload": payload})

    def next_checkpoint(self, discovered: tuple[SourceObject, ...]) -> str:
        return "v2"


class ImplementationConnector:
    connector_id = "git"

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        return (
            SourceObject(
                external_object_id="commit:implementation-v1",
                external_version="implementation-v1",
                locator="git:commit:implementation-v1",
            ),
        )

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        return RawSourceObject(
            connector_type=self.connector_id,
            external_object_id=object_id,
            external_version=version,
            author="engineer@example.test",
            observed_at=IMPLEMENTATION_AT,
            source_locator="git:commit:implementation-v1",
            content_hash="sha256:implementation-v1",
            payload={"changed_paths": ["src/export.py"]},
        )

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        return normalize_raw_source(raw)

    def next_checkpoint(self, discovered: tuple[SourceObject, ...]) -> str:
        return "implementation-v1"


def _detect(delta, graph):  # type: ignore[no-untyped-def]
    return detect_evidence_drift(delta.added, graph, "fixture")


@pytest.mark.anyio
async def test_detection_sees_combined_successful_connectors_and_uses_case_changeset(
    tmp_path: Path,
) -> None:
    harness = SyncHarness(
        tmp_path,
        (DeclarationConnector(), ImplementationConnector()),
        case_detector=_detect,
    )

    result = await harness.run()

    assert result.status is SyncRunStatus.SUCCESS
    cases = harness.case_store.list()
    assert len(cases) == 1
    assert cases[0].case_type.value == "CODE_LAG"
    assert harness.graph_store.history(cases[0].id)[0].reconciliation_cases_created == (
        cases[0].id,
    )


@pytest.mark.anyio
async def test_removing_git_causal_evidence_prevents_case_creation(tmp_path: Path) -> None:
    harness = SyncHarness(
        tmp_path,
        (DeclarationConnector(),),
        case_detector=_detect,
    )

    result = await harness.run()

    assert result.status is SyncRunStatus.SUCCESS
    assert harness.case_store.list() == ()
