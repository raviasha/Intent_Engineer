"""Assembly of the local CLI runtime from production adapters and services."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.base import Connector
from intent_engineering.capture.git.connector import GitConnector
from intent_engineering.capture.markdown.connector import MarkdownConnector
from intent_engineering.context import ContextProvider
from intent_engineering.core.models import (
    CandidateAssertion,
    DriftObservation,
    EvidenceDelta,
    EvidenceRecord,
    Graph,
    ProjectConfig,
    ReconciliationCase,
)
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.core.policy.project import ProjectNotInitialized, workspace_path
from intent_engineering.extract.deterministic import DeterministicReasoner
from intent_engineering.reconcile import DetectionInput, LocalResolutionService, detect_drift
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.yaml.checkpoint_store import YamlCheckpointStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from intent_engineering.sync import SyncOrchestrator


def _front_matter(content: str) -> Mapping[str, Any] | None:
    """Read strict YAML metadata owned by the local fixture convention."""
    if not content.startswith("---\n"):
        return None
    closing = content.find("\n---\n", 4)
    if closing < 0:
        return None
    loaded = yaml.safe_load(content[4:closing])
    if not isinstance(loaded, Mapping):
        return None
    metadata = loaded.get("intent_engineering")
    return cast(Mapping[str, Any], metadata) if isinstance(metadata, Mapping) else None


def _detection_input(record: EvidenceRecord) -> DetectionInput | None:
    """Validate one top-level or front-matter detector fixture against the Task 5 model."""
    raw = record.payload.get("detection_input")
    if raw is None:
        content = record.payload.get("content")
        metadata = _front_matter(content) if isinstance(content, str) else None
        raw = metadata.get("detection_input") if metadata is not None else None
    if not isinstance(raw, Mapping):
        return None
    payload = dict(cast(Mapping[str, Any], raw))
    for side_name in ("requirement", "implementation", "test", "decision"):
        side = payload.get(side_name)
        if not isinstance(side, Mapping):
            continue
        copied_side = dict(cast(Mapping[str, Any], side))
        references = copied_side.get("evidence_refs")
        if isinstance(references, Sequence) and not isinstance(references, str):
            if any(item != "$self" for item in references):
                return None
            copied_side["evidence_refs"] = [
                record.id if item == "$self" else item for item in references
            ]
        payload[side_name] = copied_side
    return DetectionInput.model_validate(payload)


def _detect_cases(delta: EvidenceDelta, graph: Graph, actor: str) -> Sequence[DriftObservation]:
    """Project fixture evidence into deterministic Task 5 observations without graph mutation."""
    del graph
    observations: list[DriftObservation] = []
    for record in delta.added:
        if not refs_allowed((record.id,), (record,), actor):
            continue
        detection_input = _detection_input(record)
        if detection_input is not None:
            observations.extend(detect_drift(detection_input))
    return tuple(observations)


class _FrontMatterReasoner(DeterministicReasoner):
    """Expose approved Markdown fixture metadata at the existing reasoner boundary."""

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        normalized = tuple(self._normalized(record) for record in delta.added)
        return super().extract_assertions(delta.model_copy(update={"added": normalized}))

    def _normalized(self, record: EvidenceRecord) -> EvidenceRecord:
        content = record.payload.get("content")
        metadata = _front_matter(content) if isinstance(content, str) else None
        payload = dict(record.payload)
        if metadata is not None:
            for key in ("intent_assertion", "detection_input"):
                if key not in payload and key in metadata:
                    payload[key] = metadata[key]
        assertion = payload.get("intent_assertion")
        if isinstance(assertion, Mapping):
            copied = dict(assertion)
            references = copied.get("evidence_refs")
            valid_refs = (
                isinstance(references, Sequence)
                and not isinstance(references, str)
                and len(references) == 1
                and references[0] in {"$self", record.id}
            )
            if not valid_refs or not refs_allowed((record.id,), (record,), self._actor):
                payload.pop("intent_assertion", None)
                return record.model_copy(update={"payload": payload})
            copied["evidence_refs"] = [record.id]
            payload["intent_assertion"] = copied
        return record.model_copy(update={"payload": payload})


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
    resolution: LocalResolutionService

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
        reasoner=_FrontMatterReasoner(actor=config.local_actor),
        case_detector=lambda delta, graph: _detect_cases(delta, graph, config.local_actor),
    )
    resolution = LocalResolutionService(graph_store, evidence_store, case_store, config.local_actor)
    resolution.recover()
    return Runtime(
        root=root,
        workspace=workspace,
        config=config,
        graph_store=graph_store,
        evidence_store=evidence_store,
        case_store=case_store,
        checkpoint_store=checkpoint_store,
        sync=sync,
        resolution=resolution,
    )


def resolve_connectors(runtime: Runtime, sources: str) -> tuple[Connector, ...]:
    """Resolve a stable comma-delimited local connector list without provider fallbacks."""
    requested = parse_sources(sources)
    connectors: list[Connector] = []
    for source in requested:
        if source == "markdown":
            connectors.append(MarkdownConnector(runtime.root, runtime.config))
        elif source == "git":
            connectors.append(GitConnector(runtime.root))
        else:  # pragma: no cover - parse_sources establishes this boundary
            raise AssertionError(source)
    return tuple(connectors)


def parse_sources(sources: str) -> tuple[str, ...]:
    """Validate a connector selection before the command crosses into AnyIO."""
    requested = tuple(item.strip() for item in sources.split(","))
    if not requested or any(not item for item in requested):
        raise ValueError("sources must name one or more connectors")
    if len(requested) != len(set(requested)):
        raise ValueError("sources must not contain duplicates")
    unknown = tuple(item for item in requested if item not in {"markdown", "git"})
    if unknown:
        raise ValueError("sources must be markdown and/or git")
    return requested


def new_run_id() -> str:
    """Return a unique opaque run identifier without embedding project paths."""
    from uuid import uuid4

    return f"run:{uuid4().hex}"
