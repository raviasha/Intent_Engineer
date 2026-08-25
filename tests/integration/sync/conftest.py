"""Real durable fixtures for idempotent sync orchestration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from intent_engineering.capture.base import (
    ConnectorError,
    RawSourceObject,
    SourceObject,
    normalize_raw_source,
)
from intent_engineering.core.models import Graph, NodeType, SourceMode
from intent_engineering.extract.deterministic import DeterministicReasoner
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.yaml.checkpoint_store import YamlCheckpointStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from intent_engineering.sync.models import SyncRunResult
from intent_engineering.sync.orchestrator import SyncOrchestrator

NOW = datetime(2026, 8, 25, tzinfo=UTC)


class FixtureConnector:
    """A repeatable connector whose discovery intentionally repeats one source."""

    connector_id = "markdown"

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        return (SourceObject(external_object_id="fixture:requirements", external_version="v1", locator="requirements.md"),)

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        return RawSourceObject(
            connector_type="markdown",
            external_object_id=object_id,
            external_version=version,
            author="fixture@example.test",
            observed_at=NOW,
            source_locator="requirements.md",
            content_hash="sha256:fixture-requirements-v1",
            payload={
                "intent_assertion": {
                    "id": "assertion:local-export",
                    "subject_id": "requirement:local-export",
                    "change_kind": "initialize",
                    "node_type": NodeType.REQUIREMENT,
                    "label": "Exports remain local-first",
                    "source_mode": SourceMode.EXPLICIT,
                    "evidence_refs": ("evidence:fixture-requirements-v1",),
                    "confidence": 0.9,
                }
            },
        )

    def normalize(self, raw: RawSourceObject):  # type: ignore[no-untyped-def]
        return normalize_raw_source(raw)

    def next_checkpoint(self, discovered: tuple[SourceObject, ...]) -> str | None:
        return "v1" if discovered else None


class BrokenConnector:
    """A connector that fails at discovery without revealing implementation detail."""

    connector_id = "broken"

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        raise ConnectorError("fixture failure")

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        raise AssertionError("fetch must not run after failed discovery")

    def normalize(self, raw: RawSourceObject):  # type: ignore[no-untyped-def]
        raise AssertionError("normalize must not run after failed discovery")

    def next_checkpoint(self, discovered: tuple[SourceObject, ...]) -> str | None:
        raise AssertionError("checkpoint must not run after failed discovery")


class SyncHarness:
    """One configured orchestrator run repeatedly against durable local adapters."""

    def __init__(self, root: Path, connectors: tuple[FixtureConnector | BrokenConnector, ...]) -> None:
        graph = YamlGraphStore(root / "graph.yaml", history_path=root / "history.jsonl")
        graph.initialize(Graph(id="fixture-graph", version=0, nodes=(), edges=()))
        self.orchestrator = SyncOrchestrator(
            graph_store=graph,
            evidence_store=JsonlEvidenceStore(root / "evidence.jsonl"),
            checkpoint_store=YamlCheckpointStore(root / "checkpoints.yaml"),
            case_store=JsonlCaseStore(root / "cases.jsonl"),
            reasoner=DeterministicReasoner(actor="fixture"),
        )
        self.connectors = connectors

    async def run(self) -> SyncRunResult:
        return await self.orchestrator.run("fixture-run", self.connectors)


@pytest.fixture
def sync_harness(tmp_path: Path) -> SyncHarness:
    return SyncHarness(tmp_path, (FixtureConnector(),))


@pytest.fixture
def partial_failure_harness(tmp_path: Path) -> SyncHarness:
    return SyncHarness(tmp_path, (FixtureConnector(), BrokenConnector()))
