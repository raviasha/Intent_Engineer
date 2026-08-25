"""Assembly of the local CLI runtime from production adapters and services."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.base import Connector, ConnectorError, RawSourceObject, SourceObject
from intent_engineering.capture.git.connector import GitConnector
from intent_engineering.capture.markdown.connector import MarkdownConnector
from intent_engineering.context import ContextProvider
from intent_engineering.core.models import EvidenceRecord, ProjectConfig, ReconciliationCase
from intent_engineering.core.policy.project import ProjectNotInitialized, workspace_path
from intent_engineering.extract.deterministic import DeterministicReasoner
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.yaml.checkpoint_store import YamlCheckpointStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from intent_engineering.sync import SyncOrchestrator


@dataclass(frozen=True)
class Runtime:
    """Configured local services and their canonical production stores."""

    root: Path
    workspace: Path
    config: ProjectConfig
    graph_store: YamlGraphStore
    evidence_store: JsonlEvidenceStore
    case_store: JsonlCaseStore
    checkpoint_store: YamlCheckpointStore
    sync: SyncOrchestrator

    def evidence(self) -> tuple[EvidenceRecord, ...]:
        """Load persisted evidence in append order for read-only CLI projections."""
        return _records(self.workspace / "evidence" / "evidence.jsonl", EvidenceRecord)

    def cases(self) -> tuple[ReconciliationCase, ...]:
        """Return the current durable reconciliation-case versions."""
        return tuple(self.case_store.list())

    def context(self) -> ContextProvider:
        """Build a fresh, conservative context provider from durable state."""
        return ContextProvider(self.graph_store.load(), self.cases(), self.config, self.evidence())


def _records(path: Path, model: type[EvidenceRecord | ReconciliationCase]) -> tuple[Any, ...]:
    if not path.exists():
        return ()
    records: list[Any] = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                records.append(model.model_validate_json(line))
    return tuple(records)


def load_runtime(root: Path) -> Runtime:
    """Locate one initialized workspace and assemble only reviewed local adapters."""
    root = root.resolve()
    workspace = workspace_path(root)
    config_path = workspace / "config.yaml"
    if not config_path.is_file():
        raise ProjectNotInitialized("local project is not initialized")
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("project configuration is invalid")
    config = ProjectConfig.model_validate(cast(dict[str, Any], loaded))
    graph_store = YamlGraphStore(
        workspace / "graph.yaml", history_path=workspace / "history" / "changesets.jsonl"
    )
    evidence_store = JsonlEvidenceStore(workspace / "evidence" / "evidence.jsonl")
    case_store = JsonlCaseStore(workspace / "reconciliation" / "cases.jsonl")
    checkpoint_store = YamlCheckpointStore(workspace / "cache" / "checkpoints.yaml")
    sync = SyncOrchestrator(
        graph_store=graph_store,
        evidence_store=evidence_store,
        checkpoint_store=checkpoint_store,
        case_store=case_store,
        reasoner=DeterministicReasoner(actor=config.local_actor),
    )
    return Runtime(
        root=root,
        workspace=workspace,
        config=config,
        graph_store=graph_store,
        evidence_store=evidence_store,
        case_store=case_store,
        checkpoint_store=checkpoint_store,
        sync=sync,
    )


def resolve_connectors(runtime: Runtime, sources: str) -> tuple[Connector, ...]:
    """Resolve a stable comma-delimited local connector list without provider fallbacks."""
    requested = tuple(item.strip() for item in sources.split(",") if item.strip())
    connectors: list[Connector] = []
    for source in requested:
        if source == "markdown":
            connectors.append(MarkdownConnector(runtime.root, runtime.config))
        elif source == "git":
            connectors.append(GitConnector(runtime.root))
        else:
            connectors.append(_UnavailableConnector(source))
    if not connectors:
        raise ValueError("at least one source is required")
    return tuple(connectors)


class _UnavailableConnector:
    """Represent an unavailable requested connector as an isolated sync failure."""

    def __init__(self, connector_id: str) -> None:
        self.connector_id = connector_id

    async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
        del cursor
        raise ConnectorError("requested connector is unavailable")

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        del object_id, version
        raise ConnectorError("requested connector is unavailable")

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        del raw
        raise ConnectorError("requested connector is unavailable")

    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        del discovered
        return None


def new_run_id() -> str:
    """Return a unique opaque run identifier without embedding project paths."""
    from uuid import uuid4

    return f"run:{uuid4().hex}"
