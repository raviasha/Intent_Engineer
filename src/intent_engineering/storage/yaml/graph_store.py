"""Canonical YAML graph storage backed by append-only ChangeSet history."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from intent_engineering.core.graph.applier import apply_changeset
from intent_engineering.core.models import ChangeSet, Graph
from intent_engineering.storage._atomic import atomic_write_bytes
from intent_engineering.storage.jsonl.history_store import JsonlHistoryStore


def _yaml_bytes(graph: Graph) -> bytes:
    data = graph.model_dump(mode="json", by_alias=True)
    return cast(str, yaml.safe_dump(data, allow_unicode=True, sort_keys=True)).encode("utf-8")


class YamlGraphStore:
    """Atomically replace canonical graph YAML before recording applied history."""

    def __init__(self, path: Path, *, history_path: Path | None = None) -> None:
        self.path = path
        resolved_history_path = history_path or path.with_suffix(".history.jsonl")
        self._history_store = JsonlHistoryStore(resolved_history_path)

    def initialize(self, graph: Graph) -> None:
        """Durably establish canonical graph state."""
        atomic_write_bytes(self.path, _yaml_bytes(graph))

    def load(self) -> Graph:
        """Load and fully validate canonical graph YAML."""
        loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError(f"graph YAML must contain a mapping: {self.path}")
        return Graph.model_validate(cast(dict[str, Any], loaded))

    def apply(self, changeset: ChangeSet) -> Graph:
        """Validate, replace canonical state atomically, then append history."""
        next_graph = apply_changeset(self.load(), changeset)
        next_graph.assert_invariants()
        atomic_write_bytes(self.path, _yaml_bytes(next_graph))
        self._history_store.append(changeset)
        return next_graph

    def history(self, subject_id: str) -> Sequence[ChangeSet]:
        """Return durable ChangeSets involving a graph subject."""
        return self._history_store.history(subject_id)
