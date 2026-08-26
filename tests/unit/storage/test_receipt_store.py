"""Integrity contracts for durable at-most-once execution claims."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import traceback
from pathlib import Path
from threading import Barrier, Thread

import pytest
from pydantic import ValidationError

from intent_engineering.mutations.models import ExecutionReceipt, receipt_id
from intent_engineering.storage.jsonl.receipt_store import (
    JsonlReceiptStore,
    ReceiptStoreError,
)
from intent_engineering.storage.secure import UnsafePathError
from tests.unit.mutations.test_approval import _approve
from tests.unit.mutations.test_planner import NOW, base_plan


def _claim_after_barrier(
    path: str,
    plan_id: str,
    approval_id: str,
    barrier: object,
    results: object,
) -> None:
    store = JsonlReceiptStore(Path(path))
    barrier.wait()  # type: ignore[union-attr]
    results.put(  # type: ignore[union-attr]
        store.claim(plan_id, approval_id, "local:reviewer", NOW)
    )


def _successful_receipt_payload() -> dict[str, object]:
    plan = base_plan()
    approval = _approve(plan)
    material: dict[str, object] = {
        "schema_version": 1,
        "plan_id": plan.id,
        "plan_hash": plan.canonical_hash,
        "approval_id": approval.id,
        "target_version": plan.before_version,
        "executed_by": "local:reviewer",
        "status": "succeeded",
        "attempted_at": "2026-08-26T12:00:00Z",
        "completed_at": "2026-08-26T12:00:00Z",
        "resulting_version": "v2",
        "evidence_ref": "evidence:mcp-write:" + "a" * 64,
        "redacted_error": None,
    }
    return {**material, "id": receipt_id(material)}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resulting_version", " "),
        ("evidence_ref", "evidence:unbound"),
    ],
)
def test_success_receipt_requires_canonical_result_and_write_evidence_identity(
    field: str,
    value: str,
) -> None:
    payload = _successful_receipt_payload()
    payload[field] = value
    material = {key: item for key, item in payload.items() if key != "id"}
    payload["id"] = receipt_id(material)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        ExecutionReceipt.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plan_id", "PRIVATE-INVALID-PLAN-ID"),
        ("approval_id", "PRIVATE-INVALID-APPROVAL-ID"),
        ("actor", "PRIVATE\x00ACTOR"),
        ("claimed_at", NOW.replace(tzinfo=None)),
    ],
)
def test_claim_rejects_noncanonical_fields_without_retaining_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    plan = base_plan()
    approval = _approve(plan)
    values = {
        "plan_id": plan.id,
        "approval_id": approval.id,
        "actor": "local:reviewer",
        "claimed_at": NOW,
    }
    values[field] = value
    store = JsonlReceiptStore(tmp_path / "receipts.jsonl")

    with pytest.raises(ReceiptStoreError) as caught:
        store.claim(**values)  # type: ignore[arg-type]

    rendered = traceback.TracebackException.from_exception(
        caught.value,
        capture_locals=True,
    )
    repository_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value.args == ("invalid execution claim",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "PRIVATE-INVALID" not in repository_locals
    assert "PRIVATE\\x00ACTOR" not in repository_locals
    assert store.list() == ()


def test_two_store_instances_can_create_only_one_durable_claim(tmp_path: Path) -> None:
    plan = base_plan()
    approval = _approve(plan)
    path = tmp_path / "receipts.jsonl"
    stores = (JsonlReceiptStore(path), JsonlReceiptStore(path))
    barrier = Barrier(3)
    results: list[bool] = []

    def claim(store: JsonlReceiptStore) -> None:
        barrier.wait()
        results.append(store.claim(plan.id, approval.id, "local:reviewer", NOW))

    threads = tuple(Thread(target=claim, args=(store,)) for store in stores)
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
    assert JsonlReceiptStore(path).is_claimed(plan.id, approval.id)


def test_first_claim_fsyncs_the_parent_directory_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = base_plan()
    approval = _approve(plan)
    store = JsonlReceiptStore(tmp_path / "receipts.jsonl")
    kinds: list[str] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        kinds.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    assert store.claim(plan.id, approval.id, "local:reviewer", NOW)
    assert kinds[-2:] == ["file", "directory"]


def test_two_processes_can_create_only_one_durable_claim(tmp_path: Path) -> None:
    plan = base_plan()
    approval = _approve(plan)
    path = tmp_path / "receipts.jsonl"
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = tuple(
        context.Process(
            target=_claim_after_barrier,
            args=(str(path), plan.id, approval.id, barrier, results),
        )
        for _ in range(2)
    )

    for process in processes:
        process.start()
    for process in processes:
        process.join()
        assert process.exitcode == 0
    output = sorted(results.get() for _ in processes)
    results.close()

    assert output == [False, True]
    assert JsonlReceiptStore(path).is_claimed(plan.id, approval.id)


def test_receipt_store_rejects_symlink_hardlink_and_noncanonical_records(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"")
    symlink = tmp_path / "symlink.jsonl"
    symlink.symlink_to(outside)
    with pytest.raises(UnsafePathError):
        JsonlReceiptStore(symlink)
    hardlink = tmp_path / "hardlink.jsonl"
    os.link(outside, hardlink)
    with pytest.raises(UnsafePathError):
        JsonlReceiptStore(hardlink)

    plan = base_plan()
    approval = _approve(plan)
    path = tmp_path / "receipts.jsonl"
    store = JsonlReceiptStore(path)
    assert store.claim(plan.id, approval.id, "local:reviewer", NOW)
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ReceiptStoreError, match="invalid receipt store"):
        JsonlReceiptStore(path)
