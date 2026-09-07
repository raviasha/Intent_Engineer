"""Bounded repository observation and explicit reviewed test execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
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
GIT_TIMEOUT_SECONDS = 5
_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_GIT_EXECUTABLE = Path("/usr/bin/git")
_FIXED_ENVIRONMENT: Mapping[str, str] = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
}
_GIT_ENVIRONMENT: Mapping[str, str] = {
    **_FIXED_ENVIRONMENT,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}
_VETTED_INTERPRETERS = frozenset({"/bin/sh", "/usr/bin/python3"})
_DESCRIPTOR_PROBE = """\
import subprocess
import sys

descriptor = int(sys.argv[1])
interpreter = sys.argv[2]
if interpreter == '/usr/bin/python3':
    command = [interpreter, '-']
    expected = 37
elif interpreter == '/bin/sh':
    command = [interpreter, '-s', '--']
    expected = 37
else:
    raise SystemExit(125)
completed = subprocess.run(
    command,
    stdin=descriptor,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    pass_fds=(descriptor,),
    check=False,
)
raise SystemExit(0 if completed.returncode == expected else 125)
"""
_PROCESS_SUPERVISOR = """\
import hashlib
import os
import signal
import subprocess
import sys
import time

child = None

def stop_tree(sig=signal.SIGTERM):
    if child is None:
        return
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        pass

def interrupted(sig, _frame):
    stop_tree(signal.SIGTERM)
    time.sleep(0.05)
    stop_tree(signal.SIGKILL)
    raise SystemExit(128 + sig)

signal.signal(signal.SIGTERM, interrupted)
if not hasattr(signal, 'pthread_sigmask'):
    raise SystemExit(125)
probe = subprocess.Popen(
    ['/usr/bin/python3', '-I', '-S', '-c', 'import time; time.sleep(30)'],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
try:
    if os.getpgid(probe.pid) != probe.pid:
        raise RuntimeError('process group unavailable')
    os.killpg(probe.pid, signal.SIGTERM)
    probe.wait(timeout=1)
except BaseException:
    probe.kill()
    probe.wait()
    raise SystemExit(125)
try:
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
except BaseException:
    raise SystemExit(125)
reviewed_descriptor = int(sys.argv[1])
expected_digest = sys.argv[2]
expected_size = int(sys.argv[3])
reviewed_argv = sys.argv[4:]
digest = hashlib.sha256()
observed_size = 0
os.lseek(reviewed_descriptor, 0, os.SEEK_SET)
while chunk := os.read(reviewed_descriptor, 65536):
    observed_size += len(chunk)
    if observed_size > expected_size:
        raise SystemExit(125)
    digest.update(chunk)
if observed_size != expected_size or digest.hexdigest() != expected_digest:
    raise SystemExit(125)
os.lseek(reviewed_descriptor, 0, os.SEEK_SET)
spawned = None
try:
    spawned = subprocess.Popen(
        reviewed_argv,
        stdin=reviewed_descriptor,
        start_new_session=True,
    )
    if os.getpgid(spawned.pid) != spawned.pid:
        spawned.kill()
        spawned.wait()
        raise SystemExit(125)
    child = spawned
finally:
    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
code = child.wait()
stop_tree(signal.SIGTERM)
time.sleep(0.05)
stop_tree(signal.SIGKILL)
raise SystemExit(code if code >= 0 else 128 - code)
"""


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
    content: bytes
    interpreter: str | None


@dataclass(frozen=True, slots=True)
class _GitPin:
    identity: tuple[int, int]
    modified_ns: int
    digest: str


def _pin_git_executable() -> _GitPin:
    descriptor = os.open(
        _GIT_EXECUTABLE,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
            or metadata.st_size > MAX_EXECUTABLE_BYTES
        ):
            raise ValueError("Git executable is unavailable")
        content = bytearray()
        while chunk := os.read(descriptor, 64 * 1024):
            content.extend(chunk)
        return _GitPin(
            identity=(metadata.st_dev, metadata.st_ino),
            modified_ns=metadata.st_mtime_ns,
            digest=hashlib.sha256(content).hexdigest(),
        )
    finally:
        os.close(descriptor)
        if "content" in locals():
            content.clear()


def _git_pin_matches(pin: _GitPin) -> bool:
    try:
        return _pin_git_executable() == pin
    except (OSError, ValueError):
        return False


def _descriptor_matches(descriptor: int, pin: _ExecutablePin) -> bool:
    content = bytearray()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(pin.content):
            return False
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, 64 * 1024):
            content.extend(chunk)
            if len(content) > MAX_EXECUTABLE_BYTES:
                return False
        return hashlib.sha256(content).hexdigest() == pin.digest
    except OSError:
        return False
    finally:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
        except OSError:
            pass
        content.clear()


def _stage_executable_descriptor(pin: _ExecutablePin) -> int:
    """Freeze reviewed bytes behind an unlinked, read-only descriptor."""
    temporary = tempfile.TemporaryDirectory(prefix="intent-reviewed-test-")
    staged = Path(temporary.name) / "executable"
    writer: int | None = None
    held: int | None = None
    try:
        writer = os.open(
            staged,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o400,
        )
        written = 0
        while written < len(pin.content):
            written += os.write(writer, pin.content[written:])
        os.fsync(writer)
        os.close(writer)
        writer = None
        held = os.open(
            staged,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        os.unlink(staged)
        temporary.cleanup()
        if not _descriptor_matches(held, pin):
            raise ValueError("staged executable changed")
        descriptor = held
        held = None
        return descriptor
    finally:
        if writer is not None:
            os.close(writer)
        if held is not None:
            os.close(held)
        temporary.cleanup()


def _terminate_fixed_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (PermissionError, ProcessLookupError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
    process.wait()


def _run_git_bounded(root: Path, args: tuple[str, ...], *, max_bytes: int) -> str:
    """Run one fixed Git argv with sanitized state, deadline, and streaming cap."""
    if type(args) is not tuple or not args or type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("invalid bounded Git invocation")
    process = subprocess.Popen(
        [
            str(_GIT_EXECUTABLE),
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=root,
        env=dict(_GIT_ENVIRONMENT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        shell=False,
        bufsize=0,
    )
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    total = 0
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    succeeded = False
    try:
        assert process.stdout is not None
        assert process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, True)
        selector.register(process.stderr, selectors.EVENT_READ, False)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("bounded Git invocation timed out")
            events = selector.select(remaining)
            if not events:
                raise TimeoutError("bounded Git invocation timed out")
            for key, _mask in events:
                file_object = key.fileobj
                descriptor = file_object if isinstance(file_object, int) else file_object.fileno()
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("bounded Git output is oversized")
                if key.data:
                    stdout.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise ValueError("bounded Git invocation failed")
        succeeded = True
        return stdout.decode("utf-8")
    finally:
        selector.close()
        if not succeeded and process.poll() is None:
            _terminate_fixed_process(process)
        stdout.clear()


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
            self._root = root
            self._directory = directory
            self._git_pin = _pin_git_executable()
            self._require_repository_root(root)
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

    def _git(self, args: tuple[str, ...], *, max_bytes: int) -> str:
        if not _git_pin_matches(self._git_pin):
            raise ValueError("Git executable changed")
        result = _run_git_bounded(self._root, args, max_bytes=max_bytes)
        if not _git_pin_matches(self._git_pin):
            raise ValueError("Git executable changed")
        return result

    def _require_repository_root(self, root: Path) -> None:
        top = self._git(("rev-parse", "--show-toplevel"), max_bytes=8192).strip()
        if Path(top).resolve(strict=True) != root.resolve(strict=True):
            raise ValueError("foreign Git repository")

    def _current_revision(self) -> str:
        revision = self._git(("rev-parse", "--verify", "--quiet", "HEAD"), max_bytes=256).strip()
        if _REVISION.fullmatch(revision) is None:
            raise ValueError("invalid Git revision")
        return revision

    def _changed_paths(self) -> tuple[str, ...]:
        tracked = self._git(
            ("diff", "--no-ext-diff", "--name-only", "-z", "HEAD"),
            max_bytes=MAX_CHANGED_PATH_OUTPUT_BYTES,
        )
        untracked = self._git(
            ("ls-files", "--others", "--exclude-standard", "-z"),
            max_bytes=MAX_CHANGED_PATH_OUTPUT_BYTES,
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
        interpreter: str | None = None
        if source.content.startswith(b"#!"):
            first_line = source.content.partition(b"\n")[0]
            try:
                interpreter = first_line.removeprefix(b"#!").decode("ascii")
            except UnicodeError as error:
                raise ValueError("test interpreter is unavailable") from error
            if interpreter not in _VETTED_INTERPRETERS:
                raise ValueError("test interpreter is unavailable")
            interpreter_metadata = os.stat(interpreter, follow_symlinks=False)
            if (
                not stat.S_ISREG(interpreter_metadata.st_mode)
                or interpreter_metadata.st_uid != 0
                or interpreter_metadata.st_mode & 0o022
            ):
                raise ValueError("test interpreter is unavailable")
        return _ExecutablePin(
            relative=relative,
            identities=source.identities,
            modified_ns=source.modified_ns,
            digest=hashlib.sha256(source.content).hexdigest(),
            content=source.content,
            interpreter=interpreter,
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
        self, argv: tuple[str, ...], pin: _ExecutablePin
    ) -> tuple[TestRunStatus, int | None, bytes, bytes]:
        if not await self._process_group_available():
            raise ValueError("test process containment is unavailable")
        if not await self._descriptor_launch_available(pin.interpreter):
            raise ValueError("descriptor-backed launch is unavailable")
        stdout = bytearray()
        stderr = bytearray()
        budget = [MAX_TEST_OUTPUT_BYTES]
        overflow = [False]
        timed_out = False
        descriptor = _stage_executable_descriptor(pin)
        if pin.interpreter == "/usr/bin/python3":
            reviewed_argv = (pin.interpreter, "-", *argv[1:])
        elif pin.interpreter == "/bin/sh":
            reviewed_argv = (pin.interpreter, "-s", "--", *argv[1:])
        else:
            raise ValueError("descriptor-backed launch is unavailable")
        execution_argv = (
            "/usr/bin/python3",
            "-I",
            "-S",
            "-c",
            _PROCESS_SUPERVISOR,
            str(descriptor),
            pin.digest,
            str(len(pin.content)),
            *reviewed_argv,
        )
        process: anyio.abc.Process | None = None
        try:
            if not _descriptor_matches(descriptor, pin):
                raise ValueError("staged executable changed")
            process = await anyio.open_process(
                execution_argv,
                cwd=self._root,
                env=dict(_FIXED_ENVIRONMENT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(descriptor,),
            )
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
            if process is not None and process.returncode is None:
                with anyio.CancelScope(shield=True):
                    self._stop_process(process)
                    with anyio.move_on_after(1):
                        await process.wait()
                    if process.returncode is None:
                        self._stop_process(process, force=True)
                        await process.wait()
            if process is not None:
                await process.aclose()
            os.close(descriptor)
            stdout.clear()
            stderr.clear()
            budget.clear()
            overflow.clear()

    @staticmethod
    def _stop_process(process: anyio.abc.Process, *, force: bool = False) -> None:
        try:
            process.kill() if force else process.terminate()
        except ProcessLookupError:
            pass

    @staticmethod
    async def _process_group_available() -> bool:
        """Prove owned group signaling before any reviewed command can start."""
        probe: anyio.abc.Process | None = None
        group_signal_sent = False
        try:
            probe = await anyio.open_process(
                ["/usr/bin/python3", "-c", "import time; time.sleep(30)"],
                cwd="/",
                env=dict(_FIXED_ENVIRONMENT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            if os.getpgid(probe.pid) != probe.pid:
                return False
            os.killpg(probe.pid, signal.SIGTERM)
            group_signal_sent = True
            with anyio.fail_after(1):
                await probe.wait()
            return probe.returncode is not None
        except (OSError, TimeoutError):
            return False
        finally:
            if probe is not None and probe.returncode is None:
                # The probe is fixed internal code and has no descendants; this is cleanup,
                # never a fallback for a reviewed command tree.
                if group_signal_sent:
                    try:
                        os.killpg(probe.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    try:
                        probe.kill()
                    except ProcessLookupError:
                        pass
                with anyio.CancelScope(shield=True):
                    await probe.wait()
            if probe is not None:
                await probe.aclose()

    @staticmethod
    async def _descriptor_launch_available(interpreter: str | None) -> bool:
        """Prove nested execution reads the held descriptor before use."""
        if interpreter not in _VETTED_INTERPRETERS:
            return False
        with tempfile.TemporaryFile() as handle:
            process: anyio.abc.Process | None = None
            try:
                source = (
                    b"raise SystemExit(37)\n" if interpreter == "/usr/bin/python3" else b"exit 37\n"
                )
                handle.write(source)
                handle.flush()
                handle.seek(0)
                descriptor = handle.fileno()
                process = await anyio.open_process(
                    [
                        "/usr/bin/python3",
                        "-I",
                        "-S",
                        "-c",
                        _DESCRIPTOR_PROBE,
                        str(descriptor),
                        interpreter,
                    ],
                    cwd="/",
                    env=dict(_FIXED_ENVIRONMENT),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(descriptor,),
                )
                with anyio.fail_after(1):
                    await process.wait()
                return process.returncode == 0
            except (OSError, TimeoutError, ValueError):
                return False
            finally:
                if process is not None and process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (PermissionError, ProcessLookupError):
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    with anyio.CancelScope(shield=True):
                        await process.wait()
                if process is not None:
                    await process.aclose()

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
                or pin is None
                or not self._executable_matches(pin)
            ):
                return TestRunResult(command_id=command_id, status=TestRunStatus.REJECTED)
            self._require_repository_root(self._root)
            before = self._current_revision()
            status, exit_code, stdout, stderr = await self._execute(argv, pin)
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
