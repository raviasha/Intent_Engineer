"""Crash-consistency and untrusted-journal tests for local canonical transactions."""

from __future__ import annotations

import base64
import json
from hashlib import sha256
from pathlib import Path

import pytest

from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import (
    LocalTransactionCoordinator,
    TransactionRecoveryError,
)


def _coordinator(
    tmp_path: Path,
    *,
    fault_hook: object | None = None,
) -> tuple[LocalTransactionCoordinator, dict[str, Path], Path]:
    root = SecureDirectory.open(tmp_path)
    paths = {
        "graph": tmp_path / "graph.yaml",
        "history": tmp_path / "history.jsonl",
        "cases": tmp_path / "cases.jsonl",
    }
    coordinator = LocalTransactionCoordinator(
        root.file(".local-transaction.json"),
        {name: root.file(path.name) for name, path in paths.items()},
        fault_hook=fault_hook,  # type: ignore[arg-type]
    )
    return coordinator, paths, tmp_path / ".local-transaction.json"


def _seed(paths: dict[str, Path]) -> dict[str, bytes | None]:
    paths["graph"].write_bytes(b"graph-before\n")
    paths["history"].write_bytes(b"history-before\n")
    return {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}


def _mutate(coordinator: LocalTransactionCoordinator) -> None:
    with coordinator.transaction() as transaction:
        transaction.write("graph", b"graph-after\n")
        transaction.append("history", b"history-after\n")
        transaction.write("cases", b"cases-after\n")


def test_ordinary_exception_restores_exact_bytes_and_existence(tmp_path: Path) -> None:
    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)

    with pytest.raises(
        RuntimeError,
        match="fixture failure",
    ), coordinator.transaction() as transaction:
        transaction.write("graph", b"changed\n")
        transaction.write("cases", b"created\n")
        raise RuntimeError("fixture failure")

    assert {name: path.read_bytes() if path.exists() else None for name, path in paths.items()} == before
    assert not journal.exists()


@pytest.mark.parametrize(
    "stage",
    ["journal_prepared", "target:graph", "target:history", "target:cases"],
)
def test_crash_after_each_precommit_durable_stage_recovers_exact_preimages(
    tmp_path: Path,
    stage: str,
) -> None:
    def crash(current: str) -> None:
        if current == stage:
            raise SystemExit()

    coordinator, paths, journal = _coordinator(tmp_path, fault_hook=crash)
    before = _seed(paths)

    with pytest.raises(SystemExit):
        _mutate(coordinator)

    assert journal.exists()
    recovery, _, _ = _coordinator(tmp_path)
    recovery.recover()

    assert {name: path.read_bytes() if path.exists() else None for name, path in paths.items()} == before
    assert not journal.exists()
    recovery.recover()


def test_stale_committed_journal_completes_without_replaying_preimages(tmp_path: Path) -> None:
    def crash(stage: str) -> None:
        if stage == "journal_committed":
            raise SystemExit()

    coordinator, paths, journal = _coordinator(tmp_path, fault_hook=crash)
    _seed(paths)

    with pytest.raises(SystemExit):
        _mutate(coordinator)

    after = {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}
    assert after == {
        "graph": b"graph-after\n",
        "history": b"history-before\nhistory-after\n",
        "cases": b"cases-after\n",
    }
    recovery, _, _ = _coordinator(tmp_path)
    recovery.recover()

    assert {name: path.read_bytes() if path.exists() else None for name, path in paths.items()} == after
    assert not journal.exists()


def _preimage(target: str, content: bytes | None) -> dict[str, object]:
    return {
        "target": target,
        "existed": content is not None,
        "content": None if content is None else base64.b64encode(content).decode("ascii"),
        "digest": f"sha256:{sha256(content or b'').hexdigest()}",
    }


def _valid_journal(paths: dict[str, Path]) -> dict[str, object]:
    return {
        "schema": "intent.local_transaction",
        "version": 1,
        "state": "prepared",
        "transaction_id": "a" * 64,
        "preimages": [
            _preimage(name, path.read_bytes() if path.exists() else None)
            for name, path in paths.items()
        ],
    }


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda payload: payload.update({"unknown": True}),
        lambda payload: payload["preimages"].append(payload["preimages"][0]),
        lambda payload: payload["preimages"][0].update({"target": "../graph"}),
        lambda payload: payload["preimages"][0].update({"content": "%%%"}),
        lambda payload: payload["preimages"][0].update({"digest": "sha256:" + "0" * 64}),
    ],
)
def test_corrupt_or_untrusted_journal_is_rejected_without_target_mutation(
    tmp_path: Path,
    corrupt: object,
) -> None:
    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)
    payload = _valid_journal(paths)
    corrupt(payload)  # type: ignore[operator]
    journal.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    with pytest.raises(TransactionRecoveryError, match="local transaction recovery failed"):
        coordinator.recover()

    assert {name: path.read_bytes() if path.exists() else None for name, path in paths.items()} == before
    assert journal.exists()
