"""Reusable contract tests for canonical graph storage."""

from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Thread

import pytest

from intent_engineering.core.graph.applier import StaleGraphVersion, UnknownIdentity
from intent_engineering.core.models import (
    ChangeSet,
    Edge,
    Graph,
    Node,
    NodeType,
    RelationType,
    SourceMode,
)
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.interfaces import GraphStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def node(node_id: str) -> Node:
    return Node(
        id=node_id,
        type=NodeType.REQUIREMENT,
        label="Export is local-first",
        status="active",
        created_by="tester",
        created_at=NOW,
        last_modified_by="tester",
        last_modified_at=NOW,
        source_mode=SourceMode.EXPLICIT,
        evidence_refs=("ev-1",),
    )


def edge(edge_id: str, *, to_id: str = "req-2") -> Edge:
    return Edge(
        id=edge_id,
        from_id="req-1",
        relation=RelationType.VERIFIED_BY,
        to_id=to_id,
        status="active",
        created_by="tester",
        created_at=NOW,
        last_modified_by="tester",
        last_modified_at=NOW,
    )


def graph() -> Graph:
    return Graph(id="graph-1", version=4, nodes=(node("req-1"), node("req-2")), edges=(edge("edge-1"),))


def changeset(**changes: object) -> ChangeSet:
    payload: dict[str, object] = {
        "id": "cs-1",
        "actor": "tester",
        "timestamp": NOW,
        "baseline_graph_version": 4,
        "evidence_refs": ("ev-1",),
        "nodes_added": (),
        "nodes_updated": (),
        "nodes_superseded": (),
        "edges_added": (),
        "edges_updated": (),
        "edges_superseded": (),
        "confidence_changes": (),
        "implementation_status_changes": (),
        "reconciliation_cases_created": (),
        "reconciliation_cases_resolved": (),
        "validation_status": "approved",
    }
    payload.update(changes)
    return ChangeSet(**payload)


def assert_graph_store_round_trip(store: GraphStore, initial: Graph) -> None:
    store.initialize(initial)
    assert store.load() == initial


def test_yaml_graph_store_satisfies_round_trip_contract(tmp_path: Path) -> None:
    assert_graph_store_round_trip(YamlGraphStore(tmp_path / "graph.yaml"), graph())


def test_yaml_graph_store_uses_edge_aliases_and_iso_datetimes(tmp_path: Path) -> None:
    path = tmp_path / "graph.yaml"
    store = YamlGraphStore(path)
    store.initialize(graph())

    text = path.read_text()

    assert "from: req-1" in text
    assert "to: req-2" in text
    assert "from_id:" not in text
    assert "created_at: '2026-08-25T00:00:00Z'" in text


def test_graph_apply_replaces_graph_before_appending_history(tmp_path: Path) -> None:
    store = YamlGraphStore(tmp_path / "graph.yaml", history_path=tmp_path / "history.jsonl")
    store.initialize(graph())
    mutation = changeset(nodes_added=(node("req-3"),))

    result = store.apply(mutation)

    assert result.version == 5
    assert tuple(item.id for item in result.nodes) == ("req-1", "req-2", "req-3")
    assert store.history("req-3") == (mutation,)


def test_failed_apply_preserves_canonical_bytes_and_does_not_append_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "graph.yaml"
    history_path = tmp_path / "history.jsonl"
    store = YamlGraphStore(path, history_path=history_path)
    store.initialize(graph())
    before = path.read_bytes()
    invalid = changeset(edges_added=(edge("edge-invalid", to_id="missing-node"),))

    with pytest.raises(ValueError, match="missing node: missing-node"):
        store.apply(invalid)

    assert path.read_bytes() == before
    assert store.history("missing-node") == ()
    assert history_path.exists() is False


def test_graph_store_rejects_unknown_mutation_identity(tmp_path: Path) -> None:
    store = YamlGraphStore(tmp_path / "graph.yaml")
    store.initialize(graph())

    with pytest.raises(UnknownIdentity, match="missing-node"):
        store.apply(changeset(nodes_superseded=("missing-node",)))


class SnapshotBarrierGraphStore(YamlGraphStore):
    """Force the unlocked implementation to take two identical graph snapshots."""

    def __init__(self, path: Path, barrier: Barrier, *, history_path: Path) -> None:
        super().__init__(path, history_path=history_path)
        self._barrier = barrier

    def load(self) -> Graph:
        result = super().load()
        self._barrier.wait()
        return result


def test_graph_writers_do_not_both_commit_from_the_same_baseline(tmp_path: Path) -> None:
    path = tmp_path / "graph.yaml"
    history_path = tmp_path / "history.jsonl"
    YamlGraphStore(path, history_path=history_path).initialize(graph())
    snapshot_barrier = Barrier(2)
    start_barrier = Barrier(3)
    results: list[Graph | StaleGraphVersion] = []

    def writer(change: ChangeSet) -> None:
        store = SnapshotBarrierGraphStore(path, snapshot_barrier, history_path=history_path)
        start_barrier.wait()
        try:
            results.append(store.apply(change))
        except StaleGraphVersion as error:
            results.append(error)

    first = Thread(
        target=writer,
        args=(changeset(id="cs-graph-1", nodes_added=(node("req-3"),)),),
    )
    second = Thread(
        target=writer,
        args=(changeset(id="cs-graph-2", nodes_added=(node("req-4"),)),),
    )
    first.start()
    second.start()
    start_barrier.wait()
    first.join()
    second.join()

    successful = [result for result in results if isinstance(result, Graph)]
    failures = [result for result in results if isinstance(result, StaleGraphVersion)]

    assert len(successful) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], StaleGraphVersion)
    loaded = YamlGraphStore(path, history_path=history_path).load()
    assert loaded.version == 5
    assert {item.id for item in loaded.nodes} in ({"req-1", "req-2", "req-3"}, {"req-1", "req-2", "req-4"})


def _acquire_nested_same_path_lock(path: str, result: object) -> None:
    with same_path_lock(Path(path)), same_path_lock(Path(path)):
        result.put("entered")  # type: ignore[union-attr]


def test_nested_same_path_lock_completes_without_self_deadlock(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    process = context.Process(target=_acquire_nested_same_path_lock, args=(str(tmp_path / "graph.yaml"), result))

    process.start()
    process.join(timeout=1)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("nested same-path lock acquisition self-deadlocked")

    assert process.exitcode == 0
    assert result.get(timeout=1) == "entered"
    result.close()
