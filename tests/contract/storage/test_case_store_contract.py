"""Contract tests for append-only reconciliation-case persistence."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from intent_engineering.core.models import ReconciliationCase, ReconciliationStatus
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.interfaces import CaseStore
from intent_engineering.storage.jsonl.case_store import (
    CaseStoreError,
    ConflictingCaseFingerprint,
    ConflictingCaseId,
    JsonlCaseStore,
)
from tests.unit.reconcile.builders import NOW
from tests.unit.reconcile.test_case_lifecycle import reconciliation_case


def assert_case_store_contract(store: CaseStore, case: ReconciliationCase) -> None:
    assert store.put(case) is True
    assert store.put(case) is False
    assert store.get(case.id) == case
    assert store.find_by_fingerprint(case.fingerprint) == case
    assert store.list() == (case,)
    assert store.list(ReconciliationStatus.OPEN) == (case,)


def test_jsonl_case_store_satisfies_idempotency_contract(tmp_path: Path) -> None:
    assert_case_store_contract(JsonlCaseStore(tmp_path / "cases.jsonl"), reconciliation_case())


def test_case_store_appends_lifecycle_versions_and_reads_latest(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    store = JsonlCaseStore(path)
    opened = reconciliation_case()
    proposed = transition_case(opened, ReconciliationStatus.PROPOSED, "reviewer", NOW)

    assert store.put(opened) is True
    assert store.put(proposed) is True
    assert store.get(opened.id) == proposed
    assert JsonlCaseStore(path).list(ReconciliationStatus.PROPOSED) == (proposed,)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_case_store_rejects_changed_identity_and_reused_fingerprint(tmp_path: Path) -> None:
    store = JsonlCaseStore(tmp_path / "cases.jsonl")
    case = reconciliation_case()
    store.put(case)

    with pytest.raises(ConflictingCaseId, match=case.id):
        store.put(case.model_copy(update={"subject_ref": "other-requirement"}))
    with pytest.raises(ConflictingCaseFingerprint, match=case.fingerprint):
        store.put(case.model_copy(update={"id": "case-2"}))


def test_case_store_rejects_model_copy_that_bypasses_lifecycle_validation(tmp_path: Path) -> None:
    store = JsonlCaseStore(tmp_path / "cases.jsonl")
    invalid = reconciliation_case().model_copy(update={"status": ReconciliationStatus.NEEDS_HUMAN})

    with pytest.raises(CaseStoreError, match="invalid reconciliation case"):
        store.put(invalid)


def test_case_store_rejects_blank_or_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(" \n", encoding="utf-8")

    with pytest.raises(CaseStoreError, match="blank case record at line 1"):
        JsonlCaseStore(path)


def _concurrent_case_put(path: str, case: ReconciliationCase, barrier: object, results: object) -> None:
    store = JsonlCaseStore(Path(path))
    barrier.wait()  # type: ignore[union-attr]
    try:
        results.put(("returned", store.put(case)))  # type: ignore[union-attr]
    except (ConflictingCaseId, ConflictingCaseFingerprint):
        results.put(("conflict", case.id))  # type: ignore[union-attr]


def _concurrent_puts(path: Path, cases: tuple[ReconciliationCase, ReconciliationCase]) -> list[tuple[str, object]]:
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_concurrent_case_put, args=(str(path), case, barrier, results))
        for case in cases
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()
        assert process.exitcode == 0
    output = [results.get() for _ in cases]
    results.close()
    return output


def test_store_instances_dedupe_same_case_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    case = reconciliation_case()

    assert sorted(_concurrent_puts(path, (case, case))) == [("returned", False), ("returned", True)]
    assert JsonlCaseStore(path).list() == (case,)


def test_store_instances_never_append_an_outdated_lifecycle_version_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    opened = reconciliation_case()
    proposed = transition_case(opened, ReconciliationStatus.PROPOSED, "reviewer", NOW)

    results = _concurrent_puts(path, (opened, proposed))

    assert results in (
        [("returned", True), ("returned", True)],
        [("returned", True), ("conflict", opened.id)],
        [("conflict", opened.id), ("returned", True)],
    )
    assert JsonlCaseStore(path).get(opened.id).status is ReconciliationStatus.PROPOSED
