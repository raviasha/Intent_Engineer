"""Contract tests for durable optimistic connector checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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
