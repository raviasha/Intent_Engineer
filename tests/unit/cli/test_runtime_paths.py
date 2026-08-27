"""Configured graph-path containment and runtime wiring regressions."""

from __future__ import annotations

import stat
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


def test_initialization_creates_and_runtime_exposes_the_shared_intent_proposal_target(
    tmp_path: Path,
) -> None:
    """Catches a lazily created or separately opened proposal file outside recovery scope."""
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    ledger = project / ".intent/history/intent-proposals.jsonl"

    metadata = ledger.lstat()
    runtime = load_runtime(project)

    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink == 1
    assert ledger.read_bytes() == b""
    assert runtime.intent_proposals.path == ledger
    assert runtime.intent_proposals.bytes() == b""
    assert "intent_proposals" in runtime.transactions.target_names


def test_runtime_loads_a_legacy_initialized_workspace_without_a_proposal_ledger(
    tmp_path: Path,
) -> None:
    """Catches treating the newly introduced empty target as mandatory legacy state."""
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    ledger = project / ".intent/history/intent-proposals.jsonl"
    if ledger.exists():
        ledger.unlink()

    runtime = load_runtime(project)

    assert runtime.intent_proposals.list() == ()
    assert runtime.intent_proposals.bytes() == b""


def test_reinitializing_a_legacy_workspace_creates_only_the_missing_empty_ledger(
    tmp_path: Path,
) -> None:
    """Catches the idempotent initialization path leaving a legacy workspace incomplete."""
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    ledger = project / ".intent/history/intent-proposals.jsonl"
    ledger.unlink()
    graph_before = (project / ".intent/graph.yaml").read_bytes()

    initialize_project(project)

    assert ledger.read_bytes() == b""
    assert (project / ".intent/graph.yaml").read_bytes() == graph_before
