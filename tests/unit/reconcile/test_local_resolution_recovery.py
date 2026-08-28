"""Crash-only recovery tests for transaction-coordinated local resolution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intent_engineering.core.models import ReconciliationStatus, ResolutionAction
from intent_engineering.reconcile import LocalResolutionService
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.secure import SecureDirectory, SecureFile
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from tests.contract.storage.test_evidence_store_contract import evidence_record
from tests.contract.storage.test_graph_store_contract import graph
from tests.unit.reconcile.test_case_lifecycle import reconciliation_case

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def _files(root: SecureDirectory) -> dict[str, SecureFile]:
    return {
        "graph": root.file("graph.yaml"),
        "history": root.file("history.jsonl"),
        "cases": root.file("cases.jsonl"),
    }


def _coordinator(
    root: SecureDirectory,
    files: dict[str, SecureFile],
    fault_hook: Callable[[str], None] | None = None,
) -> LocalTransactionCoordinator:
    return LocalTransactionCoordinator(
        root.file(".local-transaction.json"),
        files,
        fault_hook=fault_hook,
    )


def _service(
    tmp_path: Path,
    *,
    needs_human: bool = True,
    fault_hook: Callable[[str], None] | None = None,
) -> tuple[LocalResolutionService, dict[str, Path]]:
    root = SecureDirectory.open(tmp_path)
    files = _files(root)
    transactions = _coordinator(root, files, fault_hook)
    graph_store = YamlGraphStore(
        files["graph"],
        history_path=files["history"],
        transactions=transactions,
    )
    graph_store.initialize(graph())
    evidence_store = JsonlEvidenceStore(root.file("evidence.jsonl"))
    evidence = evidence_record(id="ev-1")
    evidence_store.put(evidence)
    case_store = JsonlCaseStore(files["cases"])
    opened = reconciliation_case(
        subject_ref="req-1",
        affected_refs=("req-1",),
        evidence_sides=(
            reconciliation_case()
            .evidence_sides[0]
            .model_copy(update={"evidence_refs": (evidence.id,)}),
        ),
    )
    case_store.put(opened)
    if needs_human:
        case_store.put(transition_case(opened, ReconciliationStatus.PROPOSED, "tester", NOW))
        case_store.put(
            transition_case(
                case_store.get(opened.id),
                ReconciliationStatus.NEEDS_HUMAN,
                "tester",
                NOW,
            )
        )
    service = LocalResolutionService(
        graph_store,
        evidence_store,
        case_store,
        "tester",
        transactions=transactions,
    )
    return service, {name: secure_file.path for name, secure_file in files.items()}


def _recover(tmp_path: Path) -> LocalResolutionService:
    root = SecureDirectory.open(tmp_path)
    files = _files(root)
    transactions = _coordinator(root, files)
    transactions.recover()
    return LocalResolutionService(
        YamlGraphStore(
            files["graph"],
            history_path=files["history"],
            transactions=transactions,
        ),
        JsonlEvidenceStore(root.file("evidence.jsonl")),
        JsonlCaseStore(files["cases"]),
        "tester",
        transactions=transactions,
    )


@pytest.mark.parametrize("stage", ("target:graph", "target:history", "target:cases"))
def test_system_exit_after_each_durable_resolution_stage_recovers_exact_preimage(
    tmp_path: Path,
    stage: str,
) -> None:
    def crash(current: str) -> None:
        if current == stage:
            raise SystemExit()

    service, paths = _service(tmp_path, fault_hook=crash)
    before = {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    }
    case = service._case_store.get("case-1")
    graph_version = service._graph_store.load().version
    canonical = service._canonical_changeset(
        case,
        graph_version,
        ResolutionAction.UPDATE_IMPLEMENTATION,
    )
    approval = service._approval_hash(
        case,
        graph_version,
        ResolutionAction.UPDATE_IMPLEMENTATION,
        canonical,
    )

    with pytest.raises(SystemExit):
        service.resolve(
            "case-1",
            ResolutionAction.UPDATE_IMPLEMENTATION,
            approve=approval,
        )

    assert service._journal_path().exists()
    fresh = _recover(tmp_path)
    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert not fresh._journal_path().exists()
    fresh.recover()


def test_system_exit_during_preview_case_append_recovers_exact_preimage(
    tmp_path: Path,
) -> None:
    def crash(stage: str) -> None:
        if stage == "target:cases":
            raise SystemExit()

    service, paths = _service(tmp_path, needs_human=False, fault_hook=crash)
    before = {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    }

    with pytest.raises(SystemExit):
        service.resolve("case-1", ResolutionAction.UPDATE_IMPLEMENTATION)

    assert service._journal_path().exists()
    fresh = _recover(tmp_path)
    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert not fresh._journal_path().exists()
