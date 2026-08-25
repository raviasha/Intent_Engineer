"""Configured graph-path containment and runtime wiring regressions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project


def _configure_graph_path(project: Path, graph_path: str) -> None:
    config_path = project / ".intent" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["graph_path"] = graph_path
    config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")


def test_runtime_honors_a_contained_configured_graph_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    state = project / ".intent" / "state"
    state.mkdir()
    custom = state / "canonical.yaml"
    (project / ".intent" / "graph.yaml").replace(custom)
    _configure_graph_path(project, ".intent/state/canonical.yaml")

    runtime = load_runtime(project)

    assert runtime.graph_store.path == custom
    assert runtime.graph_store.load().id == f"graph:{project.name}"


@pytest.mark.parametrize("graph_path", ["/tmp/outside.yaml", "../outside.yaml", ".intent/../x"])
def test_runtime_rejects_escaped_configured_graph_paths(
    tmp_path: Path,
    graph_path: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    _configure_graph_path(project, graph_path)

    with pytest.raises(ValueError, match="configured graph path"):
        load_runtime(project)


def test_runtime_rejects_symlinked_configured_graph_components(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".intent" / "linked").symlink_to(outside, target_is_directory=True)
    _configure_graph_path(project, ".intent/linked/graph.yaml")

    with pytest.raises(ValueError, match="configured graph path"):
        load_runtime(project)
