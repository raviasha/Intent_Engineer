"""Safe initialization rules for the local ``.intent`` workspace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from intent_engineering.core.models import Graph, ProjectConfig
from intent_engineering.storage._atomic import atomic_write_bytes


class ProjectNotInitialized(RuntimeError):
    """Raised when a command needs a local workspace that does not exist."""


class ProjectAlreadyInitialized(RuntimeError):
    """Raised when initialization would overwrite a local workspace path."""


@dataclass(frozen=True)
class InitializedProject:
    """The canonical paths produced by one successful initialization."""

    root: Path
    workspace: Path
    config_path: Path
    graph_path: Path


def workspace_path(root: Path) -> Path:
    """Return the normalized workspace root for a supplied project path."""
    return root.resolve() / ".intent"


def _yaml_bytes(value: dict[str, Any]) -> bytes:
    return cast(str, yaml.safe_dump(value, allow_unicode=True, sort_keys=True)).encode("utf-8")


def _initial_graph(project_id: str) -> Graph:
    return Graph(
        id=f"graph:{project_id}",
        version=0,
        name=project_id,
        purpose="Local evidence-backed intent graph",
        nodes=(),
        edges=(),
    )


def initialize_project(root: Path, *, force: bool = False) -> InitializedProject:
    """Create local state only, refusing every existing target unless forced."""
    root = root.resolve()
    workspace = workspace_path(root)
    config_path = workspace / "config.yaml"
    graph_path = workspace / "graph.yaml"
    directories = tuple(
        workspace / name for name in ("evidence", "reconciliation", "history", "approvals", "cache")
    )
    targets = (config_path, graph_path, *directories)
    if not force:
        existing = next((target for target in targets if target.exists()), None)
        if existing is not None:
            raise ProjectAlreadyInitialized("local workspace already contains initialized paths")
    workspace.mkdir(parents=True, exist_ok=True)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    config = ProjectConfig(
        project_id=root.name or "project",
        local_actor="local",
        source_exclusions=(".intent/**", ".git/**"),
    )
    graph = _initial_graph(config.project_id)
    atomic_write_bytes(config_path, _yaml_bytes(config.model_dump(mode="json")))
    atomic_write_bytes(graph_path, _yaml_bytes(graph.model_dump(mode="json", by_alias=True)))
    return InitializedProject(root, workspace, config_path, graph_path)
