"""End-to-end cursor and checkpoint coverage for the real Markdown connector."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from intent_engineering.capture.markdown.connector import MarkdownConnector
from intent_engineering.core.models import (
    CandidateAssertion,
    DriftObservation,
    EvidenceDelta,
    EvidenceSide,
    Graph,
    ProjectConfig,
    ReconciliationCaseType,
)
from intent_engineering.extract.deterministic import DeterministicReasoner
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.yaml.checkpoint_store import YamlCheckpointStore
from intent_engineering.sync.models import SyncRunStatus
from sync.conftest import SyncHarness


class CountingReasoner(DeterministicReasoner):
    """Count semantic boundary use without changing fixture extraction behavior."""

    def __init__(self) -> None:
        super().__init__(actor="fixture")
        self.calls = 0

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        self.calls += 1
        return super().extract_assertions(delta)


class FailOnVersionUpdateReasoner(CountingReasoner):
    """Fail once after the new Markdown evidence row is durable."""

    def __init__(self) -> None:
        super().__init__()
        self._failed = False

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        if delta.prior_versions and not self._failed:
            self._failed = True
            raise RuntimeError("reasoner private detail")
        return super().extract_assertions(delta)


class FailOnVersionUpdateDetector:
    """Fail once after the graph stage for a changed Markdown document."""

    def __init__(self) -> None:
        self.calls = 0
        self._failed = False

    def __call__(self, delta: EvidenceDelta, graph: Graph) -> Sequence[DriftObservation]:
        self.calls += 1
        if any(item.predecessor_id is not None for item in delta.ingestions) and not self._failed:
            self._failed = True
            raise RuntimeError("detector private detail")
        return ()


class TwoCasesOnVersionUpdate:
    """Emit two deterministic cases only for a changed Markdown source version."""

    def __call__(self, delta: EvidenceDelta, graph: Graph) -> Sequence[DriftObservation]:
        if not any(item.predecessor_id is not None for item in delta.ingestions):
            return ()
        evidence_id = delta.added[0].id
        side = EvidenceSide(
            label="markdown",
            claim="changed document",
            evidence_refs=(evidence_id,),
            observed_at=datetime(2026, 8, 25, tzinfo=UTC),
            authors=("fixture@example.test",),
            confidence=0.9,
        )
        return tuple(
            DriftObservation(
                subject_ref=f"requirement:markdown-{index}",
                case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
                affected_refs=(f"requirement:markdown-{index}",),
                evidence_sides=(side,),
                detector_id="markdown-fixture",
                fingerprint=sha256(f"markdown-{index}".encode()).hexdigest(),
            )
            for index in (1, 2)
        )


class FailSecondCaseStore:
    """Fail once after the first real durable case write."""

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


def markdown_harness(
    state_root: Path,
    source_root: Path,
    *,
    reasoner: DeterministicReasoner | None = None,
    detector: Any = None,
    case_store: Any = None,
) -> SyncHarness:
    connector = MarkdownConnector(
        source_root,
        ProjectConfig(project_id="markdown-sync", local_actor="fixture@example.test"),
    )
    return SyncHarness(
        state_root,
        (connector,),
        reasoner=reasoner,
        case_detector=detector,
        case_store=case_store,
    )


@pytest.mark.anyio
async def test_markdown_manifest_sync_skips_reasoner_and_detector_for_identical_snapshot(
    tmp_path: Path,
) -> None:
    """A committed complete manifest prevents stale Markdown evidence from re-entering semantics."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "intent.md").write_text("# Intent\n", encoding="utf-8")
    reasoner = CountingReasoner()
    detector = FailOnVersionUpdateDetector()
    harness = markdown_harness(tmp_path / "state", source_root, reasoner=reasoner, detector=detector)

    first = await harness.run()
    checkpoint_bytes = harness.checkpoint_path.read_bytes()
    second = await harness.run()

    assert first.evidence_added == 1
    assert second.status is SyncRunStatus.SUCCESS
    assert (second.evidence_added, second.changes_applied, second.cases_created) == (0, 0, 0)
    assert reasoner.calls == 1
    assert detector.calls == 1
    assert harness.checkpoint_path.read_bytes() == checkpoint_bytes


@pytest.mark.anyio
async def test_markdown_sync_processes_only_one_changed_document_and_retries_after_evidence_failure(
    tmp_path: Path,
) -> None:
    """A failed changed-document sync retains its prior manifest and retries just that document."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    path = source_root / "intent.md"
    path.write_text("# Initial\n", encoding="utf-8")
    reasoner = FailOnVersionUpdateReasoner()
    harness = markdown_harness(tmp_path / "state", source_root, reasoner=reasoner)

    await harness.run()
    checkpoint_bytes = harness.checkpoint_path.read_bytes()
    path.write_text("# Changed\n", encoding="utf-8")
    failed = await harness.run()
    assert harness.checkpoint_path.read_bytes() == checkpoint_bytes
    recovered = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    assert recovered.status is SyncRunStatus.SUCCESS
    assert recovered.evidence_added == 0
    assert harness.checkpoint_path.read_bytes() != checkpoint_bytes


@pytest.mark.anyio
async def test_markdown_sync_migrates_legacy_hash_checkpoint_with_one_safe_rescan(tmp_path: Path) -> None:
    """A Task-6 hash cursor causes one full scan and is replaced by a versioned manifest."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    path = source_root / "intent.md"
    path.write_text("# Intent\n", encoding="utf-8")
    connector = MarkdownConnector(
        source_root,
        ProjectConfig(project_id="markdown-sync", local_actor="fixture@example.test"),
    )
    legacy = (await connector.discover(None))[0].external_version
    state_root = tmp_path / "state"
    state_root.mkdir()
    checkpoint_store = YamlCheckpointStore(state_root / "checkpoints.yaml")
    checkpoint_store.compare_and_set("markdown", None, legacy, datetime(2026, 8, 25, tzinfo=UTC))
    harness = markdown_harness(state_root, source_root)

    result = await harness.run()
    migrated = checkpoint_store.get("markdown")

    assert result.status is SyncRunStatus.SUCCESS
    assert result.evidence_added == 1
    assert migrated is not None and migrated.cursor is not None
    assert migrated.cursor.startswith("markdown:v1:")


@pytest.mark.anyio
async def test_markdown_detector_and_partial_case_failures_preserve_checkpoint_bytes(
    tmp_path: Path,
) -> None:
    """Post-graph and partial-case failures cannot consume the prior Markdown manifest."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    path = source_root / "intent.md"
    path.write_text("# Initial\n", encoding="utf-8")
    detector = FailOnVersionUpdateDetector()
    harness = markdown_harness(tmp_path / "state", source_root, detector=detector)

    await harness.run()
    detector_checkpoint = harness.checkpoint_path.read_bytes()
    path.write_text("# Detector changed\n", encoding="utf-8")
    detector_failed = await harness.run()
    assert harness.checkpoint_path.read_bytes() == detector_checkpoint
    await harness.run()

    raw_store = JsonlCaseStore(tmp_path / "case-state" / "cases.jsonl")
    case_harness = markdown_harness(
        tmp_path / "case-state",
        source_root,
        detector=TwoCasesOnVersionUpdate(),
        case_store=FailSecondCaseStore(raw_store),
    )
    await case_harness.run()
    case_checkpoint = case_harness.checkpoint_path.read_bytes()
    path.write_text("# Case changed\n", encoding="utf-8")
    case_failed = await case_harness.run()
    assert case_harness.checkpoint_path.read_bytes() == case_checkpoint

    assert detector_failed.status is SyncRunStatus.FAILED
    assert case_failed.status is SyncRunStatus.FAILED


@pytest.mark.anyio
async def test_markdown_deletion_commits_manifest_without_semantic_reprocessing(
    tmp_path: Path,
) -> None:
    """A removed document updates the full cursor while leaving unchanged evidence out of semantics."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    retained = source_root / "retained.md"
    removed = source_root / "removed.md"
    retained.write_text("# Retained\n", encoding="utf-8")
    removed.write_text("# Removed\n", encoding="utf-8")
    reasoner = CountingReasoner()
    detector = FailOnVersionUpdateDetector()
    harness = markdown_harness(tmp_path / "state", source_root, reasoner=reasoner, detector=detector)

    initial = await harness.run()
    initial_checkpoint = harness.checkpoint_path.read_bytes()
    removed.unlink()
    deletion = await harness.run()
    deletion_checkpoint = harness.checkpoint_path.read_bytes()
    no_op = await harness.run()

    assert initial.evidence_added == 2
    assert deletion.status is SyncRunStatus.SUCCESS
    assert (deletion.evidence_added, deletion.changes_applied, deletion.cases_created) == (0, 0, 0)
    assert deletion_checkpoint != initial_checkpoint
    assert reasoner.calls == 1
    assert detector.calls == 1
    assert no_op.status is SyncRunStatus.SUCCESS
    assert harness.checkpoint_path.read_bytes() == deletion_checkpoint
    assert reasoner.calls == 1
    assert detector.calls == 1
