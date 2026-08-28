"""Every public ChangeSet group must commit through one local transaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from intent_engineering.core.models import (
    ReconciliationCase,
    ReconciliationStatus,
    ResolutionAction,
)
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.executor import CaseEffectMismatch, LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from tests.contract.storage.test_graph_store_contract import NOW, changeset, graph
from tests.unit.reconcile.test_case_lifecycle import reconciliation_case


def _executor(
    tmp_path: Path,
) -> tuple[LocalChangeSetExecutor, YamlGraphStore, JsonlCaseStore, dict[str, Path]]:
    root = SecureDirectory.open(tmp_path)
    files = {
        "graph": root.file("graph.yaml"),
        "history": root.file("history.jsonl"),
        "cases": root.file("cases.jsonl"),
    }
    transactions = LocalTransactionCoordinator(
        root.file(".local-transaction.json"),
        files,
    )
    graph_store = YamlGraphStore(
        files["graph"],
        history_path=files["history"],
        transactions=transactions,
    )
    graph_store.initialize(graph())
    case_store = JsonlCaseStore(files["cases"])
    executor = LocalChangeSetExecutor(graph_store, case_store, transactions)
    return (
        executor,
        graph_store,
        case_store,
        {name: secure_file.path for name, secure_file in files.items()},
    )


def _case() -> ReconciliationCase:
    return reconciliation_case(subject_ref="req-1", affected_refs=("req-1",))


def test_executor_commits_case_creation_graph_version_and_full_history(tmp_path: Path) -> None:
    executor, graph_store, case_store, _ = _executor(tmp_path)
    case = _case()
    mutation = changeset(
        evidence_refs=case.all_evidence_refs,
        reconciliation_cases_created=(case.id,),
    )

    result = executor.apply(mutation, created_cases=(case,))

    assert result.version == 5
    assert case_store.get(case.id) == case
    assert graph_store.history(case.id) == (mutation,)


def test_executor_commits_case_resolution_as_declared_effect(tmp_path: Path) -> None:
    executor, graph_store, case_store, _ = _executor(tmp_path)
    opened = _case()
    proposed = transition_case(opened, ReconciliationStatus.PROPOSED, "reviewer", NOW)
    needs_human = transition_case(proposed, ReconciliationStatus.NEEDS_HUMAN, "reviewer", NOW)
    case_store.put(opened)
    case_store.put(proposed)
    case_store.put(needs_human)
    mutation = changeset(
        id="cs-resolve",
        evidence_refs=needs_human.all_evidence_refs,
        reconciliation_cases_resolved=(opened.id,),
    )
    resolved = transition_case(
        needs_human,
        ReconciliationStatus.RESOLVED,
        "reviewer",
        NOW,
        resolution=ResolutionAction.UPDATE_IMPLEMENTATION,
        changeset_id=mutation.id,
    )

    result = executor.apply(mutation, resolved_cases=(resolved,))

    assert result.version == 5
    assert case_store.get(opened.id) == resolved
    assert graph_store.history(opened.id) == (mutation,)


def test_executor_rejects_missing_case_payload_before_any_mutation(tmp_path: Path) -> None:
    executor, _, _, paths = _executor(tmp_path)
    mutation = changeset(reconciliation_cases_created=("case-1",))
    before = {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    }

    with pytest.raises(CaseEffectMismatch, match="case effects do not match ChangeSet"):
        executor.apply(mutation)

    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
