"""Contract tests for durable optimistic connector checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Thread

import pytest

from intent_engineering.core.models import SyncCheckpoint
from intent_engineering.storage.yaml.checkpoint_store import StaleCheckpoint, YamlCheckpointStore

FIRST = datetime(2026, 8, 25, tzinfo=UTC)
SECOND = datetime(2026, 8, 26, tzinfo=UTC)


def test_checkpoint_compare_and_set_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.yaml"
    store = YamlCheckpointStore(path)

    committed = store.compare_and_set("git", None, "abc", FIRST)

    assert committed == SyncCheckpoint(connector_id="git", cursor="abc", committed_at=FIRST)
    assert YamlCheckpointStore(path).get("git") == committed


def test_checkpoint_compare_and_set_rejects_stale_expected_value(tmp_path: Path) -> None:
    store = YamlCheckpointStore(tmp_path / "checkpoints.yaml")
    first = store.compare_and_set("git", None, "abc", FIRST)

    with pytest.raises(StaleCheckpoint, match="git"):
        store.compare_and_set("git", None, "def", SECOND)

    assert store.get("git") == first


def test_checkpoint_compare_and_set_advances_matching_checkpoint(tmp_path: Path) -> None:
    store = YamlCheckpointStore(tmp_path / "checkpoints.yaml")
    first = store.compare_and_set("git", None, "abc", FIRST)

    second = store.compare_and_set("git", first, "def", SECOND)

    assert second == SyncCheckpoint(connector_id="git", cursor="def", committed_at=SECOND)
    assert store.get("git") == second


class SnapshotBarrierCheckpointStore(YamlCheckpointStore):
    """Force the unlocked implementation to read a shared stale checkpoint."""

    def __init__(self, path: Path, barrier: Barrier) -> None:
        super().__init__(path)
        self._barrier = barrier

    def _load_all(self) -> dict[str, SyncCheckpoint]:
        result = super()._load_all()
        self._barrier.wait()
        return result


def test_matching_checkpoint_cas_callers_do_not_both_succeed(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.yaml"
    snapshot_barrier = Barrier(2)
    start_barrier = Barrier(3)
    results: list[SyncCheckpoint | StaleCheckpoint] = []

    def writer(cursor: str) -> None:
        store = SnapshotBarrierCheckpointStore(path, snapshot_barrier)
        start_barrier.wait()
        try:
            results.append(store.compare_and_set("git", None, cursor, FIRST))
        except StaleCheckpoint as error:
            results.append(error)

    first = Thread(target=writer, args=("abc",))
    second = Thread(target=writer, args=("def",))
    first.start()
    second.start()
    start_barrier.wait()
    first.join()
    second.join()

    successful = [result for result in results if isinstance(result, SyncCheckpoint)]
    failures = [result for result in results if isinstance(result, StaleCheckpoint)]

    assert len(successful) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], StaleCheckpoint)
    assert YamlCheckpointStore(path).get("git") in successful
