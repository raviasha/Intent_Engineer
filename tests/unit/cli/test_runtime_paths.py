"""Configured graph-path containment and runtime wiring regressions."""

from __future__ import annotations

import multiprocessing
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import ProjectAlreadyInitialized, initialize_project


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


def test_initialization_creates_and_runtime_exposes_webauthn_ledger_targets(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    credentials = project / ".intent/approvals/webauthn-credentials.jsonl"
    challenges = project / ".intent/approvals/webauthn-challenges.jsonl"

    runtime = load_runtime(project)

    assert credentials.read_bytes() == b""
    assert challenges.read_bytes() == b""
    assert runtime.webauthn_credentials.list() == ()
    assert "webauthn_credentials" in runtime.transactions.target_names
    assert "webauthn_challenges" in runtime.transactions.target_names


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


def _force_initialize_probe(project: str, connection: Any) -> None:
    try:
        initialize_project(Path(project), force=True)
    except ProjectAlreadyInitialized as error:
        result = (
            "fixed-unsafe"
            if error.args == ("local workspace has an unsafe path",)
            and error.__context__ is None
            else "unsafe-error"
        )
    except Exception:  # noqa: BLE001 - child reports any unexpected public failure
        result = "unexpected-error"
    else:
        result = "accepted"
    connection.send(result)
    connection.close()


def test_force_initialization_rejects_intent_proposal_fifo_without_blocking_or_replacement(
    tmp_path: Path,
) -> None:
    """Catches force-state inspection blocking on or replacing a proposal-ledger FIFO."""
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    ledger = project / ".intent/history/intent-proposals.jsonl"
    ledger.unlink()
    os.mkfifo(ledger)
    (project / ".intent/graph.yaml").write_bytes(b"graph: [")
    before = os.lstat(ledger)
    context = multiprocessing.get_context("fork")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(target=_force_initialize_probe, args=(str(project), sending))
    process.start()
    sending.close()
    process.join(1.0)
    result = "blocked"
    if process.is_alive():
        process.terminate()
        process.join()
    elif receiving.poll():
        result = receiving.recv()
    receiving.close()

    after = os.lstat(ledger)
    assert result == "fixed-unsafe"
    assert stat.S_ISFIFO(after.st_mode)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert (project / ".intent/graph.yaml").read_bytes() == b"graph: ["
