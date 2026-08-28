"""Recovery and boundary regression coverage for durable connector syncs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

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
                subject_ref="requirement:local-export",
                case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
                affected_refs=("requirement:local-export",),
                evidence_sides=(side,),
                detector_id="fixture",
                fingerprint=sha256(f"fixture-{index}".encode()).hexdigest(),
            )
            for index in (1, 2)
        )


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


class MultiObjectFailOnceConnector:
    """Fail object two once after object one has been normalized successfully."""

    connector_id = "multi"

    def __init__(self) -> None:
        self.batch = False
        self._failed = False

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        names = ("baseline",) if not self.batch else ("one", "two", "three")
        return tuple(
            SourceObject(
                external_object_id=f"fixture:{name}",
                external_version="v1",
                locator=f"{name}.md",
            )
            for name in names
        )

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        if object_id == "fixture:two" and not self._failed:
            self._failed = True
            raise RuntimeError("private second-object failure")
        name = object_id.removeprefix("fixture:")
        return RawSourceObject(
            connector_type=self.connector_id,
            external_object_id=object_id,
            external_version=version,
            author="fixture@example.test",
            observed_at=datetime(2026, 8, 25, tzinfo=UTC),
            source_locator=f"{name}.md",
            content_hash=f"sha256:{name}",
            payload={
                "intent_assertion": {
                    "id": f"assertion:{name}",
                    "subject_id": f"requirement:{name}:{version}",
                    "change_kind": "initialize",
                    "node_type": "REQUIREMENT",
                    "label": f"Requirement {name}",
                    "source_mode": "explicit",
                    "evidence_refs": (f"evidence:{self.connector_id}:{object_id}:v1",),
                    "confidence": 0.9,
                }
            },
        )

    def normalize(self, raw: RawSourceObject):  # type: ignore[no-untyped-def]
        from intent_engineering.capture.base import normalize_raw_source

        return normalize_raw_source(raw)

    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        return "batch-v1" if self.batch else "baseline-v1"


class MutablePendingConnector:
    """Expose source deletion/change after one record is durable and a later fetch fails."""

    connector_id = "mutable"

    def __init__(self) -> None:
        self.visible: tuple[tuple[str, str], ...] = (("baseline", "v1"),)
        self.fail_object: str | None = None
        self._failed = False

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        return tuple(
            SourceObject(
                external_object_id=f"fixture:{name}",
                external_version=version,
                locator=f"{name}.md",
            )
            for name, version in self.visible
        )

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        if object_id == self.fail_object and not self._failed:
            self._failed = True
            raise RuntimeError("private later-object failure")
        name = object_id.removeprefix("fixture:")
        return RawSourceObject(
            connector_type=self.connector_id,
            external_object_id=object_id,
            external_version=version,
            author="fixture@example.test",
            observed_at=datetime(2026, 8, 25, tzinfo=UTC),
            source_locator=f"{name}.md",
            content_hash=f"sha256:{name}:{version}",
            payload={
                "intent_assertion": {
                    "id": f"assertion:{name}:{version}",
                    "subject_id": f"requirement:{name}:{version}",
                    "change_kind": "initialize",
                    "node_type": "REQUIREMENT",
                    "label": f"Requirement {name} {version}",
                    "source_mode": "explicit",
                    "evidence_refs": (
                        f"evidence:{self.connector_id}:{object_id}:{version}",
                    ),
                    "confidence": 0.9,
                }
            },
        )

    def normalize(self, raw: RawSourceObject):  # type: ignore[no-untyped-def]
        from intent_engineering.capture.base import normalize_raw_source

        return normalize_raw_source(raw)

    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        if not discovered:
            return None
        return ",".join(f"{item.external_object_id}@{item.external_version}" for item in discovered)


class SharedProviderConnector:
    """One of multiple connector instances observing the same provider evidence."""

    connector_type = "shared"

    def __init__(self, connector_id: str) -> None:
        self.connector_id = connector_id
        self.active_version = "v1"

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        if cursor == self.active_version:
            return ()
        return (
            SourceObject(
                external_object_id="fixture:shared",
                external_version=self.active_version,
                locator="shared.md",
            ),
        )

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        return RawSourceObject(
            connector_type=self.connector_type,
            external_object_id=object_id,
            external_version=version,
            author="fixture@example.test",
            observed_at=datetime(2026, 8, 25, tzinfo=UTC),
            source_locator="shared.md",
            content_hash=f"sha256:shared:{version}",
            payload={},
        )

    def normalize(self, raw: RawSourceObject):  # type: ignore[no-untyped-def]
        from intent_engineering.capture.base import normalize_raw_source

        return normalize_raw_source(raw)

    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        return self.active_version if discovered else None


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
async def test_retry_replays_an_atomically_rolled_back_case_group(tmp_path: Path) -> None:
    """An ordinary case-stage failure rolls back the entire declared case group."""
    raw_store = JsonlCaseStore(tmp_path / "cases.jsonl")
    failed_once = False

    def fail_case_stage(stage: str) -> None:
        nonlocal failed_once
        if stage == "target:cases" and not failed_once:
            failed_once = True
            raise RuntimeError("case transaction private detail")

    harness = SyncHarness(
        tmp_path,
        (FixtureConnector(),),
        case_detector=TwoCaseDetector(),
        case_store=raw_store,
        transaction_fault_hook=fail_case_stage,
    )

    failed = await harness.run()
    assert raw_store.list() == ()
    recovered = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert failed.cases_created == 0
    assert recovered.cases_created == 2
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
async def test_multi_object_fetch_failure_keeps_prior_records_and_retries_full_pending_delta(
    tmp_path: Path,
) -> None:
    connector = MultiObjectFailOnceConnector()
    reasoner = RecordingReasoner()
    harness = SyncHarness(tmp_path, (connector,), reasoner=reasoner)
    baseline = await harness.run()
    assert baseline.status is SyncRunStatus.SUCCESS
    checkpoint_before = harness.checkpoint_path.read_bytes()
    connector.batch = True

    failed = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert (failed.evidence_added, failed.changes_applied, failed.cases_created) == (1, 0, 0)
    assert len(harness.evidence_store.list()) == 2
    assert harness.checkpoint_path.read_bytes() == checkpoint_before

    recovered = await harness.run()

    assert recovered.status is SyncRunStatus.SUCCESS
    assert (recovered.evidence_added, recovered.changes_applied, recovered.cases_created) == (2, 1, 0)
    assert len(harness.evidence_store.list()) == 4
    assert {record.external_object_id for record in reasoner.deltas[-1].added} == {
        "fixture:one",
        "fixture:two",
        "fixture:three",
    }
    assert harness.checkpoint_path.read_bytes() != checkpoint_before


@pytest.mark.anyio
async def test_deleted_source_record_is_replayed_from_durable_evidence_before_completion(
    tmp_path: Path,
) -> None:
    """Removing a source cannot erase evidence persisted beyond the retained checkpoint."""
    connector = MutablePendingConnector()
    reasoner = RecordingReasoner()
    harness = SyncHarness(tmp_path, (connector,), reasoner=reasoner)
    await harness.run()
    connector.visible = (("one", "v1"), ("two", "v1"))
    connector.fail_object = "fixture:two"

    failed = await harness.run()
    connector.visible = ()
    recovered = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    assert recovered.status is SyncRunStatus.SUCCESS
    assert recovered.evidence_added == 0
    assert recovered.changes_applied == 1
    assert recovered.connectors["mutable"].checkpoint_advanced is True
    assert [record.external_object_id for record in reasoner.deltas[-1].added] == ["fixture:one"]

    repeated = await harness.run()
    assert (repeated.evidence_added, repeated.changes_applied, repeated.cases_created) == (0, 0, 0)
    assert repeated.connectors["mutable"].checkpoint_advanced is False
    assert len(reasoner.deltas) == 2


@pytest.mark.anyio
async def test_changed_source_replays_pending_then_new_version_in_durable_order(tmp_path: Path) -> None:
    """A changed object cannot replace its already-durable pending semantic version."""
    connector = MutablePendingConnector()
    reasoner = RecordingReasoner()
    harness = SyncHarness(tmp_path, (connector,), reasoner=reasoner)
    await harness.run()
    connector.visible = (("one", "v1"), ("two", "v1"))
    connector.fail_object = "fixture:two"
    await harness.run()
    connector.visible = (("one", "v2"), ("two", "v1"))

    recovered = await harness.run()
    replay = reasoner.deltas[-1]

    assert recovered.status is SyncRunStatus.SUCCESS
    assert recovered.evidence_added == 2
    assert [
        (record.external_object_id, record.external_version) for record in replay.added
    ] == [("fixture:one", "v1"), ("fixture:one", "v2"), ("fixture:two", "v1")]
    assert replay.prior_versions["fixture:one"] == replay.added[0].id


@pytest.mark.anyio
async def test_legacy_evidence_and_checkpoint_migrate_with_one_semantic_replay(tmp_path: Path) -> None:
    """A pre-boundary row is enriched without conflicting and an empty boundary rescans once."""
    connector = FixtureConnector()
    reasoner = RecordingReasoner()
    harness = SyncHarness(tmp_path, (connector,), reasoner=reasoner)
    source = (await connector.discover(None))[0]
    legacy = connector.normalize(await connector.fetch(source.external_object_id, source.external_version))
    assert harness.evidence_store.put(legacy) is True
    harness.checkpoint_store.compare_and_set(
        connector.connector_id,
        None,
        "legacy-cursor",
        datetime(2026, 8, 24, tzinfo=UTC),
    )

    migrated = await harness.run()
    repeated = await harness.run()

    assert migrated.status is SyncRunStatus.SUCCESS
    assert (migrated.evidence_added, migrated.changes_applied) == (0, 1)
    assert migrated.connectors["markdown"].checkpoint_advanced is True
    assert reasoner.deltas[0].added == (legacy,)
    assert harness.evidence_store.get(legacy.id) == legacy
    assert harness.checkpoint_store.get("markdown").consumed_evidence_ids == (legacy.id,)  # type: ignore[union-attr]
    assert (repeated.evidence_added, repeated.changes_applied, repeated.cases_created) == (0, 0, 0)


@pytest.mark.anyio
async def test_overlapping_provider_instances_replay_and_consume_independently(tmp_path: Path) -> None:
    left = SharedProviderConnector("left")
    right = SharedProviderConnector("right")
    reasoner = RecordingReasoner()
    harness = SyncHarness(tmp_path, (left, right), reasoner=reasoner)

    first = await harness.run()
    repeated = await harness.run()

    assert first.status is SyncRunStatus.SUCCESS
    assert first.connectors["left"].evidence_added == 1
    assert first.connectors["right"].evidence_added == 0
    assert len(reasoner.deltas) == 2
    assert reasoner.deltas[0].added == reasoner.deltas[1].added
    evidence_id = reasoner.deltas[0].added[0].id
    assert harness.checkpoint_store.get("left").consumed_evidence_ids == (evidence_id,)  # type: ignore[union-attr]
    assert harness.checkpoint_store.get("right").consumed_evidence_ids == (evidence_id,)  # type: ignore[union-attr]
    assert [item.sequence for item in harness.evidence_store.ledger("left")] == [1]
    assert [item.sequence for item in harness.evidence_store.ledger("right")] == [1]
    assert (repeated.evidence_added, repeated.changes_applied, repeated.cases_created) == (0, 0, 0)


@pytest.mark.anyio
async def test_divergent_provider_instances_advance_three_versions_in_combined_runs(
    tmp_path: Path,
) -> None:
    left = SharedProviderConnector("left")
    right = SharedProviderConnector("right")
    harness = SyncHarness(tmp_path, (left, right), reasoner=RecordingReasoner())

    first = await harness.run()
    left.active_version, right.active_version = "left-v2", "right-v2"
    second = await harness.run()
    left.active_version, right.active_version = "left-v3", "right-v3"
    third = await harness.run()
    repeated = await harness.run()

    assert all(item.status is SyncRunStatus.SUCCESS for item in (first, second, third, repeated))
    assert [item.evidence.external_version for item in harness.evidence_store.ledger("left")] == [
        "v1",
        "left-v2",
        "left-v3",
    ]
    assert [item.evidence.external_version for item in harness.evidence_store.ledger("right")] == [
        "v1",
        "right-v2",
        "right-v3",
    ]
    assert harness.evidence_store.ledger("left")[-1].predecessor_id != (
        harness.evidence_store.ledger("right")[-1].predecessor_id
    )
    assert all(third.connectors[item].checkpoint_advanced for item in ("left", "right"))
    assert (repeated.evidence_added, repeated.changes_applied, repeated.cases_created) == (0, 0, 0)
    assert all(not repeated.connectors[item].checkpoint_advanced for item in ("left", "right"))


async def _three_version_checkpoint_harness(
    tmp_path: Path,
) -> tuple[SharedProviderConnector, RecordingReasoner, SyncHarness]:
    connector = SharedProviderConnector("prefix")
    reasoner = RecordingReasoner()
    harness = SyncHarness(tmp_path, (connector,), reasoner=reasoner)
    await harness.run()
    connector.active_version = "v2"
    await harness.run()
    connector.active_version = "v3"
    await harness.run()
    return connector, reasoner, harness


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["missing", "skipped", "reordered"])
async def test_runtime_rejects_non_prefix_consumption_without_mutation(
    tmp_path: Path,
    kind: str,
) -> None:
    _connector, reasoner, harness = await _three_version_checkpoint_harness(tmp_path)
    checkpoint = harness.checkpoint_store.get("prefix")
    assert checkpoint is not None
    ids = tuple(item.evidence.id for item in harness.evidence_store.ledger("prefix"))
    invalid = {
        "missing": (ids[0], "evidence:missing"),
        "skipped": (ids[0], ids[2]),
        "reordered": (ids[1], ids[0]),
    }[kind]
    payload = checkpoint.model_dump(mode="json")
    payload["consumed_evidence_ids"] = list(invalid)
    harness.checkpoint_path.write_text(
        yaml.safe_dump({"checkpoints": {"prefix": payload}}, sort_keys=True),
        encoding="utf-8",
    )
    graph_before = harness.graph_path.read_bytes()
    checkpoint_before = harness.checkpoint_path.read_bytes()
    case_before = harness.case_store.list()
    delta_count = len(reasoner.deltas)

    result = await harness.run()

    assert result.status is SyncRunStatus.FAILED
    assert result.connectors["prefix"].redacted_error == "connector failed"
    assert harness.graph_path.read_bytes() == graph_before
    assert harness.checkpoint_path.read_bytes() == checkpoint_before
    assert harness.case_store.list() == case_before
    assert len(reasoner.deltas) == delta_count


@pytest.mark.anyio
async def test_runtime_accepts_exact_consumed_prefix_and_replays_only_suffix(tmp_path: Path) -> None:
    _connector, reasoner, harness = await _three_version_checkpoint_harness(tmp_path)
    checkpoint = harness.checkpoint_store.get("prefix")
    assert checkpoint is not None
    ledger = harness.evidence_store.ledger("prefix")
    payload = checkpoint.model_dump(mode="json")
    payload["consumed_evidence_ids"] = [ledger[0].evidence.id, ledger[1].evidence.id]
    harness.checkpoint_path.write_text(
        yaml.safe_dump({"checkpoints": {"prefix": payload}}, sort_keys=True),
        encoding="utf-8",
    )

    result = await harness.run()

    assert result.status is SyncRunStatus.SUCCESS
    assert result.connectors["prefix"].checkpoint_advanced is True
    assert reasoner.deltas[-1].added == (ledger[2].evidence,)
    assert harness.checkpoint_store.get("prefix").consumed_evidence_ids == tuple(  # type: ignore[union-attr]
        item.evidence.id for item in ledger
    )


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
