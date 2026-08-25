"""Runtime startup must recover raw preimages before parsing torn stores."""

from __future__ import annotations

from pathlib import Path

import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator


def test_runtime_recovers_torn_yaml_and_jsonl_before_store_construction(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    workspace = SecureDirectory.open(project / ".intent")

    def crash(stage: str) -> None:
        if stage == "target:cases":
            raise SystemExit()

    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        {
            "graph": workspace.file("graph.yaml"),
            "history": workspace.file("history/changesets.jsonl"),
            "cases": workspace.file("reconciliation/cases.jsonl"),
        },
        fault_hook=crash,
    )
    with pytest.raises(SystemExit), coordinator.transaction() as transaction:
        transaction.write("graph", b"torn: [")
        transaction.write("history", b'{"torn":')
        transaction.write("cases", b'{"torn":')

    runtime = load_runtime(project)

    assert runtime.graph_store.load().id == f"graph:{project.name}"
    assert runtime.graph_store.history("anything") == ()
    assert runtime.case_store.list() == ()
