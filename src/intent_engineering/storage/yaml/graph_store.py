"""Canonical YAML graph storage backed by append-only ChangeSet history."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from intent_engineering.core.graph.applier import apply_changeset
from intent_engineering.core.models import ChangeSet, Graph
from intent_engineering.storage._atomic import atomic_write_bytes, same_path_lock
from intent_engineering.storage.jsonl.history_store import JsonlHistoryStore
from intent_engineering.storage.secure import SecureFile, coerce_secure_file


def _yaml_bytes(graph: Graph) -> bytes:
    data = graph.model_dump(mode="json", by_alias=True)
    return cast(str, yaml.safe_dump(data, allow_unicode=True, sort_keys=True)).encode("utf-8")


def _canonical_graph_payload(loaded: dict[str, Any]) -> dict[str, Any]:
    """Normalize the supplied starter graph shape to the canonical model payload."""
    starter_graph = loaded.get("graph")
    if not isinstance(starter_graph, dict):
        return loaded

    payload = dict(starter_graph)
    semantic_version = payload.pop("version", "0.1.0")
    payload["schema_version"] = str(semantic_version)
    payload["version"] = 0
    payload["nodes"] = loaded.get("nodes", ())
    payload["edges"] = loaded.get("edges", ())
    return payload


class YamlGraphStore:
    """Atomically replace canonical graph YAML before recording applied history."""

    def __init__(
        self,
        path: Path | SecureFile,
        *,
        history_path: Path | SecureFile | None = None,
    ) -> None:
        self._file = coerce_secure_file(path)
        self.path = self._file.path
        resolved_history_path = history_path or self._file.sibling(
            f"{self.path.stem}.history.jsonl"
        )
        self._history_store = JsonlHistoryStore(resolved_history_path)

    def initialize(self, graph: Graph) -> None:
        """Durably establish canonical graph state."""
        with same_path_lock(self._file):
            atomic_write_bytes(self._file, _yaml_bytes(graph))

    def _load_unlocked(self) -> Graph:
        """Load graph state while the caller holds this graph's path lock."""
        loaded = yaml.safe_load(self._file.read_bytes().decode("utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("graph YAML must contain a mapping")
        return Graph.model_validate(_canonical_graph_payload(cast(dict[str, Any], loaded)))

    def load(self) -> Graph:
        """Load and fully validate canonical graph YAML."""
        with same_path_lock(self._file):
            return self._load_unlocked()

    def apply(self, changeset: ChangeSet) -> Graph:
        """Validate, replace canonical state atomically, then append history."""
        with same_path_lock(self._file):
            next_graph = apply_changeset(self._load_unlocked(), changeset)
            next_graph.assert_invariants()
            atomic_write_bytes(self._file, _yaml_bytes(next_graph))
            self._history_store.append(changeset)
            return next_graph

    def history(self, subject_id: str) -> Sequence[ChangeSet]:
        """Return durable ChangeSets involving a graph subject."""
        return self._history_store.history(subject_id)
