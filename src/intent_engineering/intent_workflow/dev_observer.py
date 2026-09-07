"""Bounded repository observation and explicit reviewed test execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import anyio
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from pydantic import ConfigDict, Field

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.capture.git.connector import run_git
from intent_engineering.core.models import EvidenceRecord, JsonValue, ProjectConfig
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.check import (
    MAX_TEST_RESULT_BYTES,
    TestResultArtifact,
    validate_test_result_artifact,
)
from intent_engineering.storage.secure import SecureDirectory, UnsafePathError

MAX_TEST_OUTPUT_BYTES = 64 * 1024
TEST_TIMEOUT_SECONDS = 300
MAX_EXECUTABLE_BYTES = 16 * 1024 * 1024
MAX_CHANGED_PATHS = 4096
MAX_CHANGED_PATH_BYTES = 4096
MAX_CHANGED_PATH_OUTPUT_BYTES = 1024 * 1024
_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_FIXED_ENVIRONMENT: Mapping[str, str] = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
}


class DevObserverError(ValueError):
    """One fixed public failure for unsafe or unavailable observation."""

    def __init__(self) -> None:
        super().__init__("development observation unavailable")


class TestRunStatus(StrEnum):
    """Stable outcomes from an explicit reviewed command."""

    __test__ = False

    PASSED = "passed"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT = "output_limit"


class _ObserverModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ObservationResult(_ObserverModel):
    """A passive snapshot containing evidence candidates and no graph mutation."""

    schema_version: Literal[1] = 1
    current_revision: Annotated[str, Field(pattern=_REVISION.pattern)]
    command_ids: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    evidence_candidates: tuple[EvidenceRecord, ...] = ()


class TestRunResult(_ObserverModel):
    """Bounded output and optional canonical artifact from one reviewed command."""

    schema_version: Literal[1] = 1
    command_id: Annotated[str, Field(max_length=76)]
    status: TestRunStatus
    exit_code: Annotated[int, Field(ge=-255, le=255)] | None = None
    stdout: Annotated[str, Field(max_length=MAX_TEST_OUTPUT_BYTES)] = ""
    stderr: Annotated[str, Field(max_length=MAX_TEST_OUTPUT_BYTES)] = ""
    artifact: TestResultArtifact | None = None


@dataclass(frozen=True, slots=True)
class _ExecutablePin:
    relative: str
    identities: tuple[tuple[int, int], ...]
    modified_ns: int
    digest: str


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _command_id(argv: tuple[str, ...]) -> str:
    return f"test:sha256:{hashlib.sha256(_canonical_bytes(list(argv))).hexdigest()}"


def _evidence(
    *,
    repository_id: str,
    revision: str,
    observed_at: datetime,
    author: str,
    acl: tuple[str, ...],
    kind: str,
    paths: tuple[str, ...],
) -> EvidenceRecord:
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "repository_id": repository_id,
        "commit_sha": revision,
        "observation_kind": kind,
        "changed_paths": list(paths),
    }
    content = _canonical_bytes(payload)
    digest = hashlib.sha256(content).hexdigest()
    return normalize_raw_source(
        RawSourceObject(
            connector_type="dev_observer",
            external_object_id=f"{kind}:{revision}:{digest}",
            external_version=f"sha256:{digest}",
            author=author,
            observed_at=observed_at,
            source_locator=f"git:{kind}:{revision}",
            content_hash=f"sha256:{digest}",
            payload=payload,
            acl=acl,
        )
    )


class DevObserver:
    """Observe one exact Git root and run only pinned project commands on request."""

    def __init__(
        self,
        project_root: Path,
        config: ProjectConfig,
        *,
        repository_id: str,
        principals: frozenset[str],
    ) -> None:
        self._directory: SecureDirectory | None = None
        try:
            if (
                type(config) is not ProjectConfig
                or type(repository_id) is not str
                or not repository_id
                or type(principals) is not frozenset
                or not principals
                or any(type(item) is not str or not item for item in principals)
            ):
                raise ValueError("invalid development observer")
            root = Path(os.path.abspath(project_root))
            directory = SecureDirectory.open(root)
            self._require_repository_root(root)
            self._root = root
            self._directory = directory
            self._config = config
            self._repository_id = repository_id
            self._principals = principals
            self._acl = tuple(sorted(principals))
            self._commands = {_command_id(argv): argv for argv in config.test_commands}
            self._pins = {
                identifier: self._pin_executable_if_present(argv[0])
                for identifier, argv in self._commands.items()
            }
            self._last_revision: str | None = None
            self._last_paths: tuple[str, ...] | None = None
            self._seen_evidence_ids: set[str] = set()
        except Exception as error:
            if self._directory is not None:
                self._directory.close()
                self._directory = None
            raise DevObserverError() from error

    @property
    def command_ids(self) -> tuple[str, ...]:
        """Return stable content-derived identifiers for configured argv entries."""
        return tuple(self._commands)

    @staticmethod
    def _require_repository_root(root: Path) -> None:
        top = run_git(root, ["rev-parse", "--show-toplevel"]).strip()
        if Path(top).resolve(strict=True) != root.resolve(strict=True):
            raise ValueError("foreign Git repository")

    def _current_revision(self) -> str:
        revision = run_git(
            self._root,
            ["rev-parse", "--verify", "--quiet", "HEAD"],
        ).strip()
        if _REVISION.fullmatch(revision) is None:
            raise ValueError("invalid Git revision")
        return revision

    def _changed_paths(self) -> tuple[str, ...]:
        tracked = run_git(self._root, ["diff", "--name-only", "-z", "HEAD"])
        untracked = run_git(
            self._root,
            ["ls-files", "--others", "--exclude-standard", "-z"],
        )
        encoded_bytes = len(tracked.encode("utf-8")) + len(untracked.encode("utf-8"))
        paths = tuple(sorted({item for item in (tracked + untracked).split("\x00") if item}))
        if (
            encoded_bytes > MAX_CHANGED_PATH_OUTPUT_BYTES
            or len(paths) > MAX_CHANGED_PATHS
            or any(not path or len(path.encode("utf-8")) > MAX_CHANGED_PATH_BYTES for path in paths)
        ):
            raise ValueError("Git path observation is oversized")
        return paths

    def _pin_executable(self, relative: str) -> _ExecutablePin:
        if self._directory is None:
            raise ValueError("closed development observer")
        source = self._directory.read_relative(
            relative,
            nonblocking=True,
            max_bytes=MAX_EXECUTABLE_BYTES,
        )
        metadata = os.stat(self._root / relative, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & 0o111:
            raise ValueError("test executable is unavailable")
        return _ExecutablePin(
            relative=relative,
            identities=source.identities,
            modified_ns=source.modified_ns,
            digest=hashlib.sha256(source.content).hexdigest(),
        )

    def _pin_executable_if_present(self, relative: str) -> _ExecutablePin | None:
        try:
            return self._pin_executable(relative)
        except (OSError, UnsafePathError):
            return None

    def _executable_matches(self, pin: _ExecutablePin | None) -> bool:
        if self._directory is None or pin is None:
            return False
        try:
            source = self._directory.read_relative(
                pin.relative,
                expected_identities=pin.identities,
                nonblocking=True,
                max_bytes=MAX_EXECUTABLE_BYTES,
            )
            metadata = os.stat(self._root / pin.relative, follow_symlinks=False)
            return (
                source.modified_ns == pin.modified_ns
                and hashlib.sha256(source.content).hexdigest() == pin.digest
                and stat.S_ISREG(metadata.st_mode)
                and bool(metadata.st_mode & 0o111)
            )
        except (OSError, UnsafePathError):
            return False

    def _read_result(self, relative: str) -> bytes | None:
        if self._directory is None:
            raise ValueError("closed development observer")
        target = self._directory.file(relative)
        try:
            return target.read_optional_nonblocking(max_bytes=MAX_TEST_RESULT_BYTES)
        finally:
            target.close()

    def poll(self, *, at: datetime) -> ObservationResult:
        """Return new bounded evidence candidates without running commands or mutating state."""
        try:
            if type(at) is not datetime or at.tzinfo is None or at.utcoffset() != timedelta(0):
                raise ValueError("invalid observation time")
            self._require_repository_root(self._root)
            revision = self._current_revision()
            paths = self._changed_paths()
            candidates: list[EvidenceRecord] = []
            if self._last_revision != revision:
                candidates.append(
                    _evidence(
                        repository_id=self._repository_id,
                        revision=revision,
                        observed_at=at.astimezone(UTC),
                        author=self._config.local_actor,
                        acl=self._acl,
                        kind="git_head",
                        paths=(),
                    )
                )
            if self._last_paths is not None and self._last_paths != paths:
                candidates.append(
                    _evidence(
                        repository_id=self._repository_id,
                        revision=revision,
                        observed_at=at.astimezone(UTC),
                        author=self._config.local_actor,
                        acl=self._acl,
                        kind="git_paths",
                        paths=paths,
                    )
                )
            for relative in self._config.test_result_paths:
                raw = self._read_result(relative)
                if raw is None:
                    continue
                artifact = validate_test_result_artifact(
                    raw,
                    repository_id=self._repository_id,
                    commit_sha=revision,
                    at=at,
                    principals=self._principals,
                )
                candidates.append(artifact.evidence())
            self._last_revision = revision
            self._last_paths = paths
            fresh = tuple(item for item in candidates if item.id not in self._seen_evidence_ids)
            self._seen_evidence_ids.update(item.id for item in fresh)
            return ObservationResult(
                current_revision=revision,
                command_ids=self.command_ids,
                changed_paths=paths,
                evidence_candidates=fresh,
            )
        except Exception as error:
            raise DevObserverError() from error

    async def _read_stream(
        self,
        stream: anyio.abc.ByteReceiveStream,
        retained: bytearray,
        budget: list[int],
        overflow: list[bool],
        process: anyio.abc.Process,
    ) -> None:
        while True:
            try:
                chunk = await stream.receive()
            except (EndOfStream, BrokenResourceError, ClosedResourceError):
                return
            available = budget[0]
            if available > 0:
                captured = chunk[:available]
                retained.extend(captured)
                budget[0] -= len(captured)
            if len(chunk) > available:
                overflow[0] = True
                self._stop_process(process)

    async def _execute(
        self, argv: tuple[str, ...]
    ) -> tuple[TestRunStatus, int | None, bytes, bytes]:
        stdout = bytearray()
        stderr = bytearray()
        budget = [MAX_TEST_OUTPUT_BYTES]
        overflow = [False]
        timed_out = False
        process = await anyio.open_process(
            argv,
            cwd=self._root,
            env=dict(_FIXED_ENVIRONMENT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert process.stdout is not None
            assert process.stderr is not None
            try:
                with anyio.fail_after(TEST_TIMEOUT_SECONDS):
                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(
                            self._read_stream,
                            process.stdout,
                            stdout,
                            budget,
                            overflow,
                            process,
                        )
                        tasks.start_soon(
                            self._read_stream,
                            process.stderr,
                            stderr,
                            budget,
                            overflow,
                            process,
                        )
                        await process.wait()
            except TimeoutError:
                timed_out = True
            if overflow[0]:
                status = TestRunStatus.OUTPUT_LIMIT
            elif timed_out:
                status = TestRunStatus.TIMED_OUT
            elif process.returncode == 0:
                status = TestRunStatus.PASSED
            else:
                status = TestRunStatus.FAILED
            return status, process.returncode, bytes(stdout), bytes(stderr)
        finally:
            if process.returncode is None:
                with anyio.CancelScope(shield=True):
                    self._stop_process(process)
                    with anyio.move_on_after(1):
                        await process.wait()
                    if process.returncode is None:
                        self._stop_process(process, force=True)
                        await process.wait()
            await process.aclose()
            stdout.clear()
            stderr.clear()
            budget.clear()
            overflow.clear()

    @staticmethod
    def _stop_process(process: anyio.abc.Process, *, force: bool = False) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                process.kill() if force else process.terminate()
            except ProcessLookupError:
                pass

    def _write_artifact(self, artifact: TestResultArtifact) -> None:
        if not self._config.test_result_paths or self._directory is None:
            return
        target = self._directory.file(self._config.test_result_paths[0], create_parents=True)
        try:
            target.atomic_write(artifact.canonical_bytes(), reject_target_races=True)
        finally:
            target.close()

    async def run_reviewed_tests(self, command_id: str, *, at: datetime) -> TestRunResult:
        """Run one configured argv explicitly, never through a shell or background poll."""
        argv = self._commands.get(command_id) if type(command_id) is str else None
        if argv is None:
            public_id = command_id if type(command_id) is str and len(command_id) <= 76 else ""
            return TestRunResult(command_id=public_id, status=TestRunStatus.REJECTED)
        pin = self._pins[command_id]
        try:
            if (
                type(at) is not datetime
                or at.tzinfo is None
                or at.utcoffset() != timedelta(0)
                or not self._executable_matches(pin)
            ):
                return TestRunResult(command_id=command_id, status=TestRunStatus.REJECTED)
            self._require_repository_root(self._root)
            before = self._current_revision()
            status, exit_code, stdout, stderr = await self._execute(argv)
            after = self._current_revision()
            if before != after or not self._executable_matches(pin):
                return TestRunResult(command_id=command_id, status=TestRunStatus.REJECTED)
            artifact = None
            if status is TestRunStatus.PASSED:
                artifact = TestResultArtifact(
                    repository_id=self._repository_id,
                    commit_sha=after,
                    observed_at=at.astimezone(UTC),
                    status="passed",
                    test_ids=(command_id,),
                    author=self._config.local_actor,
                    acl=self._acl,
                )
                self._write_artifact(artifact)
            return TestRunResult(
                command_id=command_id,
                status=status,
                exit_code=exit_code,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                artifact=artifact,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError, UnsafePathError, ValueError):
            return TestRunResult(command_id=command_id, status=TestRunStatus.REJECTED)

    def close(self) -> None:
        """Release the held repository descriptor."""
        if self._directory is not None:
            self._directory.close()
            self._directory = None


__all__ = [
    "MAX_CHANGED_PATHS",
    "MAX_TEST_OUTPUT_BYTES",
    "TEST_TIMEOUT_SECONDS",
    "DevObserver",
    "DevObserverError",
    "ObservationResult",
    "TestRunResult",
    "TestRunStatus",
]
