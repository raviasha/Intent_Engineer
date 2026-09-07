"""One bounded orchestration boundary for local and CI intent assurance checks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Protocol

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.core.models import (
    EvidenceRecord,
    JsonValue,
    ProjectConfig,
    ReconciliationCase,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.readiness import EnsureResult, EnsureStatus
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.sync.models import SyncRunResult, SyncRunStatus
from intent_engineering.validation.service import ValidationReport

MAX_TEST_RESULT_BYTES = 64 * 1024
MAX_TEST_RESULT_AGE = timedelta(hours=24)
_MAX_TEST_IDS = 256
_MAX_IDENTIFIER_BYTES = 512
_MAX_DRIFT_REPORT_BYTES = 1024 * 1024
_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class CheckStatus(StrEnum):
    """Stable aggregate states for the consolidated command."""

    PASSED = "passed"
    PARTIAL = "partial"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"


class CheckReason(StrEnum):
    """Fixed, non-sensitive reasons suitable for local and CI policy."""

    CHECKS_PASSED = "checks_passed"
    READINESS_REQUIRED = "readiness_required"
    TEST_RESULTS_REQUIRED = "test_results_required"
    TEST_RESULTS_INVALID = "test_results_invalid"
    TEST_RUN_FAILED = "test_run_failed"
    CAPTURE_PARTIAL = "capture_partial"
    CAPTURE_FAILED = "capture_failed"
    VALIDATION_FAILED = "validation_failed"
    ASSURANCE_FAILED = "assurance_failed"
    REVIEW_REQUIRED = "review_required"
    OPERATION_FAILED = "operation_failed"


class SharedStateRestoreStatus(StrEnum):
    """Bounded outcomes from an approved shared-baseline restore boundary."""

    VERIFIED = "verified"
    NOT_REQUIRED = "not_required"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    STALE = "stale"
    DIVERGED = "diverged"
    UPGRADE_REQUIRED = "upgrade_required"


class _CheckModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SharedStateRestoreResult(_CheckModel):
    """One secret-free result from verifying and restoring an approved baseline."""

    schema_version: Literal[1] = 1
    status: SharedStateRestoreStatus


class SharedStateRestorer(Protocol):
    """Future transport boundary; implementations must verify before restoring."""

    def verify_and_restore_approved_baseline(self, root: Path) -> SharedStateRestoreResult: ...


def _identifier(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError("invalid check identifier")
    return value


def _exact_tuple(value: object, info: ValidationInfo) -> object:
    if info.mode == "json" and type(value) is list:
        return tuple(value)
    if info.mode == "python" and type(value) is not tuple:
        raise ValueError("invalid check collection")
    return value


def evidence_repository_id(config: ProjectConfig) -> str:
    """Stable project evidence identity; never the checkout's WebAuthn/service identity."""
    return _identifier(config.project_id)


class TestResultBinding(_CheckModel):
    """Independently observed inputs required to accept a test execution artifact."""

    __test__: ClassVar[bool] = False

    execution_snapshot: Annotated[str, Field(pattern=_DIGEST)]
    intent_baseline: Annotated[str, Field(pattern=_DIGEST)]
    reviewed_commands: Annotated[str, Field(pattern=_DIGEST)]
    command_ids: tuple[str, ...]


class TestResultArtifact(_CheckModel):
    """Canonical passing-test result accepted as evidence, never as a command."""

    __test__: ClassVar[bool] = False

    schema_version: Literal[2] = 2
    repository_id: str
    commit_sha: Annotated[str, Field(pattern=_REVISION.pattern)]
    execution_snapshot: Annotated[str, Field(pattern=_DIGEST)]
    intent_baseline: Annotated[str, Field(pattern=_DIGEST)]
    reviewed_commands: Annotated[str, Field(pattern=_DIGEST)]
    observed_at: datetime
    status: Literal["passed", "failed", "cancelled"]
    test_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=_MAX_TEST_IDS)]
    author: str
    acl: Annotated[tuple[str, ...], Field(min_length=1, max_length=_MAX_TEST_IDS)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid test result schema version")
        return value

    @field_validator("test_ids", "acl", mode="before")
    @classmethod
    def require_exact_lists(cls, value: object, info: ValidationInfo) -> object:
        return _exact_tuple(value, info)

    @field_validator("repository_id", "author")
    @classmethod
    def require_identifiers(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("test_ids", "acl")
    @classmethod
    def require_unique_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("duplicate test result identifier")
        for value in values:
            _identifier(value)
        return values

    @field_validator("observed_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("test result timestamp must be UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_lowercase_revision(self) -> TestResultArtifact:
        if self.commit_sha != self.commit_sha.lower():
            raise ValueError("test result revision must be lowercase")
        return self

    def canonical_bytes(self) -> bytes:
        """Return the stable semantic spelling used for immutable evidence identity."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def evidence(self) -> EvidenceRecord:
        """Project the artifact into the existing immutable evidence contract."""
        canonical = self.canonical_bytes()
        digest = hashlib.sha256(canonical).hexdigest()
        payload: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "repository_id": self.repository_id,
            "commit_sha": self.commit_sha,
            "execution_snapshot": self.execution_snapshot,
            "intent_baseline": self.intent_baseline,
            "reviewed_commands": self.reviewed_commands,
            "outcome": self.status,
            "test_refs": list(self.test_ids),
        }
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return normalize_raw_source(
            RawSourceObject(
                connector_type="test_result",
                external_object_id=f"test-run:{self.commit_sha}",
                external_version=f"sha256:{digest}",
                author=self.author,
                observed_at=self.observed_at,
                source_locator=f"test:run:{self.commit_sha}",
                content_hash=f"sha256:{hashlib.sha256(encoded_payload).hexdigest()}",
                payload=payload,
                acl=self.acl,
            )
        )


class CheckRequest(_CheckModel):
    """One bounded consolidated check request with no command-execution input."""

    schema_version: Literal[1] = 1
    ci: bool = False
    require_review: bool = False
    sources: tuple[str, ...] = ("markdown", "git")
    test_results: Path | None = None
    run_test: str | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid check schema version")
        return value

    @field_validator("sources")
    @classmethod
    def require_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        allowed = frozenset({"markdown", "git", "github", "mcp"})
        if (
            not values
            or len(values) != len(set(values))
            or any(value not in allowed for value in values)
        ):
            raise ValueError("invalid check sources")
        for value in values:
            _identifier(value)
        return values

    @field_validator("test_results")
    @classmethod
    def require_relative_result_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if not isinstance(value, Path) or value.is_absolute() or not value.parts:
            raise ValueError("invalid test result path")
        if any(part in {"", ".", ".."} for part in value.parts):
            raise ValueError("invalid test result path")
        return value

    @field_validator("run_test")
    @classmethod
    def require_reviewed_command_id(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"test:sha256:[0-9a-f]{64}", value) is None:
            raise ValueError("invalid reviewed test command id")
        return value

    @model_validator(mode="after")
    def reject_ambiguous_test_execution(self) -> CheckRequest:
        if self.run_test is not None and (self.ci or self.test_results is not None):
            raise ValueError("reviewed test execution is incompatible with CI or result input")
        return self


class CheckResult(_CheckModel):
    """Bounded aggregate result with one stable exit-code selection."""

    schema_version: Literal[1] = 1
    status: CheckStatus
    reason: CheckReason
    exit_code: Literal[0, 1, 3, 4]
    readiness_status: EnsureStatus | None = None
    capture_status: SyncRunStatus | None = None
    validation_valid: bool | None = None
    test_evidence_id: str | None = None
    review_case_count: Annotated[int, Field(ge=0)] = 0
    drift_report: str = ""


class CheckRuntime(Protocol):
    """Narrow composition surface over existing readiness and assurance services."""

    @property
    def repository_id(self) -> str: ...

    @property
    def principals(self) -> frozenset[str]: ...

    def restore(self, *, require_shared: bool) -> SharedStateRestoreResult: ...

    def ensure(self) -> EnsureResult: ...

    def read_test_results(self, path: Path) -> bytes: ...

    def current_revision(self) -> str: ...

    def test_result_binding(self) -> TestResultBinding: ...

    async def run_reviewed_tests(self, command_id: str, *, at: datetime) -> TestResultArtifact: ...

    async def capture(
        self,
        sources: tuple[str, ...],
        test_result: TestResultArtifact | None,
    ) -> SyncRunResult: ...

    def validate(self) -> ValidationReport: ...

    async def assure(self) -> SyncRunResult: ...

    def authorized_cases(self) -> tuple[ReconciliationCase, ...]: ...

    def render_drift(self, cases: tuple[ReconciliationCase, ...]) -> str: ...


def validate_test_result_artifact(
    raw: bytes,
    *,
    repository_id: str,
    commit_sha: str,
    at: datetime,
    principals: frozenset[str],
    binding: TestResultBinding,
    require_all_commands: bool = False,
) -> TestResultArtifact:
    """Parse and bind one artifact using the canonical check ingestion contract."""
    if type(raw) is not bytes or not raw or len(raw) > MAX_TEST_RESULT_BYTES:
        raise ValueError("invalid test result artifact")
    decoded = loads_strict_object(raw.decode("utf-8"))
    if decoded.get("schema_version") != 2:
        raise ValueError("invalid test result artifact")
    artifact = TestResultArtifact.model_validate_json(raw)
    if (
        type(repository_id) is not str
        or type(commit_sha) is not str
        or type(at) is not datetime
        or at.tzinfo is None
        or at.utcoffset() != timedelta(0)
        or type(principals) is not frozenset
        or any(type(principal) is not str or not principal for principal in principals)
    ):
        raise ValueError("invalid test result binding")
    now = at.astimezone(UTC)
    if (
        artifact.repository_id != repository_id
        or artifact.commit_sha != commit_sha
        or artifact.status != "passed"
        or artifact.observed_at > now
        or now - artifact.observed_at > MAX_TEST_RESULT_AGE
        or frozenset(artifact.acl).isdisjoint(principals)
        or type(binding) is not TestResultBinding
        or artifact.execution_snapshot != binding.execution_snapshot
        or artifact.intent_baseline != binding.intent_baseline
        or artifact.reviewed_commands != binding.reviewed_commands
        or (binding.command_ids and not set(artifact.test_ids).issubset(binding.command_ids))
        or (
            require_all_commands
            and binding.command_ids
            and set(artifact.test_ids) != set(binding.command_ids)
        )
    ):
        raise ValueError("invalid test result binding")
    return artifact


class CheckService:
    """Compose existing deterministic services without acquiring human authority."""

    def __init__(
        self,
        runtime: CheckRuntime,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runtime = runtime
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _result(
        status: CheckStatus,
        reason: CheckReason,
        exit_code: Literal[0, 1, 3, 4],
        *,
        readiness: EnsureStatus | None = None,
        capture: SyncRunStatus | None = None,
        validation: bool | None = None,
        evidence_id: str | None = None,
        cases: int = 0,
        report: str = "",
    ) -> CheckResult:
        return CheckResult(
            status=status,
            reason=reason,
            exit_code=exit_code,
            readiness_status=readiness,
            capture_status=capture,
            validation_valid=validation,
            test_evidence_id=evidence_id,
            review_case_count=cases,
            drift_report=report,
        )

    def _parse_test_result(self, path: Path, *, ci: bool) -> TestResultArtifact:
        raw = self._runtime.read_test_results(path)
        now = self._clock()
        return validate_test_result_artifact(
            raw,
            repository_id=self._runtime.repository_id,
            commit_sha=self._runtime.current_revision(),
            at=now,
            principals=self._runtime.principals,
            binding=self._runtime.test_result_binding(),
            require_all_commands=ci,
        )

    async def run(self, request: CheckRequest) -> CheckResult:
        """Run ordered bounded checks and return one fixed machine result."""
        if type(request) is not CheckRequest:
            return self._result(CheckStatus.FAILED, CheckReason.OPERATION_FAILED, 1)
        readiness: EnsureResult | None = None
        artifact: TestResultArtifact | None = None
        evidence_id: str | None = None
        try:
            restored = self._runtime.restore(require_shared=request.ci)
            if type(restored) is not SharedStateRestoreResult:
                raise ValueError("invalid shared-state restore result")
        except Exception:  # noqa: BLE001 - cancellation remains a BaseException
            return self._result(CheckStatus.FAILED, CheckReason.READINESS_REQUIRED, 1)
        failure_status = {
            SharedStateRestoreStatus.UNAVAILABLE: EnsureStatus.SHARED_STATE_UNAVAILABLE,
            SharedStateRestoreStatus.INVALID: EnsureStatus.SHARED_STATE_INVALID,
            SharedStateRestoreStatus.STALE: EnsureStatus.OFFLINE_STALE,
            SharedStateRestoreStatus.DIVERGED: EnsureStatus.HUMAN_ATTENTION_REQUIRED,
            SharedStateRestoreStatus.UPGRADE_REQUIRED: EnsureStatus.UPGRADE_REQUIRED,
        }.get(restored.status)
        if request.ci and restored.status is SharedStateRestoreStatus.NOT_REQUIRED:
            failure_status = EnsureStatus.SHARED_STATE_UNAVAILABLE
        if failure_status is not None:
            needs_review = failure_status is EnsureStatus.HUMAN_ATTENTION_REQUIRED
            return self._result(
                CheckStatus.REVIEW_REQUIRED if needs_review else CheckStatus.FAILED,
                CheckReason.READINESS_REQUIRED,
                4 if needs_review else 1,
                readiness=failure_status,
            )
        try:
            readiness = self._runtime.ensure()
        except Exception:  # noqa: BLE001 - fixed readiness failure boundary
            return self._result(CheckStatus.FAILED, CheckReason.READINESS_REQUIRED, 1)
        if readiness.status is not EnsureStatus.READY:
            exit_code: Literal[1, 4] = (
                4 if readiness.status is EnsureStatus.HUMAN_ATTENTION_REQUIRED else 1
            )
            status = CheckStatus.REVIEW_REQUIRED if exit_code == 4 else CheckStatus.FAILED
            return self._result(
                status,
                CheckReason.READINESS_REQUIRED,
                exit_code,
                readiness=readiness.status,
            )
        if request.ci and request.test_results is None:
            return self._result(
                CheckStatus.FAILED,
                CheckReason.TEST_RESULTS_REQUIRED,
                1,
                readiness=readiness.status,
            )
        if request.run_test is not None:
            try:
                now = self._clock()
                executed = await self._runtime.run_reviewed_tests(request.run_test, at=now)
                if type(executed) is not TestResultArtifact:
                    raise ValueError("invalid reviewed test result")
                artifact = validate_test_result_artifact(
                    executed.canonical_bytes(),
                    repository_id=self._runtime.repository_id,
                    commit_sha=self._runtime.current_revision(),
                    at=now,
                    principals=self._runtime.principals,
                    binding=self._runtime.test_result_binding(),
                )
                evidence_id = artifact.evidence().id
            except Exception:  # noqa: BLE001 - fixed explicit-test failure boundary
                return self._result(
                    CheckStatus.FAILED,
                    CheckReason.TEST_RUN_FAILED,
                    1,
                    readiness=readiness.status,
                )
        elif request.test_results is not None:
            try:
                artifact = self._parse_test_result(request.test_results, ci=request.ci)
                evidence_id = artifact.evidence().id
            except Exception:  # noqa: BLE001 - fixed secret-free artifact failure
                return self._result(
                    CheckStatus.FAILED,
                    CheckReason.TEST_RESULTS_INVALID,
                    1,
                    readiness=readiness.status,
                )
        try:
            capture = await self._runtime.capture(request.sources, artifact)
        except Exception:  # noqa: BLE001 - cancellation remains a BaseException
            return self._result(
                CheckStatus.FAILED,
                CheckReason.CAPTURE_FAILED,
                1,
                readiness=readiness.status,
                evidence_id=evidence_id,
            )
        if capture.status is SyncRunStatus.FAILED:
            return self._result(
                CheckStatus.FAILED,
                CheckReason.CAPTURE_FAILED,
                1,
                readiness=readiness.status,
                capture=capture.status,
                evidence_id=evidence_id,
            )
        try:
            validation = self._runtime.validate()
        except Exception:  # noqa: BLE001 - fixed validation failure boundary
            return self._result(
                CheckStatus.FAILED,
                CheckReason.VALIDATION_FAILED,
                1,
                readiness=readiness.status,
                capture=capture.status,
                evidence_id=evidence_id,
            )
        if not validation.valid:
            return self._result(
                CheckStatus.FAILED,
                CheckReason.VALIDATION_FAILED,
                1,
                readiness=readiness.status,
                capture=capture.status,
                validation=False,
                evidence_id=evidence_id,
            )
        try:
            assurance = await self._runtime.assure()
            if assurance.status is not SyncRunStatus.SUCCESS:
                raise ValueError("assurance failed")
            cases = self._runtime.authorized_cases()
            report = self._runtime.render_drift(cases)
            if len(report.encode("utf-8")) > _MAX_DRIFT_REPORT_BYTES:
                raise ValueError("drift report is oversized")
        except Exception:  # noqa: BLE001 - fixed assurance failure boundary
            return self._result(
                CheckStatus.FAILED,
                CheckReason.ASSURANCE_FAILED,
                1,
                readiness=readiness.status,
                capture=capture.status,
                validation=True,
                evidence_id=evidence_id,
            )
        if capture.status is SyncRunStatus.PARTIAL:
            return self._result(
                CheckStatus.PARTIAL,
                CheckReason.CAPTURE_PARTIAL,
                3,
                readiness=readiness.status,
                capture=capture.status,
                validation=True,
                evidence_id=evidence_id,
                cases=len(cases),
                report=report,
            )
        if request.require_review and cases:
            return self._result(
                CheckStatus.REVIEW_REQUIRED,
                CheckReason.REVIEW_REQUIRED,
                4,
                readiness=readiness.status,
                capture=capture.status,
                validation=True,
                evidence_id=evidence_id,
                cases=len(cases),
                report=report,
            )
        if artifact is not None:
            try:
                validate_test_result_artifact(
                    artifact.canonical_bytes(),
                    repository_id=self._runtime.repository_id,
                    commit_sha=self._runtime.current_revision(),
                    at=self._clock(),
                    principals=self._runtime.principals,
                    binding=self._runtime.test_result_binding(),
                    require_all_commands=request.ci,
                )
            except Exception:  # noqa: BLE001 - recheck the live boundary before success
                return self._result(
                    CheckStatus.FAILED,
                    CheckReason.TEST_RESULTS_INVALID,
                    1,
                    readiness=readiness.status,
                    capture=capture.status,
                    validation=True,
                )
        return self._result(
            CheckStatus.PASSED,
            CheckReason.CHECKS_PASSED,
            0,
            readiness=readiness.status,
            capture=capture.status,
            validation=True,
            evidence_id=evidence_id,
            cases=len(cases),
            report=report,
        )


__all__ = [
    "MAX_TEST_RESULT_AGE",
    "MAX_TEST_RESULT_BYTES",
    "CheckReason",
    "CheckRequest",
    "CheckResult",
    "CheckRuntime",
    "CheckService",
    "CheckStatus",
    "SharedStateRestoreResult",
    "SharedStateRestoreStatus",
    "SharedStateRestorer",
    "TestResultArtifact",
    "TestResultBinding",
    "evidence_repository_id",
    "validate_test_result_artifact",
]
