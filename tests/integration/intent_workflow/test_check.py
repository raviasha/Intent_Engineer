"""Consolidated readiness, capture, validation, assurance, and drift checks."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import intent_engineering.cli.runtime as runtime_module
from intent_engineering.cli.runtime import CheckRuntimeAdapter
from intent_engineering.core.models import ReconciliationCase
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.check import (
    CheckReason,
    CheckRequest,
    CheckService,
    CheckStatus,
    SharedStateRestoreResult,
    SharedStateRestoreStatus,
    TestResultArtifact,
)
from intent_engineering.intent_workflow.readiness import (
    EnsureResult,
    EnsureStatus,
    ReadinessTarget,
)
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.sync.models import SyncRunResult, SyncRunStatus
from intent_engineering.validation.service import ValidationReport

NOW = datetime(2026, 9, 7, 10, tzinfo=UTC)
REVISION = "a" * 40


def _readiness(status: EnsureStatus = EnsureStatus.READY) -> EnsureResult:
    return EnsureResult(
        status=status,
        attention_route=(
            ReadinessTarget.HOME if status is EnsureStatus.READY else ReadinessTarget.ONBOARDING
        ),
        graph_version=2 if status is EnsureStatus.READY else 0,
        pending_proposal_ids=(),
        open_case_ids=(),
    )


def _sync(status: SyncRunStatus = SyncRunStatus.SUCCESS) -> SyncRunResult:
    return SyncRunResult(
        run_id="run:check",
        status=status,
        connectors={},
        evidence_added=1,
        changes_applied=0,
        cases_created=0,
        duration_ms=0,
    )


def _validation(valid: bool = True) -> ValidationReport:
    return ValidationReport(
        valid=valid,
        graph_id="graph:demo",
        graph_version=2,
        diagnostics=(),
    )


def _artifact(**updates: object) -> bytes:
    value: dict[str, object] = {
        "schema_version": 1,
        "repository_id": "demo",
        "commit_sha": REVISION,
        "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "test_ids": ["test:export"],
        "author": "ci:test-runner",
        "acl": ["local:asha"],
    }
    value.update(updates)
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def test_check_request_rejects_unknown_or_duplicate_source_ids() -> None:
    """Catches the public service request bypassing the reviewed connector allowlist."""
    for sources in (("markdown", "markdown"), ("prompt-command",)):
        with pytest.raises(ValidationError):
            CheckRequest(sources=sources)


@dataclass
class _Runtime:
    events: list[str] = field(default_factory=list)
    readiness: EnsureResult = field(default_factory=_readiness)
    capture_result: SyncRunResult = field(default_factory=_sync)
    validation_result: ValidationReport = field(default_factory=_validation)
    assurance_result: SyncRunResult = field(default_factory=_sync)
    artifact: bytes | None = None
    revision: str = REVISION
    cases: tuple[ReconciliationCase, ...] = ()
    captured_artifact: TestResultArtifact | None = None
    restore_result: SharedStateRestoreResult = field(
        default_factory=lambda: SharedStateRestoreResult(
            status=SharedStateRestoreStatus.VERIFIED,
        )
    )

    def restore(self, *, require_shared: bool) -> SharedStateRestoreResult:
        self.events.append("restore:shared" if require_shared else "restore:local")
        return self.restore_result

    @property
    def repository_id(self) -> str:
        return "demo"

    @property
    def principals(self) -> frozenset[str]:
        return frozenset({"local:asha"})

    def ensure(self) -> EnsureResult:
        self.events.append("readiness")
        return self.readiness

    def read_test_results(self, path: Path) -> bytes:
        self.events.append("test-result-read")
        assert path == Path("results.json")
        assert self.artifact is not None
        return self.artifact

    def current_revision(self) -> str:
        self.events.append("revision")
        return self.revision

    async def capture(
        self,
        sources: tuple[str, ...],
        test_result: TestResultArtifact | None,
    ) -> SyncRunResult:
        self.events.append("capture")
        assert sources == ("markdown", "git")
        self.captured_artifact = test_result
        return self.capture_result

    def validate(self) -> ValidationReport:
        self.events.append("validation")
        return self.validation_result

    async def assure(self) -> SyncRunResult:
        self.events.append("assurance")
        return self.assurance_result

    def authorized_cases(self) -> tuple[ReconciliationCase, ...]:
        self.events.append("cases")
        return self.cases

    def render_drift(self, cases: tuple[ReconciliationCase, ...]) -> str:
        self.events.append("render")
        assert cases == self.cases
        return "# bounded drift\n"


@pytest.mark.anyio
async def test_check_runs_each_existing_boundary_in_order_and_returns_a_stable_pass() -> None:
    """Catches validation or assurance being bypassed or run against the wrong stage."""
    runtime = _Runtime(artifact=_artifact())

    result = await CheckService(runtime, clock=lambda: NOW).run(
        CheckRequest(test_results=Path("results.json"))
    )

    assert runtime.events == [
        "restore:local",
        "readiness",
        "test-result-read",
        "revision",
        "capture",
        "validation",
        "assurance",
        "cases",
        "render",
    ]
    assert result.status is CheckStatus.PASSED
    assert result.reason is CheckReason.CHECKS_PASSED
    assert result.exit_code == 0
    assert result.readiness_status is EnsureStatus.READY
    assert result.capture_status is SyncRunStatus.SUCCESS
    assert result.validation_valid is True
    assert result.test_evidence_id is not None
    assert result.drift_report == "# bounded drift\n"
    assert runtime.captured_artifact == TestResultArtifact.model_validate_json(_artifact())


@pytest.mark.anyio
async def test_verified_ci_restore_precedes_readiness_capture_and_validation() -> None:
    """Catches CI consuming local state before the approved baseline is restored."""
    runtime = _Runtime(artifact=_artifact())

    result = await CheckService(runtime, clock=lambda: NOW).run(
        CheckRequest(ci=True, test_results=Path("results.json"))
    )

    assert result.exit_code == 0
    assert runtime.events == [
        "restore:shared",
        "readiness",
        "test-result-read",
        "revision",
        "capture",
        "validation",
        "assurance",
        "cases",
        "render",
    ]


@pytest.mark.anyio
async def test_readiness_failure_stops_before_opening_mutating_runtime_boundaries() -> None:
    """Catches CI or local check initializing/capturing when readiness rejects the baseline."""
    runtime = _Runtime(readiness=_readiness(EnsureStatus.ONBOARDING_REQUIRED))

    result = await CheckService(runtime, clock=lambda: NOW).run(CheckRequest(ci=True))

    assert runtime.events == ["restore:shared", "readiness"]
    assert (result.status, result.reason, result.exit_code) == (
        CheckStatus.FAILED,
        CheckReason.READINESS_REQUIRED,
        1,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("capture_status", "expected_status", "expected_reason", "exit_code"),
    [
        (SyncRunStatus.PARTIAL, CheckStatus.PARTIAL, CheckReason.CAPTURE_PARTIAL, 3),
        (SyncRunStatus.FAILED, CheckStatus.FAILED, CheckReason.CAPTURE_FAILED, 1),
    ],
)
async def test_capture_outcomes_have_distinct_fixed_exit_codes(
    capture_status: SyncRunStatus,
    expected_status: CheckStatus,
    expected_reason: CheckReason,
    exit_code: int,
) -> None:
    """Catches connector partial failure being collapsed into success or review."""
    runtime = _Runtime(capture_result=_sync(capture_status))

    result = await CheckService(runtime, clock=lambda: NOW).run(CheckRequest())

    assert (result.status, result.reason, result.exit_code) == (
        expected_status,
        expected_reason,
        exit_code,
    )
    if capture_status is SyncRunStatus.PARTIAL:
        assert runtime.events[-3:] == ["assurance", "cases", "render"]


@pytest.mark.anyio
async def test_invalid_canonical_state_stops_before_assurance() -> None:
    """Catches deterministic assurance mutating a snapshot rejected by canonical validation."""
    runtime = _Runtime(validation_result=_validation(False))

    result = await CheckService(runtime, clock=lambda: NOW).run(CheckRequest())

    assert runtime.events == ["restore:local", "readiness", "capture", "validation"]
    assert (result.reason, result.exit_code) == (CheckReason.VALIDATION_FAILED, 1)


@pytest.mark.anyio
async def test_required_review_is_selected_only_for_authorized_open_cases() -> None:
    """Catches a review gate passing despite a rendered authorized nonterminal case."""
    runtime = _Runtime()
    runtime.cases = (object(),)  # type: ignore[assignment]

    result = await CheckService(runtime, clock=lambda: NOW).run(CheckRequest(require_review=True))

    assert (result.status, result.reason, result.exit_code) == (
        CheckStatus.REVIEW_REQUIRED,
        CheckReason.REVIEW_REQUIRED,
        4,
    )
    assert result.review_case_count == 1


@pytest.mark.anyio
async def test_ci_requires_an_explicit_passing_test_result_artifact() -> None:
    """Catches CI treating absent test evidence as a successful required check."""
    runtime = _Runtime()

    result = await CheckService(runtime, clock=lambda: NOW).run(CheckRequest(ci=True))

    assert runtime.events == ["restore:shared", "readiness"]
    assert (result.reason, result.exit_code) == (CheckReason.TEST_RESULTS_REQUIRED, 1)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("updates", "now"),
    [
        ({"schema_version": 2}, NOW),
        ({"repository_id": "other"}, NOW),
        ({"commit_sha": "b" * 40}, NOW),
        ({"observed_at": (NOW - timedelta(hours=24, microseconds=1)).isoformat()}, NOW),
        ({"observed_at": (NOW + timedelta(microseconds=1)).isoformat()}, NOW),
        ({"status": "failed"}, NOW),
        ({"test_ids": ["test:export", "test:export"]}, NOW),
        ({"acl": ["local:other"]}, NOW),
        ({"extra": "not canonical"}, NOW),
    ],
)
async def test_test_result_artifact_rejects_unbound_stale_failing_duplicate_or_hidden_input(
    updates: dict[str, object],
    now: datetime,
) -> None:
    """Catches untrusted result JSON satisfying repository-bound passing evidence."""
    runtime = _Runtime(artifact=_artifact(**updates))

    result = await CheckService(runtime, clock=lambda: now).run(
        CheckRequest(test_results=Path("results.json"))
    )

    assert result.status is CheckStatus.FAILED
    assert result.reason is CheckReason.TEST_RESULTS_INVALID
    assert result.exit_code == 1
    assert "capture" not in runtime.events


@pytest.mark.anyio
async def test_test_result_artifact_is_size_bounded_before_json_decode() -> None:
    """Catches an oversized artifact reaching parsing or durable evidence state."""
    runtime = _Runtime(artifact=b"{" + b"x" * 65_536)

    result = await CheckService(runtime, clock=lambda: NOW).run(
        CheckRequest(test_results=Path("results.json"))
    )

    assert result.reason is CheckReason.TEST_RESULTS_INVALID
    assert "capture" not in runtime.events


@pytest.mark.anyio
async def test_test_result_replay_uses_the_same_content_addressed_evidence_identity() -> None:
    """Catches identical bytes producing a new test evidence identity on replay."""
    first_runtime = _Runtime(artifact=_artifact())
    second_runtime = _Runtime(artifact=_artifact())

    first = await CheckService(first_runtime, clock=lambda: NOW).run(
        CheckRequest(test_results=Path("results.json"))
    )
    second = await CheckService(second_runtime, clock=lambda: NOW).run(
        CheckRequest(test_results=Path("results.json"))
    )

    assert first.test_evidence_id == second.test_evidence_id


@pytest.mark.anyio
async def test_cancellation_is_preserved_without_rendering_a_false_result() -> None:
    """Catches cancellation being converted into a passing or ordinary failed check."""
    runtime = _Runtime()

    async def cancelled(
        sources: tuple[str, ...], result: TestResultArtifact | None
    ) -> SyncRunResult:
        del sources, result
        raise asyncio.CancelledError

    runtime.capture = cancelled  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await CheckService(runtime, clock=lambda: NOW).run(CheckRequest())

    assert runtime.events == ["restore:local", "readiness"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("restore_status", "readiness_status"),
    [
        (
            SharedStateRestoreStatus.UNAVAILABLE,
            EnsureStatus.SHARED_STATE_UNAVAILABLE,
        ),
        (SharedStateRestoreStatus.INVALID, EnsureStatus.SHARED_STATE_INVALID),
        (SharedStateRestoreStatus.STALE, EnsureStatus.OFFLINE_STALE),
        (SharedStateRestoreStatus.UPGRADE_REQUIRED, EnsureStatus.UPGRADE_REQUIRED),
    ],
)
async def test_ci_shared_state_failure_stops_before_local_readiness(
    restore_status: SharedStateRestoreStatus,
    readiness_status: EnsureStatus,
) -> None:
    """Catches unavailable or untrusted shared state falling through to local readiness."""
    runtime = _Runtime(
        artifact=_artifact(),
        restore_result=SharedStateRestoreResult(status=restore_status),
    )

    result = await CheckService(runtime, clock=lambda: NOW).run(
        CheckRequest(ci=True, test_results=Path("results.json"))
    )

    assert runtime.events == ["restore:shared"]
    assert (result.status, result.reason, result.exit_code) == (
        CheckStatus.FAILED,
        CheckReason.READINESS_REQUIRED,
        1,
    )
    assert result.readiness_status is readiness_status


@pytest.mark.anyio
async def test_shared_state_restore_cancellation_propagates_before_readiness() -> None:
    """Catches restore cancellation being reported as an ordinary CI failure."""
    runtime = _Runtime()

    def cancelled(*, require_shared: bool) -> SharedStateRestoreResult:
        assert require_shared
        raise asyncio.CancelledError

    runtime.restore = cancelled  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await CheckService(runtime, clock=lambda: NOW).run(
            CheckRequest(ci=True, test_results=Path("results.json"))
        )


@pytest.mark.anyio
async def test_unpublished_shared_state_requires_review_before_any_ci_capture() -> None:
    """Catches preserved local decisions being treated as an operational failure or CI success."""
    runtime = _Runtime(
        restore_result=SharedStateRestoreResult(
            status=SharedStateRestoreStatus.DIVERGED,
        )
    )

    result = await CheckService(runtime, clock=lambda: NOW).run(CheckRequest(ci=True))

    assert (result.status, result.exit_code, result.readiness_status) == (
        CheckStatus.REVIEW_REQUIRED,
        4,
        EnsureStatus.HUMAN_ATTENTION_REQUIRED,
    )
    assert runtime.events == ["restore:shared"]


@pytest.mark.anyio
async def test_malformed_restore_result_is_a_fixed_readiness_failure() -> None:
    """Catches a broken restore adapter escaping the strict check result boundary."""
    runtime = _Runtime()
    runtime.restore = lambda *, require_shared: object()  # type: ignore[method-assign,return-value]

    result = await CheckService(runtime, clock=lambda: NOW).run(CheckRequest())

    assert (result.status, result.reason, result.exit_code) == (
        CheckStatus.FAILED,
        CheckReason.READINESS_REQUIRED,
        1,
    )
    assert runtime.events == []


def test_local_restore_recovers_a_prepared_transaction_before_readiness(tmp_path: Path) -> None:
    """Catches the restore boundary reading a torn graph before local recovery."""
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    workspace = SecureDirectory.open(project / ".intent")

    def crash(stage: str) -> None:
        if stage == "target:graph":
            raise SystemExit

    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        {
            "graph": workspace.file("graph.yaml"),
            "history": workspace.file("history/changesets.jsonl"),
            "cases": workspace.file("reconciliation/cases.jsonl"),
        },
        fault_hook=crash,
    )
    try:
        with pytest.raises(SystemExit), coordinator.transaction() as transaction:
            transaction.write("graph", b"torn: [")
    finally:
        coordinator.close()
        workspace.close()

    journal = project / ".intent/history/.local-transaction.json"
    assert journal.exists()
    adapter = CheckRuntimeAdapter(project)
    try:
        restored = adapter.restore(require_shared=False)
        readiness = adapter.ensure()
    finally:
        adapter.close()

    assert restored.status is SharedStateRestoreStatus.NOT_REQUIRED
    assert readiness.status is EnsureStatus.ONBOARDING_REQUIRED
    assert not journal.exists()


@pytest.mark.anyio
async def test_runtime_capture_cancellation_does_not_append_test_result_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cancellation between artifact append and source capture leaving partial evidence."""
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    evidence_path = project / ".intent" / "evidence" / "evidence.jsonl"
    before = evidence_path.read_bytes() if evidence_path.exists() else None

    async def cancelled(*args: object, **kwargs: object) -> SyncRunResult:
        del args, kwargs
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime_module, "run_selected_sync", cancelled)
    adapter = CheckRuntimeAdapter(project)
    artifact = TestResultArtifact.model_validate_json(_artifact())
    try:
        with pytest.raises(asyncio.CancelledError):
            await adapter.capture(("markdown",), artifact)
    finally:
        adapter.close()

    assert (evidence_path.read_bytes() if evidence_path.exists() else None) == before
