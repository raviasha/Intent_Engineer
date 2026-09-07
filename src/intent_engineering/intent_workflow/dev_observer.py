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
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import anyio
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from pydantic import ConfigDict, Field

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.core.models import EvidenceRecord, JsonValue, ProjectConfig
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.check import (
    MAX_TEST_RESULT_BYTES,
    TestResultArtifact,
    TestResultBinding,
    evidence_repository_id,
    validate_test_result_artifact,
)
from intent_engineering.storage.secure import (
    SecureDirectory,
    SecureFile,
    UnsafePathError,
    _read_descriptor,
    configured_graph_relative,
)

MAX_TEST_OUTPUT_BYTES = 64 * 1024
TEST_TIMEOUT_SECONDS = 300
MAX_EXECUTABLE_BYTES = 16 * 1024 * 1024
MAX_REVIEWED_SOURCE_BYTES = 24 * 1024
MAX_CHANGED_PATHS = 4096
MAX_CHANGED_PATH_BYTES = 4096
MAX_CHANGED_PATH_OUTPUT_BYTES = 1024 * 1024
MAX_COMMIT_SNAPSHOT_BYTES = 128 * 1024 * 1024
MAX_SNAPSHOT_TREE_ENTRIES = 16_384
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
    "GIT_NO_REPLACE_OBJECTS": "1",
}
_VETTED_INTERPRETERS = frozenset({"/bin/sh", "/usr/bin/python3"})
# The capability probe must launch through exactly the same interpreter wrapper.
_REVIEWED_ARGV = """\
synthetic_path = 'intent-reviewed-test:' + expected_digest
if interpreter == '/usr/bin/python3':
    wrapper = (
        'import sys\\n'
        + '_source = ' + repr(source_text) + '\\n'
        + '_path = ' + repr(synthetic_path) + '\\n'
        + 'sys.argv[0] = _path\\n'
        + '_scope = vars(sys.modules["__main__"])\\n'
        + '_scope.update({"__name__": "__main__", "__file__": _path, '
        + '"__package__": None, "__cached__": None})\\n'
        + 'exec(compile(_source, _path, "exec"), _scope, _scope)\\n'
    )
    reviewed_argv = [interpreter, '-c', wrapper, *reviewed_args]
elif interpreter == '/bin/sh':
    reviewed_argv = [interpreter, '-c', source_text, synthetic_path, *reviewed_args]
else:
    raise SystemExit(125)
"""
_DESCRIPTOR_PROBE = (
    """\
import hashlib
import os
import subprocess
import sys

descriptor = int(sys.argv[1])
interpreter = sys.argv[2]
expected_digest = sys.argv[3]
expected_size = int(sys.argv[4])
source = bytearray()
while len(source) < expected_size:
    chunk = os.read(descriptor, min(65536, expected_size - len(source)))
    if not chunk:
        raise SystemExit(125)
    source.extend(chunk)
os.close(descriptor)
if len(source) != expected_size or hashlib.sha256(source).hexdigest() != expected_digest:
    raise SystemExit(125)
try:
    source_text = bytes(source).decode('utf-8')
except UnicodeError:
    raise SystemExit(125)
if '\\x00' in source_text:
    raise SystemExit(125)
source.clear()
reviewed_args = ['intent-reviewed-probe']
"""
    + _REVIEWED_ARGV
    + """\
completed = subprocess.run(
    reviewed_argv,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
raise SystemExit(0 if completed.returncode == 37 else 125)
"""
)
_PROCESS_SUPERVISOR = (
    """\
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
source_descriptor = int(sys.argv[1])
expected_digest = sys.argv[2]
expected_size = int(sys.argv[3])
interpreter = sys.argv[4]
reviewed_args = sys.argv[5:]
source_buffer = bytearray()
while len(source_buffer) < expected_size:
    chunk = os.read(source_descriptor, min(65536, expected_size - len(source_buffer)))
    if not chunk:
        raise SystemExit(125)
    source_buffer.extend(chunk)
os.close(source_descriptor)
if (
    len(source_buffer) != expected_size
    or hashlib.sha256(source_buffer).hexdigest() != expected_digest
):
    raise SystemExit(125)
try:
    source_text = bytes(source_buffer).decode('utf-8')
except UnicodeError:
    raise SystemExit(125)
if '\\x00' in source_text:
    raise SystemExit(125)
source_buffer.clear()
"""
    + _REVIEWED_ARGV
    + """\
# source-verified-boundary
spawned = None
try:
    spawned = subprocess.Popen(
        reviewed_argv,
        stdin=subprocess.DEVNULL,
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
)


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


def _reviewed_source(pin: _ExecutablePin) -> bytes:
    content = pin.content
    if (
        pin.interpreter not in _VETTED_INTERPRETERS
        or not content
        or len(content) > MAX_REVIEWED_SOURCE_BYTES
        or b"\x00" in content
    ):
        raise ValueError("reviewed source is unsupported")
    try:
        content.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("reviewed source is unsupported") from error
    return content


def _open_source_pipe() -> tuple[int, int]:
    return os.pipe()


async def _write_source_pipe(descriptor: int, source: bytes) -> None:
    os.set_blocking(descriptor, False)
    offset = 0
    try:
        while offset < len(source):
            try:
                offset += os.write(descriptor, source[offset:])
            except BlockingIOError:
                await anyio.wait_writable(descriptor)
    finally:
        os.close(descriptor)


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


def _snapshot_token(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


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
                or repository_id != evidence_repository_id(config)
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
            self._execution_environment = dict(_FIXED_ENVIRONMENT)
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

    def current_revision(self) -> str:
        """Read HEAD through the same bounded, sanitized Git boundary as execution."""
        self._require_repository_root(self._root)
        return self._current_revision()

    @contextmanager
    def _intent_baseline(self) -> Iterator[str]:
        """Hash configured intent and its semantic history under one bounded file fence."""
        if self._directory is None:
            raise ValueError("test intent baseline unavailable")
        paths = (
            ".intent/config.yaml",
            ".intent/" + configured_graph_relative(self._config.graph_path).as_posix(),
            ".intent/history/changesets.jsonl",
        )
        digest = hashlib.sha256(b"intent.test-baseline.v2\0")
        held: list[tuple[SecureFile, int, tuple[int, ...]]] = []
        ancestors: dict[str, tuple[int, int, int, int]] = {}
        with ExitStack() as resources:
            for relative in paths:
                for parent in Path(relative).parents:
                    name = str(parent)
                    current = self._ancestor_metadata(name)
                    if ancestors.setdefault(name, current) != current:
                        raise ValueError("test intent baseline changed")
                target = self._directory.file(relative)
                resources.callback(target.close)
                descriptor = os.open(
                    target.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=target.parent_fd,
                )
                resources.callback(os.close, descriptor)
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size > 8 * 1024 * 1024
                ):
                    raise ValueError("test intent baseline unavailable")
                held.append((target, descriptor, _snapshot_token(metadata)))
                content = _read_descriptor(descriptor, max_bytes=metadata.st_size)
                if len(content) != metadata.st_size:
                    raise ValueError("test intent baseline changed")
                if (
                    relative == paths[0]
                    and ProjectConfig.model_validate(load_strict_yaml_mapping_bytes(content))
                    != self._config
                ):
                    raise ValueError("reviewed test configuration changed")
                digest.update(
                    _canonical_bytes(
                        (relative, hashlib.sha256(content).hexdigest(), _snapshot_token(metadata))
                    )
                )
            # Keep intent descriptors open while code is read. Only metadata checks
            # follow the complete content capture, so no later read can hide drift.
            yield "sha256:" + digest.hexdigest()
            for name, expected in ancestors.items():
                if self._ancestor_metadata(name) != expected:
                    raise ValueError("test intent baseline changed")
            for target, descriptor, expected_token in held:
                if (
                    _snapshot_token(os.fstat(descriptor)) != expected_token
                    or _snapshot_token(
                        os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
                    )
                    != expected_token
                ):
                    raise ValueError("test intent baseline changed")

    def test_result_binding(self) -> TestResultBinding:
        """Independently capture live code, intent baseline and reviewed test configuration."""
        with self._intent_baseline() as baseline:
            return TestResultBinding(
                execution_snapshot=self.clean_commit_snapshot(),
                intent_baseline=baseline,
                reviewed_commands="sha256:"
                + hashlib.sha256(
                    _canonical_bytes(
                        {
                            "schema": "intent.reviewed-tests.v2",
                            "commands": self._config.test_commands,
                            "result_paths": self._config.test_result_paths,
                        }
                    )
                ).hexdigest(),
                command_ids=self.command_ids,
            )

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
        observed = SecureDirectory.open(root)
        try:
            if self._directory is None or observed.identity != self._directory.identity:
                raise ValueError("repository root changed")
        finally:
            observed.close()

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

    def prepare_clean_commit_execution(self) -> None:
        """Create bounded generated-output parents before pinning ancestor timestamps."""
        if self._directory is None:
            raise ValueError("clean commit snapshot unavailable")
        for relative in (
            ".intent",
            ".intent-ci",
            ".venv",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        ):
            directory = self._directory.subdirectory(relative, create=True)
            directory.close()
        for relative in self._config.test_result_paths:
            target = self._directory.file(relative, create_parents=True)
            target.close()
        # This only prevents new writes. Snapshot rejection of existing repository bytecode
        # is the read-side boundary; -B alone would still execute stale cached code.
        self._execution_environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def _ancestor_metadata(self, relative: str) -> tuple[int, int, int, int]:
        if self._directory is None:
            raise ValueError("clean commit snapshot unavailable")
        directory = (
            self._directory.duplicate()
            if relative == "."
            else self._directory.subdirectory(relative)
        )
        try:
            metadata = os.fstat(directory.descriptor)
            return metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns
        finally:
            directory.close()

    def _reject_repository_cache_directories(self, *, deadline: float) -> None:
        """Inspect directory names too: Git does not report pre-existing empty caches."""
        if self._directory is None:
            raise ValueError("clean commit snapshot unavailable")
        pending = ["."]
        count = 0
        path_bytes = 0
        while pending:
            relative = pending.pop()
            directory = (
                self._directory.duplicate()
                if relative == "."
                else self._directory.subdirectory(relative)
            )
            try:
                with os.scandir(directory.descriptor) as entries:
                    for entry in entries:
                        path = entry.name if relative == "." else f"{relative}/{entry.name}"
                        encoded_size = len(path.encode("utf-8"))
                        count += 1
                        path_bytes += encoded_size
                        if (
                            count > MAX_SNAPSHOT_TREE_ENTRIES
                            or encoded_size > MAX_CHANGED_PATH_BYTES
                            or path_bytes > MAX_CHANGED_PATH_OUTPUT_BYTES
                            or time.monotonic() > deadline
                        ):
                            raise ValueError("clean commit snapshot unavailable")
                        if relative == "." and entry.name in {
                            ".git",
                            ".intent",
                            ".intent-ci",
                            ".venv",
                            ".pytest_cache",
                            ".mypy_cache",
                            ".ruff_cache",
                        }:
                            continue
                        if entry.name.casefold() == "__pycache__":
                            raise ValueError("clean commit snapshot unavailable")
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(path)
            finally:
                directory.close()

    def clean_commit_snapshot(self) -> str:
        """Verify actual tracked bytes against HEAD and fingerprint their held identities.

        This optional CI boundary does not trust index stat caches or arbitrary ignore rules.
        Only known generated directories and exact reviewed result paths may be untracked.
        Tracked files never receive an output-path exemption.
        """
        if self._directory is None:
            raise ValueError("clean commit snapshot unavailable")
        deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
        self._reject_repository_cache_directories(deadline=deadline)
        self._require_repository_root(self._root)
        revision = self._current_revision()
        tree = self._git(
            ("ls-tree", "-rz", "--full-tree", revision), max_bytes=MAX_CHANGED_PATH_OUTPUT_BYTES
        )
        entries = tree.removesuffix("\0").split("\0") if tree else []
        if not entries or len(entries) > MAX_CHANGED_PATHS:
            raise ValueError("clean commit snapshot unavailable")
        fingerprint = hashlib.sha256(revision.encode("ascii"))
        ancestors: dict[str, tuple[int, int, int, int]] = {}
        tracked_metadata: dict[str, tuple[int, ...]] = {}
        total_bytes = 0
        for entry in entries:
            header, path = entry.split("\t", 1)
            mode, kind, expected_hash = header.split(" ")
            if (
                mode not in {"100644", "100755"}
                or kind != "blob"
                or len(path.encode("utf-8")) > MAX_CHANGED_PATH_BYTES
                or path.casefold().endswith((".pyc", ".pyo"))
                or "__pycache__" in Path(path.casefold()).parts
            ):
                raise ValueError("clean commit snapshot unavailable")
            parents = (".", *(str(parent) for parent in reversed(Path(path).parents[:-1])))
            parent_identities = []
            for parent in parents:
                metadata_before = self._ancestor_metadata(parent)
                if ancestors.setdefault(parent, metadata_before) != metadata_before:
                    raise ValueError("clean commit snapshot changed")
                parent_identities.append(metadata_before[:2])
            source = self._directory.read_relative(
                path, nonblocking=True, max_bytes=MAX_EXECUTABLE_BYTES
            )
            if source.identities[:-1] != tuple(parent_identities):
                raise ValueError("clean commit snapshot changed")
            total_bytes += len(source.content)
            if total_bytes > MAX_COMMIT_SNAPSHOT_BYTES or time.monotonic() > deadline:
                raise ValueError("clean commit snapshot unavailable")
            material = b"blob " + str(len(source.content)).encode("ascii") + b"\0" + source.content
            digest = (
                hashlib.sha1(material, usedforsecurity=False).hexdigest()
                if len(revision) == 40
                else hashlib.sha256(material).hexdigest()
            )
            target = self._directory.file(path)
            try:
                metadata = os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
            finally:
                target.close()
            if (
                digest != expected_hash
                or (metadata.st_dev, metadata.st_ino) != source.identities[-1]
                or metadata.st_mtime_ns != source.modified_ns
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or bool(metadata.st_mode & 0o111) != (mode == "100755")
            ):
                raise ValueError("clean commit snapshot unavailable")
            fingerprint.update(
                _canonical_bytes(
                    (path, source.identities, metadata.st_mtime_ns, metadata.st_ctime_ns, mode)
                )
            )
            tracked_metadata[path] = _snapshot_token(metadata)
        staged = self._git(
            (
                "diff",
                "--cached",
                "--name-only",
                "--no-ext-diff",
                "--no-textconv",
                "--ignore-submodules=none",
                revision,
                "--",
            ),
            max_bytes=MAX_CHANGED_PATH_OUTPUT_BYTES,
        )
        untracked = self._git(
            (
                "ls-files",
                "--others",
                "-z",
                "--exclude=/.intent/",
                "--exclude=/.intent-ci/",
                "--exclude=/.venv/",
                "--exclude=/.pytest_cache/",
                "--exclude=/.mypy_cache/",
                "--exclude=/.ruff_cache/",
            ),
            max_bytes=MAX_CHANGED_PATH_OUTPUT_BYTES,
        )
        paths = tuple(path for path in untracked.split("\0") if path)
        if (
            staged
            or len(paths) > MAX_CHANGED_PATHS
            or any(path not in self._config.test_result_paths for path in paths)
            or any(path.casefold().endswith((".pyc", ".pyo")) for path in paths)
            or revision != self._current_revision()
            or time.monotonic() > deadline
        ):
            raise ValueError("clean commit snapshot unavailable")
        # Fence earlier files after the last content read and Git subprocess. A later
        # read may otherwise hide a modification to an already-hashed file.
        for path, expected_token in tracked_metadata.items():
            target = self._directory.file(path)
            try:
                metadata = os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
                if _snapshot_token(metadata) != expected_token:
                    raise ValueError("clean commit snapshot changed")
            finally:
                target.close()
        for parent, expected_metadata in sorted(ancestors.items()):
            if self._ancestor_metadata(parent) != expected_metadata:
                raise ValueError("clean commit snapshot changed")
            fingerprint.update(_canonical_bytes((parent, expected_metadata)))
        if time.monotonic() > deadline:
            raise ValueError("clean commit snapshot unavailable")
        return "sha256:" + fingerprint.hexdigest()

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
                    binding=self.test_result_binding(),
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
        source = _reviewed_source(pin)
        interpreter = pin.interpreter
        if interpreter is None:
            raise ValueError("reviewed source is unsupported")
        if not await self._process_group_available():
            raise ValueError("test process containment is unavailable")
        if not await self._descriptor_launch_available(interpreter):
            raise ValueError("private source transport is unavailable")
        stdout = bytearray()
        stderr = bytearray()
        budget = [MAX_TEST_OUTPUT_BYTES]
        overflow = [False]
        timed_out = False
        read_descriptor, write_descriptor = _open_source_pipe()
        execution_argv = (
            "/usr/bin/python3",
            "-I",
            "-S",
            "-c",
            _PROCESS_SUPERVISOR,
            str(read_descriptor),
            pin.digest,
            str(len(source)),
            interpreter,
            *argv[1:],
        )
        process: anyio.abc.Process | None = None
        try:
            process = await anyio.open_process(
                execution_argv,
                cwd=self._root,
                env=dict(self._execution_environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(read_descriptor,),
            )
            os.close(read_descriptor)
            read_descriptor = -1
            owned_writer = write_descriptor
            write_descriptor = -1
            await _write_source_pipe(owned_writer, source)
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
            if read_descriptor >= 0:
                os.close(read_descriptor)
            if write_descriptor >= 0:
                os.close(write_descriptor)
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
        """Prove isolated pipe-to-memory source transport before reviewed use."""
        if interpreter not in _VETTED_INTERPRETERS:
            return False
        source = (
            b"import __main__, sys\n"
            b"assert globals() is vars(__main__)\n"
            b"assert __name__ == '__main__'\n"
            b"assert __file__ == __main__.__file__ == sys.argv[0]\n"
            b"assert __file__.startswith('intent-reviewed-test:')\n"
            b"assert sys.argv[1:] == ['intent-reviewed-probe']\n"
            b"assert sys.stdin.read() == ''\n"
            b"raise SystemExit(37)\n"
            if interpreter == "/usr/bin/python3"
            else b"exit 37\n"
        )
        read_descriptor, write_descriptor = _open_source_pipe()
        process: anyio.abc.Process | None = None
        try:
            process = await anyio.open_process(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    "-c",
                    _DESCRIPTOR_PROBE,
                    str(read_descriptor),
                    interpreter,
                    hashlib.sha256(source).hexdigest(),
                    str(len(source)),
                ],
                cwd="/",
                env=dict(_FIXED_ENVIRONMENT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(read_descriptor,),
            )
            os.close(read_descriptor)
            read_descriptor = -1
            owned_writer = write_descriptor
            write_descriptor = -1
            await _write_source_pipe(owned_writer, source)
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
            if read_descriptor >= 0:
                os.close(read_descriptor)
            if write_descriptor >= 0:
                os.close(write_descriptor)

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
            self.prepare_clean_commit_execution()
            try:
                binding = self.test_result_binding()
            except (OSError, ValueError):
                # Dirty local runs may report their process outcome, but cannot certify HEAD.
                binding = None
            status, exit_code, stdout, stderr = await self._execute(argv, pin)
            after = self._current_revision()
            if before != after or not self._executable_matches(pin):
                return TestRunResult(command_id=command_id, status=TestRunStatus.REJECTED)
            artifact = None
            if status is TestRunStatus.PASSED and binding is not None:
                if self.test_result_binding() != binding:
                    return TestRunResult(command_id=command_id, status=TestRunStatus.REJECTED)
                artifact = TestResultArtifact(
                    repository_id=self._repository_id,
                    commit_sha=after,
                    observed_at=at.astimezone(UTC),
                    status="passed",
                    test_ids=(command_id,),
                    author=self._config.local_actor,
                    acl=self._acl,
                    execution_snapshot=binding.execution_snapshot,
                    intent_baseline=binding.intent_baseline,
                    reviewed_commands=binding.reviewed_commands,
                )
                self._write_artifact(artifact)
                if self.test_result_binding() != binding:
                    raise ValueError("test result binding changed")
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
