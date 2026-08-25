"""Recovery and boundary regression coverage for durable connector syncs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from intent_engineering.capture.base import RawSourceObject, SourceObject
from intent_engineering.core.models import (
    CandidateAssertion,
    DriftObservation,
    EvidenceDelta,
    EvidenceSide,
    Graph,
    ReconciliationCaseType,
)
from intent_engineering.extract.deterministic import DeterministicReasoner
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.yaml.checkpoint_store import YamlCheckpointStore
from intent_engineering.sync.models import ConnectorRunResult, SyncRunResult, SyncRunStatus
from sync.conftest import FixtureConnector, SyncHarness, VersionedFixtureConnector


class FailOnceReasoner(DeterministicReasoner):
    """Raise after evidence durability once, then process the same source normally."""

    def __init__(self) -> None:
        super().__init__(actor="fixture")
        self._failed = False
        self.deltas: list[EvidenceDelta] = []

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        self.deltas.append(delta)
        if not self._failed:
            self._failed = True
            raise RuntimeError("reasoner private detail")
        return super().extract_assertions(delta)


class RecordingReasoner(DeterministicReasoner):
    """Record deltas passed across the public reasoning boundary."""

    def __init__(self) -> None:
        super().__init__(actor="fixture")
        self.deltas: list[EvidenceDelta] = []

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        self.deltas.append(delta)
        return super().extract_assertions(delta)


class FailOnV2Reasoner(DeterministicReasoner):
    """Fail once after v2 evidence is durable while retaining a prior checkpoint."""

    def __init__(self) -> None:
        super().__init__(actor="fixture")
        self._failed = False

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        if (
            not self._failed
            and delta.added
            and delta.added[0].external_version == "v2"
        ):
            self._failed = True
            raise RuntimeError("reasoner private detail")
        return super().extract_assertions(delta)


class FailOnceDetector:
    """Raise after graph application once, then emit no cases."""

    def __init__(self) -> None:
        self._failed = False

    def __call__(self, delta: EvidenceDelta, graph: Graph) -> Sequence[DriftObservation]:
        if not self._failed:
            self._failed = True
            raise RuntimeError("detector private detail")
        return ()


class TwoCaseDetector:
    """Emit two fixed valid observations for partial case-persistence recovery."""

    def __call__(self, delta: EvidenceDelta, graph: Graph) -> Sequence[DriftObservation]:
        if not delta.added:
            return ()
        evidence_id = delta.added[0].id
        side = EvidenceSide(
            label="fixture",
            claim="fixture drift",
            evidence_refs=(evidence_id,),
            observed_at=datetime(2026, 8, 25, tzinfo=UTC),
            authors=("fixture@example.test",),
            confidence=0.9,
        )
        return tuple(
            DriftObservation(
                subject_ref=f"requirement:fixture-{index}",
                case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
                affected_refs=(f"requirement:fixture-{index}",),
                evidence_sides=(side,),
                detector_id="fixture",
                fingerprint=sha256(f"fixture-{index}".encode()).hexdigest(),
            )
            for index in (1, 2)
        )


class FailSecondCaseStore:
    """Delegate durable writes while failing exactly once after one stored case."""

    def __init__(self, store: JsonlCaseStore) -> None:
        self._store = store
        self._calls = 0
        self._failed = False

    def put(self, case: Any) -> bool:
        self._calls += 1
        if self._calls == 2 and not self._failed:
            self._failed = True
            raise RuntimeError("case store private detail")
        return self._store.put(case)

    def get(self, case_id: str) -> Any:
        return self._store.get(case_id)

    def find_by_fingerprint(self, fingerprint: str) -> Any:
        return self._store.find_by_fingerprint(fingerprint)

    def list(self, status: Any = None) -> Any:
        return self._store.list(status)


class RawBrokenConnector:
    """Raise an ordinary exception so orchestration must isolate it."""

    connector_id = "broken"

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        raise RuntimeError("connector private detail")

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        raise AssertionError("unreachable")

    def normalize(self, raw: RawSourceObject) -> Any:
        raise AssertionError("unreachable")

    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        raise AssertionError("unreachable")


class SelectiveBrokenCheckpointStore:
    """Fail the checkpoint read for one connector while preserving another connector's store."""

    def __init__(self, store: YamlCheckpointStore) -> None:
        self._store = store

    def get(self, connector_id: str) -> Any:
        if connector_id == "broken":
            raise RuntimeError("checkpoint private detail")
        return self._store.get(connector_id)

    def compare_and_set(self, *args: Any, **kwargs: Any) -> Any:
        return self._store.compare_and_set(*args, **kwargs)


@pytest.mark.anyio
async def test_retry_reprocesses_evidence_durable_before_reasoner_failure(tmp_path: Path) -> None:
    """A missing checkpoint makes already-durable evidence re-enter the semantic delta."""
    reasoner = FailOnceReasoner()
    harness = SyncHarness(tmp_path, (FixtureConnector(),), reasoner=reasoner)

    failed = await harness.run()
    assert harness.checkpoint_path.exists() is False
    recovered = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    assert failed.changes_applied == 0
    assert recovered.evidence_added == 0
    assert recovered.changes_applied == 1
    assert recovered.status is SyncRunStatus.SUCCESS
    assert len(reasoner.deltas[1].added) == 1


@pytest.mark.anyio
async def test_retry_commits_checkpoint_after_detector_failure_with_graph_already_durable(tmp_path: Path) -> None:
    """A retry must complete the remaining work after graph application already happened."""
    harness = SyncHarness(tmp_path, (FixtureConnector(),), case_detector=FailOnceDetector())

    failed = await harness.run()
    graph_after_failure = harness.graph_path.read_bytes()
    recovered = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    assert failed.changes_applied == 1
    assert harness.graph_path.read_bytes() == graph_after_failure
    assert recovered.evidence_added == 0
    assert recovered.changes_applied == 0
    assert recovered.connectors["markdown"].checkpoint_advanced is True


@pytest.mark.anyio
async def test_retry_completes_partial_case_persistence_without_duplicate_case(tmp_path: Path) -> None:
    """Fingerprint deduplication lets a retry persist only the unfinished case."""
    raw_store = JsonlCaseStore(tmp_path / "cases.jsonl")
    harness = SyncHarness(
        tmp_path,
        (FixtureConnector(),),
        case_detector=TwoCaseDetector(),
        case_store=FailSecondCaseStore(raw_store),
    )

    failed = await harness.run()
    recovered = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert failed.cases_created == 1
    assert recovered.cases_created == 1
    assert len(raw_store.list()) == 2
    assert recovered.connectors["markdown"].checkpoint_advanced is True


@pytest.mark.anyio
async def test_raw_connector_failure_is_redacted_and_later_connector_continues(tmp_path: Path) -> None:
    """Ordinary exceptions must not leak or abort other independent connector work."""
    harness = SyncHarness(tmp_path, (RawBrokenConnector(), FixtureConnector()))

    result = await harness.run()

    assert result.status is SyncRunStatus.PARTIAL
    assert result.connectors["broken"].redacted_error == "connector failed"
    assert result.connectors["broken"].checkpoint_advanced is False
    assert result.connectors["markdown"].evidence_added == 1


@pytest.mark.anyio
async def test_checkpoint_lookup_failure_is_redacted_and_later_connector_continues(tmp_path: Path) -> None:
    """The connector transaction boundary starts before a checkpoint lookup can fail."""
    harness = SyncHarness(
        tmp_path,
        (RawBrokenConnector(), FixtureConnector()),
        checkpoint_store=SelectiveBrokenCheckpointStore(YamlCheckpointStore(tmp_path / "checkpoints.yaml")),
    )

    result = await harness.run()

    assert result.status is SyncRunStatus.PARTIAL
    assert result.connectors["broken"].redacted_error == "connector failed"
    assert result.connectors["markdown"].checkpoint_advanced is True


@pytest.mark.anyio
async def test_version_delta_links_to_immediate_predecessor_and_preserves_checkpoint_bytes(
    tmp_path: Path,
) -> None:
    """A new source version carries its durable predecessor and a failure cannot move the cursor."""
    connector = VersionedFixtureConnector()
    reasoner = RecordingReasoner()
    harness = SyncHarness(tmp_path, (connector,), reasoner=reasoner)

    first = await harness.run()
    checkpoint_bytes = harness.checkpoint_path.read_bytes()
    connector.active_version = "v2"
    second = await harness.run()

    assert first.status is SyncRunStatus.SUCCESS
    assert second.status is SyncRunStatus.SUCCESS
    assert reasoner.deltas[1].prior_versions == {
        "fixture:requirements": reasoner.deltas[0].added[0].id
    }
    assert harness.checkpoint_path.read_bytes() != checkpoint_bytes


@pytest.mark.anyio
async def test_checkpoint_bytes_are_unchanged_when_new_evidence_fails_after_durability(
    tmp_path: Path,
) -> None:
    """A failure after new evidence cannot consume a previously committed connector cursor."""
    connector = VersionedFixtureConnector()
    harness = SyncHarness(tmp_path, (connector,), reasoner=FailOnV2Reasoner())

    await harness.run()
    checkpoint_bytes = harness.checkpoint_path.read_bytes()
    connector.active_version = "v2"
    failed = await harness.run()
    assert harness.checkpoint_path.read_bytes() == checkpoint_bytes
    recovered = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    assert harness.checkpoint_path.read_bytes() != checkpoint_bytes
    assert recovered.status is SyncRunStatus.SUCCESS


@pytest.mark.anyio
async def test_duplicate_connector_ids_fail_before_durable_writes(tmp_path: Path) -> None:
    """Ambiguous aggregate keys must be rejected before evidence or checkpoints are touched."""
    harness = SyncHarness(tmp_path, (FixtureConnector(), FixtureConnector()))
    graph_bytes = harness.graph_path.read_bytes()

    with pytest.raises(ValueError, match="duplicate connector id: markdown"):
        await harness.run()

    assert harness.graph_path.read_bytes() == graph_bytes
    assert harness.evidence_path.exists() is False
    assert harness.checkpoint_path.exists() is False


@pytest.mark.anyio
async def test_empty_connector_set_is_a_successful_noop(tmp_path: Path) -> None:
    """An empty requested source set has stable zero work and no derived durable files."""
    harness = SyncHarness(tmp_path, ())

    result = await harness.run()

    assert result.status is SyncRunStatus.SUCCESS
    assert result.connectors == {}
    assert (result.evidence_added, result.changes_applied, result.cases_created) == (0, 0, 0)
    assert harness.evidence_path.exists() is False
    assert harness.checkpoint_path.exists() is False


def test_deterministic_reasoner_produces_equal_changesets_for_equivalent_and_empty_inputs() -> None:
    """Fixture mapping must not inject wall-clock differences into ChangeSet identity or nodes."""
    reasoner = DeterministicReasoner()
    graph = Graph(id="fixture-graph", version=0, nodes=(), edges=())
    assertion = CandidateAssertion(
        id="assertion:fixture",
        subject_id="requirement:fixture",
        change_kind="initialize",
        node_type="REQUIREMENT",
        label="Fixture requirement",
        source_mode="explicit",
        evidence_refs=("evidence:fixture",),
        confidence=0.9,
    )

    assert reasoner.map_to_graph((assertion,), graph) == reasoner.map_to_graph((assertion,), graph)
    assert reasoner.map_to_graph((), graph) == reasoner.map_to_graph((), graph)


def test_aggregate_retains_failed_connector_durable_counts() -> None:
    """A failed connector's completed durable work remains visible in the run summary."""
    result = SyncRunResult.from_connector_results(
        "fixture-run",
        {
            "broken": ConnectorRunResult.failed(
                "connector failed",
                evidence_added=1,
                changes_applied=1,
                cases_created=1,
            ),
            "markdown": ConnectorRunResult.succeeded(2, 0, 0, True),
        },
        duration_ms=0,
    )

    assert result.status is SyncRunStatus.PARTIAL
    assert (result.evidence_added, result.changes_applied, result.cases_created) == (3, 1, 1)
