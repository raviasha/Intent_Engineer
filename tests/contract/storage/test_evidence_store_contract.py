"""Reusable contract tests for immutable evidence storage."""

from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intent_engineering.core.models import EvidenceRecord
from intent_engineering.storage.interfaces import EvidenceStore
from intent_engineering.storage.jsonl.evidence_store import (
    ConflictingEvidenceId,
    JsonlEvidenceStore,
)

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def evidence_record(**changes: object) -> EvidenceRecord:
    payload: dict[str, object] = {
        "id": "ev-git-1",
        "connector_type": "git",
        "external_object_id": "commit:abc",
        "external_version": "abc",
        "author": "developer@example.com",
        "observed_at": NOW,
        "source_locator": "git:abc",
        "content_hash": "sha256:1234",
        "payload": {"message": "Add local export", "files": ["README.md"]},
    }
    payload.update(changes)
    return EvidenceRecord(**payload)


def assert_evidence_store_is_idempotent(store: EvidenceStore, record: EvidenceRecord) -> None:
    assert store.put(record) is True
    assert store.put(record) is False
    assert store.get(record.id) == record
    assert store.versions(record.external_object_id) == (record,)


def test_jsonl_evidence_store_satisfies_idempotency_contract(tmp_path: Path) -> None:
    assert_evidence_store_is_idempotent(JsonlEvidenceStore(tmp_path / "evidence.jsonl"), evidence_record())


def test_evidence_store_rejects_conflicting_reuse_of_an_id(tmp_path: Path) -> None:
    store = JsonlEvidenceStore(tmp_path / "evidence.jsonl")
    record = evidence_record()
    store.put(record)

    with pytest.raises(ConflictingEvidenceId, match="ev-git-1"):
        store.put(record.model_copy(update={"content_hash": "sha256:different"}))


def test_evidence_versions_are_ordered_by_append_history(tmp_path: Path) -> None:
    store = JsonlEvidenceStore(tmp_path / "evidence.jsonl")
    first = evidence_record(id="ev-git-1", external_version="1", content_hash="sha256:first")
    second = evidence_record(id="ev-git-2", external_version="2", content_hash="sha256:second")

    store.put(first)
    store.put(second)
    reloaded = JsonlEvidenceStore(tmp_path / "evidence.jsonl")

    assert reloaded.versions("commit:abc") == (first, second)


def _put_after_barrier(
    path: str,
    record: EvidenceRecord,
    barrier: object,
    results: object,
) -> None:
    store = JsonlEvidenceStore(Path(path))
    barrier.wait()  # type: ignore[union-attr]
    try:
        results.put(("returned", store.put(record)))  # type: ignore[union-attr]
    except ConflictingEvidenceId:
        results.put(("conflict", record.content_hash))  # type: ignore[union-attr]


def concurrent_puts(path: Path, records: tuple[EvidenceRecord, EvidenceRecord]) -> list[tuple[str, object]]:
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_put_after_barrier, args=(str(path), record, barrier, results))
        for record in records
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()
        assert process.exitcode == 0
    output = [results.get() for _ in records]
    results.close()
    return output


def test_store_instances_dedupe_the_same_evidence_id_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "evidence.jsonl"
    record = evidence_record()

    results = concurrent_puts(path, (record, record))

    assert sorted(results) == [("returned", False), ("returned", True)]
    assert JsonlEvidenceStore(path).versions(record.external_object_id) == (record,)


def test_store_instances_reject_conflicting_evidence_id_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "evidence.jsonl"
    first = evidence_record(content_hash="sha256:first")
    conflicting = first.model_copy(update={"content_hash": "sha256:second"})

    results = concurrent_puts(path, (first, conflicting))

    assert {result[0] for result in results} == {"returned", "conflict"}
    reloaded = JsonlEvidenceStore(path)
    assert reloaded.get(first.id) in (first, conflicting)
    assert len(reloaded.versions(first.external_object_id)) == 1
