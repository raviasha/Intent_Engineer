"""Real durable fixtures for idempotent sync orchestration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from intent_engineering.capture.base import (
    ConnectorError,
    RawSourceObject,
    SourceObject,
    normalize_raw_source,
)
from intent_engineering.core.models import Graph, NodeType, SourceMode
from intent_engineering.extract.base import SemanticReasoner
from intent_engineering.extract.deterministic import DeterministicReasoner
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
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


class VersionedFixtureConnector:
    """A fixture connector that exposes exactly one active source version at a time."""

    connector_id = "markdown"

    def __init__(self) -> None:
        self.active_version = "v1"

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        if cursor == self.active_version:
            return ()
        return (
            SourceObject(
                external_object_id="fixture:requirements",
                external_version=self.active_version,
                locator="requirements.md",
            ),
        )

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        return RawSourceObject(
            connector_type="markdown",
            external_object_id=object_id,
            external_version=version,
            author="fixture@example.test",
            observed_at=NOW,
            source_locator="requirements.md",
            content_hash=f"sha256:fixture-requirements-{version}",
            payload={
                "intent_assertion": {
                    "id": f"assertion:local-export:{version}",
                    "subject_id": "requirement:local-export",
                    "change_kind": "initialize",
                    "node_type": NodeType.REQUIREMENT,
                    "label": "Exports remain local-first",
                    "source_mode": SourceMode.EXPLICIT,
                    "evidence_refs": (f"evidence:fixture-requirements-{version}",),
                    "confidence": 0.9,
                }
            },
        )

    def normalize(self, raw: RawSourceObject):  # type: ignore[no-untyped-def]
        return normalize_raw_source(raw)

    def next_checkpoint(self, discovered: tuple[SourceObject, ...]) -> str | None:
        return self.active_version if discovered else None


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

    def __init__(
        self,
        root: Path,
        connectors: tuple[Any, ...],
        *,
        reasoner: SemanticReasoner | None = None,
        case_detector: Any = None,
        checkpoint_store: Any = None,
        case_store: Any = None,
        transaction_fault_hook: Any = None,
    ) -> None:
        directory = SecureDirectory.open(root, create=True)
        self.graph_path = root / "graph.yaml"
        self.evidence_path = root / "evidence.jsonl"
        self.checkpoint_path = root / "checkpoints.yaml"
        self.case_path = root / "cases.jsonl"
        self.config_file = directory.file("config.yaml")
        self.config_file.atomic_write(b"project_id: fixture\n")
        raw_case_store = case_store or JsonlCaseStore(directory.file("cases.jsonl"))
        executor_case_store = (
            raw_case_store
            if isinstance(raw_case_store, JsonlCaseStore)
            else raw_case_store._store
        )
        transactions = LocalTransactionCoordinator(
            directory.file(".local-transaction.json"),
            {
                "graph": directory.file("graph.yaml"),
                "history": directory.file("history.jsonl"),
                "cases": executor_case_store._file,
                "evidence": directory.file("evidence.jsonl"),
            },
            fault_hook=transaction_fault_hook,
        )
        self.graph_store = YamlGraphStore(
            directory.file("graph.yaml"),
            history_path=directory.file("history.jsonl"),
            transactions=transactions,
        )
        self.graph_store.initialize(Graph(id="fixture-graph", version=0, nodes=(), edges=()))
        self.evidence_store = JsonlEvidenceStore(
            directory.file("evidence.jsonl"), transactions=transactions
        )
        self.checkpoint_store = checkpoint_store or YamlCheckpointStore(self.checkpoint_path)
        self.case_store = raw_case_store
        executor = LocalChangeSetExecutor(
            self.graph_store,
            executor_case_store,
            transactions,
        )
        orchestrator_options: dict[str, Any] = {}
        if case_detector is not None:
            orchestrator_options["case_detector"] = case_detector
        self.orchestrator = SyncOrchestrator(
            graph_store=self.graph_store,
            evidence_store=self.evidence_store,
            checkpoint_store=self.checkpoint_store,
            case_store=self.case_store,
            reasoner=reasoner or DeterministicReasoner(actor="fixture"),
            changeset_executor=executor,
            transactions=transactions,
            snapshot_files={"config": self.config_file},
            **orchestrator_options,
        )
        self.transactions = transactions
        self.connectors = connectors

    async def run(self) -> SyncRunResult:
        return await self.orchestrator.run("fixture-run", self.connectors)


@pytest.fixture
def sync_harness(tmp_path: Path) -> SyncHarness:
    return SyncHarness(tmp_path, (FixtureConnector(),))


@pytest.fixture
def partial_failure_harness(tmp_path: Path) -> SyncHarness:
    return SyncHarness(tmp_path, (FixtureConnector(), BrokenConnector()))
