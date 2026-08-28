"""Runtime startup must recover raw preimages before parsing torn stores."""

from __future__ import annotations

from pathlib import Path

import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator


@pytest.mark.parametrize(
    "legacy_target_names",
    [
        frozenset({"graph", "history", "cases"}),
        frozenset({"graph", "history", "cases", "evidence", "receipts"}),
    ],
)
def test_runtime_recovers_torn_yaml_and_jsonl_before_store_construction(
    tmp_path: Path,
    legacy_target_names: frozenset[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    workspace = SecureDirectory.open(project / ".intent")

    def crash(stage: str) -> None:
        if stage == "target:cases":
            raise SystemExit()

    available_targets = {
        "graph": workspace.file("graph.yaml"),
        "history": workspace.file("history/changesets.jsonl"),
        "cases": workspace.file("reconciliation/cases.jsonl"),
        "evidence": workspace.file("evidence/evidence.jsonl"),
        "receipts": workspace.file("approvals/receipts.jsonl"),
    }
    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        {name: target for name, target in available_targets.items() if name in legacy_target_names},
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
    assert runtime.intent_proposals.list() == ()
