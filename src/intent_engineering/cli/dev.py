"""Repository-bound lifecycle for the local ``intent dev`` control plane."""

# ruff: noqa: B008

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets
import selectors
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

import typer
import uvicorn
import yaml  # type: ignore[import-untyped]
from pydantic import ConfigDict, Field, field_validator
from starlette.types import Receive, Scope, Send

from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.cli.intent_workflow import (
    _canonical_scope,
    _provision_local_clarification_policy,
)
from intent_engineering.cli.output import OutputFormat, emit
from intent_engineering.cli.runtime import Runtime, load_readiness_runtime, load_runtime
from intent_engineering.control_plane import (
    ControlPlaneService,
    build_control_plane_app,
    control_plane_asset,
    local_repository_identity,
)
from intent_engineering.core.models import ProjectConfig, SourceRole, SourceRoleAssignment
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy import (
    ProjectAlreadyInitialized,
    ProjectNotInitialized,
    initialize_project,
)
from intent_engineering.intent_workflow.check import (
    SharedStateRestoreResult,
    SharedStateRestoreStatus,
)
from intent_engineering.intent_workflow.onboarding import (
    OnboardingRuntime,
    OnboardingState,
    inspect_onboarding,
)
from intent_engineering.intent_workflow.readiness import (
    EnsurePreset,
    EnsureRequest,
    EnsureResult,
    EnsureStatus,
    ReadinessService,
    ReadinessTarget,
)
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.secure import SecureDirectory, SecureFile, UnsafePathError
from intent_engineering.team_state.governance import (
    GovernanceRecord,
    GovernanceRegistry,
    default_governance_registry_root,
)
from intent_engineering.team_state.local_trust import local_or_environment_trust
from intent_engineering.team_state.restore import (
    TRUST_ENVIRONMENT_VARIABLE,
    EnvironmentTrustProvider,
    GitSharedStateRestorer,
    StaticTrustProvider,
    _origin_repository,
    _read_local_marker,
)

_METADATA_PATH = "cache/control-plane.json"
_MAX_METADATA_BYTES = 4096
_STARTUP_TIMEOUT_SECONDS = 10.0
_PROBE_TIMEOUT_SECONDS = 1.0
_MAX_PROCESS_INSPECTION_BYTES = 1_048_576
_INSTANCE_PATH = "/_intent/dev/instance"
_SHUTDOWN_PATH = "/_intent/dev/shutdown"
_BROWSER_BOOTSTRAP_PATH = "/_intent/browser/bootstrap"
_MAX_SHUTDOWN_BYTES = 1024
_PROCESS_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_INSTANCE_ID = re.compile(r"instance:[0-9a-f]{64}\Z")
_REPOSITORY_ID = re.compile(r"repo:sha256:[0-9a-f]{64}\Z")
_CONTROL_TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,256}\Z")
_CSP = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; object-src 'none'; script-src 'self'; "
    "style-src 'self'; connect-src 'self'"
)
_REPOSITORY_LOCKS: dict[tuple[int, int], threading.RLock] = {}
_REPOSITORY_LOCKS_GUARD = threading.Lock()
_BACKGROUND_ENTRYPOINT = (
    "import sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "from pathlib import Path;"
    "from intent_engineering.cli.dev import _automatic_child_entrypoint;"
    "raise SystemExit(_automatic_child_entrypoint(Path(sys.argv[1])))"
)
_PROVENANCE_MAGIC = b"intent-automatic-v1\x00"
_PROVENANCE_HEADER_BYTES = len(_PROVENANCE_MAGIC) + 5
_MAX_PROVENANCE_BYTES = 32 * 1024
_PROVENANCE_WAIT_SECONDS = 10.0
_PROVENANCE_LOCAL = b"L"
_PROVENANCE_TRUST = b"T"
_PROVENANCE_INVALID = b"I"
_LaunchMode = Literal["automatic", "manual_headless", "interactive", "legacy"]


def _governance_registry_root() -> Path:
    """Return the fixed account registry path; tests replace only this path factory."""
    return default_governance_registry_root()


@dataclass(frozen=True, slots=True)
class _GovernanceContext:
    registry: GovernanceRegistry
    repository_id: str | None
    directory_identity: tuple[int, int]
    record: GovernanceRecord | None


def _reset_repository_locks_after_fork() -> None:
    """Discard inherited thread locks before a child acquires a repository lock."""
    global _REPOSITORY_LOCKS, _REPOSITORY_LOCKS_GUARD
    _REPOSITORY_LOCKS = {}
    _REPOSITORY_LOCKS_GUARD = threading.Lock()


os.register_at_fork(after_in_child=_reset_repository_locks_after_fork)


class ControlPlaneProcessMetadata(StrictModel):
    """Canonical, non-authoritative locator for one repository-bound dev process."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    pid: Annotated[int, Field(ge=1)]
    process_start_id: str
    instance_id: str
    project_id: str
    repository_id: str
    origin: str
    launch_mode: _LaunchMode = "legacy"
    shared_state_status: SharedStateRestoreStatus = SharedStateRestoreStatus.NOT_REQUIRED

    @field_validator("process_start_id")
    @classmethod
    def validate_process_start_id(cls, value: str) -> str:
        if _PROCESS_DIGEST.fullmatch(value) is None:
            raise ValueError("invalid process identity")
        return value

    @field_validator("instance_id")
    @classmethod
    def validate_instance_id(cls, value: str) -> str:
        if _INSTANCE_ID.fullmatch(value) is None:
            raise ValueError("invalid process instance")
        return value

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        if _CONTROL_TEXT.fullmatch(value) is None or value != value.strip():
            raise ValueError("invalid project identity")
        return value

    @field_validator("repository_id")
    @classmethod
    def validate_repository_id(cls, value: str) -> str:
        if _REPOSITORY_ID.fullmatch(value) is None:
            raise ValueError("invalid repository identity")
        return value

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("invalid loopback origin") from error
        if (
            port is None
            or not 1 <= port <= 65535
            or value != f"http://localhost:{port}"
            or parsed.scheme != "http"
            or parsed.hostname != "localhost"
            or parsed.netloc != f"localhost:{port}"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid loopback origin")
        return value

    @field_validator("shared_state_status", mode="before")
    @classmethod
    def validate_shared_state_status(cls, value: object) -> SharedStateRestoreStatus:
        if type(value) is not str:
            if type(value) is SharedStateRestoreStatus:
                return value
            raise ValueError("invalid shared-state status")
        try:
            return SharedStateRestoreStatus(value)
        except ValueError:
            raise ValueError("invalid shared-state status") from None

    def canonical_bytes(self) -> bytes:
        """Return the only metadata representation written to the local cache."""
        return (
            json.dumps(
                self.model_dump(mode="json"),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )


class _DevUnavailable(RuntimeError):
    """One internal signal mapped to the fixed public lifecycle error."""


@dataclass(frozen=True, slots=True)
class _Reuse:
    metadata: ControlPlaneProcessMetadata


@dataclass(slots=True)
class _Started:
    runtime: Runtime | None
    metadata: ControlPlaneProcessMetadata | None = None
    metadata_bytes: bytes = b""
    bootstrap: str = ""
    listener: socket.socket | None = None
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    failures: list[BaseException] = field(default_factory=list)
    service: ControlPlaneService | None = None
    site: _ControlPlaneSite | None = None
    shutdown_requested: threading.Event = field(default_factory=threading.Event)


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metadata field")
        result[key] = value
    return result


def _read_metadata(target: SecureFile) -> ControlPlaneProcessMetadata | None:
    content = b""
    parsed: object = None
    try:
        content = target.read_optional_nonblocking(max_bytes=_MAX_METADATA_BYTES) or b""
        if not content:
            return None
        parsed = json.loads(content, object_pairs_hook=_strict_json_object)
        if type(parsed) is not dict:
            return None
        return ControlPlaneProcessMetadata.model_validate(parsed)
    except UnsafePathError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    finally:
        content = b""
        parsed = None


def _process_start_id(pid: int) -> str | None:
    """Hash an OS-observed process birth value so PID reuse cannot authenticate metadata."""
    if type(pid) is not int or pid < 1:
        return None
    executable = "/bin/ps" if Path("/bin/ps").is_file() else "ps"
    try:
        completed = subprocess.run(
            [executable, "-o", "lstart=", "-p", str(pid)],
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            check=False,
            capture_output=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    observed = completed.stdout.strip()
    if completed.returncode != 0 or not observed or b"\n" in observed:
        return None
    digest = hashlib.sha256(b"intent.dev-process.v1\x00" + observed).hexdigest()
    observed = b""
    return f"sha256:{digest}"


def _darwin_listener_owned_by_process(pid: int, port: int) -> bool:
    executable = Path("/usr/sbin/lsof")
    if not executable.is_file():
        return False
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-nP",
                "-a",
                "-p",
                str(pid),
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-FpnT",
            ],
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            check=False,
            capture_output=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = completed.stdout
    try:
        lines = output.splitlines()
        return bool(
            completed.returncode == 0
            and not completed.stderr
            and len(output) <= 4096
            and lines.count(f"p{pid}".encode("ascii")) == 1
            and f"n127.0.0.1:{port}".encode("ascii") in lines
            and b"TST=LISTEN" in lines
        )
    finally:
        output = b""
        lines = []


def _linux_listener_owned_by_process(pid: int, port: int) -> bool:
    inodes: set[str] = set()
    try:
        for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            with table.open("rb") as stream:
                content = stream.read(_MAX_PROCESS_INSPECTION_BYTES + 1)
            if len(content) > _MAX_PROCESS_INSPECTION_BYTES:
                return False
            for line in content.splitlines()[1:]:
                fields = line.split()
                if len(fields) < 10 or fields[3] != b"0A":
                    continue
                address, separator, encoded_port = fields[1].partition(b":")
                if (
                    separator
                    and int(encoded_port, 16) == port
                    and address in {b"0100007F", b"00000000000000000000000001000000"}
                ):
                    inodes.add(fields[9].decode("ascii"))
        if not inodes:
            return False
        sockets = {f"socket:[{inode}]" for inode in inodes}
        owned = False
        with os.scandir(f"/proc/{pid}/fd") as descriptors:
            for count, descriptor in enumerate(descriptors, start=1):
                if count > 4096:
                    return False
                if os.readlink(descriptor.path) in sockets:
                    owned = True
        return owned
    except (OSError, UnicodeError, ValueError):
        return False
    finally:
        inodes.clear()


def _listener_owned_by_process(pid: int, port: int) -> bool:
    """Fail closed unless the kernel binds the exact listener to the verified process."""
    if type(pid) is not int or pid < 1 or type(port) is not int or not 1 <= port <= 65535:
        return False
    if sys.platform == "darwin":
        return _darwin_listener_owned_by_process(pid, port)
    if sys.platform.startswith("linux"):
        return _linux_listener_owned_by_process(pid, port)
    return False


def _probe_json(metadata: ControlPlaneProcessMetadata, path: str, max_bytes: int) -> object:
    parsed = urlsplit(metadata.origin)
    connection: http.client.HTTPConnection | None = None
    content = b""
    payload: object = None
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            cast(int, parsed.port),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        connection.request(
            "GET",
            path,
            headers={
                "Host": parsed.netloc,
                "Origin": metadata.origin,
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        content = response.read(max_bytes + 1)
        if response.status != 200 or len(content) > max_bytes:
            return None
        payload = json.loads(content, object_pairs_hook=_strict_json_object)
        return payload
    except (OSError, http.client.HTTPException, json.JSONDecodeError, ValueError):
        return None
    finally:
        content = b""
        payload = None
        if connection is not None:
            connection.close()


def _probe(metadata: ControlPlaneProcessMetadata, expected_repository_id: str) -> bool:
    """Verify OS process birth and a live exact repository projection at the loopback origin."""
    parsed = urlsplit(metadata.origin)
    port = cast(int, parsed.port)
    if (
        metadata.repository_id != expected_repository_id
        or _process_start_id(metadata.pid) != metadata.process_start_id
        or not _listener_owned_by_process(metadata.pid, port)
    ):
        return False
    instance = _probe_json(metadata, _INSTANCE_PATH, 1024)
    expected = {
        "schema_version": 1,
        "instance_id": metadata.instance_id,
        "project_id": metadata.project_id,
        "repository_id": expected_repository_id,
        "launch_mode": metadata.launch_mode,
        "shared_state_status": metadata.shared_state_status.value,
    }
    if instance == expected:
        return True
    if metadata.shared_state_status is SharedStateRestoreStatus.NOT_REQUIRED:
        expected.pop("shared_state_status")
        if instance == expected:
            return True
    if metadata.launch_mode != "legacy":
        return False
    expected.pop("launch_mode")
    expected.pop("shared_state_status", None)
    return instance == expected


def _shutdown_payload(metadata: ControlPlaneProcessMetadata) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "instance_id": metadata.instance_id,
            "project_id": metadata.project_id,
            "repository_id": metadata.repository_id,
            "launch_mode": "automatic",
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _remove_exact_metadata(target: SecureFile, expected: bytes) -> None:
    current = b""
    try:
        with same_path_lock(target):
            current = target.read_optional_nonblocking(max_bytes=_MAX_METADATA_BYTES) or b""
            if current == expected:
                target.unlink(missing_ok=True)
    except (OSError, UnsafePathError):
        return
    finally:
        current = expected = b""


def _discard_stale_metadata(target: SecureFile) -> None:
    """Remove only the authenticated regular cache entry currently held by the startup lock."""
    content = target.read_optional_nonblocking(max_bytes=_MAX_METADATA_BYTES)
    if content is not None:
        target.unlink()


def _read_project_config(workspace: SecureDirectory) -> tuple[ProjectConfig, bytes]:
    target = workspace.file("config.yaml")
    content = b""
    loaded: dict[str, Any] | None = None
    try:
        content = target.read_bytes_nonblocking(max_bytes=1_048_576)
        loaded = load_strict_yaml_mapping_bytes(content)
        config = ProjectConfig.model_validate_json(
            json.dumps(loaded, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )
        return config, content
    finally:
        content = b""
        loaded = None
        target.close()


def _configure_prd_role(
    workspace: SecureDirectory,
    config: ProjectConfig,
    preimage: bytes,
    prd: str,
) -> ProjectConfig:
    scope = _canonical_scope(prd, markdown_file=True)
    assignment = SourceRoleAssignment(
        connector_id="markdown",
        scope=scope,
        role=SourceRole.DECLARED_INTENT,
        inherited=False,
    )
    retained = tuple(
        item
        for item in config.source_roles
        if (item.connector_id, item.scope) != (assignment.connector_id, assignment.scope)
    )
    updated = config.model_copy(update={"source_roles": (*retained, assignment)})
    updated = ProjectConfig.model_validate_json(updated.model_dump_json())
    if updated == config:
        return config
    target = workspace.file("config.yaml")
    encoded = b""
    try:
        encoded = cast(
            str,
            yaml.safe_dump(updated.model_dump(mode="json"), allow_unicode=True, sort_keys=True),
        ).encode("utf-8")
        with same_path_lock(target):
            if target.read_bytes_nonblocking(max_bytes=1_048_576) != preimage:
                raise _DevUnavailable()
            target.atomic_write(encoded, reject_target_races=True)
        return updated
    finally:
        preimage = encoded = b""
        target.close()


def _ensure_workspace(project: Path, prd: str | None) -> None:
    try:
        workspace = SecureDirectory.open(project).subdirectory(".intent")
    except (FileNotFoundError, UnsafePathError):
        if prd is None:
            raise ProjectNotInitialized("local project is not initialized") from None
        try:
            initialize_project(project)
        except ProjectAlreadyInitialized:
            # A concurrent first start may have completed initialization.
            pass
    else:
        workspace.close()


@contextmanager
def _try_repository_lifecycle_lock(directory: SecureDirectory) -> Iterator[bool]:
    """Try to acquire the repository lease without blocking attested reuse."""
    identity = os.fstat(directory.descriptor)
    key = (identity.st_dev, identity.st_ino)
    with _REPOSITORY_LOCKS_GUARD:
        thread_lock = _REPOSITORY_LOCKS.setdefault(key, threading.RLock())
    if not thread_lock.acquire(blocking=False):
        yield False
        return
    descriptor = -1
    locked = False
    try:
        descriptor = os.dup(directory.descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        locked = True
        yield True
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            thread_lock.release()


class _ControlPlaneSite:
    """Serve three packaged assets and otherwise preserve the strict Task 5 API unchanged."""

    def __init__(
        self,
        api: object,
        *,
        origin: str,
        csrf_secret: str,
        instance_id: str,
        project_id: str,
        repository_id: str,
        launch_mode: _LaunchMode,
        shared_state_status: SharedStateRestoreStatus,
        shutdown_requested: threading.Event,
    ) -> None:
        self._api = cast(Any, api)
        self._origin = origin
        self._host = origin.removeprefix("http://").encode("ascii")
        self._csrf = csrf_secret
        self._launch_mode = launch_mode
        self._shutdown_requested = shutdown_requested
        self._bootstrap_guard = threading.Lock()
        self._instance = json.dumps(
            {
                "schema_version": 1,
                "instance_id": instance_id,
                "project_id": project_id,
                "repository_id": repository_id,
                "launch_mode": launch_mode,
                "shared_state_status": shared_state_status.value,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._assets = {
            "/": (control_plane_asset("index.html"), b"text/html; charset=utf-8"),
            "/index.html": (control_plane_asset("index.html"), b"text/html; charset=utf-8"),
            "/app.js": (control_plane_asset("app.js"), b"text/javascript; charset=utf-8"),
            "/styles.css": (control_plane_asset("styles.css"), b"text/css; charset=utf-8"),
        }
        self._shutdown_body = json.dumps(
            {
                "schema_version": 1,
                "instance_id": instance_id,
                "project_id": project_id,
                "repository_id": repository_id,
                "launch_mode": "automatic",
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._bootstrap_body = json.dumps(
            {"token": csrf_secret},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    async def _browser_bootstrap(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Consume the in-memory fragment proof once before issuing the HttpOnly cookie."""
        status = 403
        accepted = False
        content = bytearray()
        expected = b""
        chunk = b""
        message: Any = {}
        signal_error: BaseException | None = None
        try:
            headers = scope.get("headers")
            expected_length = len(self._bootstrap_body)
            if (
                scope.get("type") != "http"
                or scope.get("method") != "POST"
                or scope.get("scheme") != "http"
                or scope.get("raw_path") != _BROWSER_BOOTSTRAP_PATH.encode("ascii")
                or scope.get("query_string") != b""
                or type(headers) is not list
            ):
                raise ValueError
            hosts = [value for name, value in headers if name.lower() == b"host"]
            origins = [value for name, value in headers if name.lower() == b"origin"]
            content_types = [value for name, value in headers if name.lower() == b"content-type"]
            lengths = [value for name, value in headers if name.lower() == b"content-length"]
            transfer_encodings = [
                value for name, value in headers if name.lower() == b"transfer-encoding"
            ]
            if (
                expected_length == 0
                or hosts != [self._host]
                or origins != [self._origin.encode("ascii")]
                or content_types != [b"application/json"]
                or lengths != [str(expected_length).encode("ascii")]
                or transfer_encodings
            ):
                raise ValueError
            while True:
                message = await receive()
                if message.get("type") != "http.request":
                    raise ValueError
                chunk = message.get("body", b"")
                if type(chunk) is not bytes:
                    raise ValueError
                content.extend(chunk)
                if len(content) > _MAX_SHUTDOWN_BYTES:
                    raise ValueError
                if not message.get("more_body", False):
                    break
            with self._bootstrap_guard:
                expected = self._bootstrap_body
                if not secrets.compare_digest(bytes(content), expected):
                    raise ValueError
                self._bootstrap_body = b""
                accepted = True
                status = 204
        except (TypeError, ValueError, UnicodeError):
            status = 403
        except BaseException as error:  # noqa: BLE001 - scrub token-bearing cancellation frames
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            signal_error = error
        finally:
            if content:
                content[:] = b"\x00" * len(content)
            content.clear()
            expected = b""
            chunk = b""
            message = {}
        if signal_error is not None:
            caught = signal_error
            signal_error = None
            raise caught.with_traceback(None)
        headers = [
            (b"content-length", b"0"),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"content-security-policy", _CSP.encode("ascii")),
            (b"referrer-policy", b"no-referrer"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"cross-origin-resource-policy", b"same-origin"),
        ]
        if accepted:
            headers.append(
                (
                    b"set-cookie",
                    f"intent_csrf={self._csrf}; Path=/; HttpOnly; SameSite=Strict".encode("ascii"),
                )
            )
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    async def _shutdown(self, scope: Scope, receive: Receive, send: Send) -> None:
        status = 403
        body = b'{"schema_version":1,"status":"rejected"}'
        accepted = False
        content = bytearray()
        try:
            headers = scope.get("headers")
            raw_path = scope.get("raw_path")
            if (
                self._launch_mode != "automatic"
                or scope.get("type") != "http"
                or scope.get("method") != "POST"
                or scope.get("scheme") != "http"
                or raw_path != _SHUTDOWN_PATH.encode("ascii")
                or scope.get("query_string") != b""
                or type(headers) is not list
            ):
                raise ValueError
            hosts = [value for name, value in headers if name.lower() == b"host"]
            origins = [value for name, value in headers if name.lower() == b"origin"]
            content_types = [value for name, value in headers if name.lower() == b"content-type"]
            lengths = [value for name, value in headers if name.lower() == b"content-length"]
            if (
                hosts != [self._host]
                or origins != [self._origin.encode("ascii")]
                or content_types != [b"application/json"]
                or lengths != [str(len(self._shutdown_body)).encode("ascii")]
            ):
                raise ValueError
            while True:
                message = await receive()
                if message.get("type") != "http.request":
                    raise ValueError
                chunk = message.get("body", b"")
                if type(chunk) is not bytes:
                    raise ValueError
                content.extend(chunk)
                if len(content) > _MAX_SHUTDOWN_BYTES:
                    raise ValueError
                if not message.get("more_body", False):
                    break
            if bytes(content) != self._shutdown_body:
                raise ValueError
            status = 202
            body = json.dumps(
                {
                    "schema_version": 1,
                    "status": "stopping",
                    "instance_id": json.loads(self._shutdown_body)["instance_id"],
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            accepted = True
        except (TypeError, ValueError, UnicodeError):
            status = 403
        finally:
            content.clear()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
        if accepted:
            self._shutdown_requested.set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if type(scope) is dict and scope.get("type") == "lifespan":
            await self._api(scope, receive, send)
            return
        path = scope.get("path") if type(scope) is dict else None
        if type(path) is str and path.startswith("/api/"):
            await self._api(scope, receive, send)
            return
        if path == _SHUTDOWN_PATH:
            await self._shutdown(scope, receive, send)
            return
        if path == _BROWSER_BOOTSTRAP_PATH:
            await self._browser_bootstrap(scope, receive, send)
            return
        status = 404
        body = b'{"schema_version":1,"status":"rejected","reason":"request_unavailable"}'
        media_type = b"application/json"
        try:
            headers = scope.get("headers")
            raw_path = scope.get("raw_path")
            if (
                type(path) is not str
                or type(raw_path) is not bytes
                or raw_path != path.encode("ascii")
                or scope.get("type") != "http"
                or scope.get("method") != "GET"
                or scope.get("scheme") != "http"
                or scope.get("query_string") != b""
                or type(headers) is not list
            ):
                raise ValueError
            hosts = [value for name, value in headers if name.lower() == b"host"]
            origins = [value for name, value in headers if name.lower() == b"origin"]
            if hosts != [self._host] or (origins and origins != [self._origin.encode("ascii")]):
                raise ValueError
            asset = (
                (self._instance, b"application/json")
                if path == _INSTANCE_PATH
                else self._assets.get(path)
            )
            if asset is not None:
                body, media_type = asset
                status = 200
        except (TypeError, ValueError, UnicodeError):
            status = 403
        response_headers = [
            (b"content-type", media_type),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"content-security-policy", _CSP.encode("ascii")),
            (b"referrer-policy", b"no-referrer"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"cross-origin-resource-policy", b"same-origin"),
        ]
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": body})

    def close(self) -> None:
        self._api = None
        self._origin = ""
        self._host = b""
        self._csrf = ""
        self._launch_mode = "legacy"
        self._instance = b""
        self._shutdown_body = b""
        self._bootstrap_body = b""
        self._shutdown_requested.clear()
        self._assets.clear()


def _listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.set_inheritable(False)
        return listener
    except BaseException:
        listener.close()
        raise


def _server_thread(
    server: uvicorn.Server, listener: socket.socket
) -> tuple[threading.Thread, list[BaseException]]:
    failures: list[BaseException] = []

    def run() -> None:
        try:
            server.run(sockets=[listener])
        except BaseException as error:  # noqa: BLE001 - transferred to the owning main thread
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            failures.append(error)

    thread = threading.Thread(target=run, name="intent-dev-loopback", daemon=True)
    thread.start()
    return thread, failures


def _wait_until_started(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server.started:
            return
        if not thread.is_alive():
            break
        time.sleep(0.01)
    raise _DevUnavailable()


def _start(
    started: _Started,
    target: SecureFile,
    repository_id: str,
    prd: str | None,
    launch_mode: _LaunchMode,
    shared_state_status: SharedStateRestoreStatus,
) -> None:
    """Populate caller-owned lifecycle state before publishing its locator."""
    runtime = started.runtime
    if runtime is None:
        raise _DevUnavailable()
    onboarding = inspect_onboarding(cast(OnboardingRuntime, runtime))
    if prd is not None and onboarding.state is OnboardingState.REQUIRED:
        _provision_local_clarification_policy(runtime, runtime.config)
    started.listener = _listener()
    port = cast(tuple[str, int], started.listener.getsockname())[1]
    origin = f"http://localhost:{port}"
    started.bootstrap = secrets.token_urlsafe(32)
    instance_id = f"instance:{secrets.token_hex(32)}"
    started.service = ControlPlaneService(
        runtime,
        origin=origin,
        shared_state_status=shared_state_status,
    )
    if prd is not None and onboarding.state is OnboardingState.REQUIRED:
        started.service.onboard_preview(prd)
    api = build_control_plane_app(
        started.service,
        origin=origin,
        csrf_secret=started.bootstrap,
    )
    started.site = _ControlPlaneSite(
        api,
        origin=origin,
        csrf_secret=started.bootstrap,
        instance_id=instance_id,
        project_id=runtime.config.project_id,
        repository_id=repository_id,
        launch_mode=launch_mode,
        shared_state_status=shared_state_status,
        shutdown_requested=started.shutdown_requested,
    )
    configuration = uvicorn.Config(
        started.site,
        host="127.0.0.1",
        port=port,
        loop="asyncio",
        lifespan="on",
        log_config=None,
        log_level="critical",
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )
    started.server = uvicorn.Server(configuration)
    started.thread, started.failures = _server_thread(started.server, started.listener)
    _wait_until_started(started.server, started.thread)
    started.service.start_development_observation()
    process_start_id = _process_start_id(os.getpid())
    if process_start_id is None:
        raise _DevUnavailable()
    started.metadata = ControlPlaneProcessMetadata(
        pid=os.getpid(),
        process_start_id=process_start_id,
        instance_id=instance_id,
        project_id=runtime.config.project_id,
        repository_id=repository_id,
        origin=origin,
        launch_mode=launch_mode,
        shared_state_status=shared_state_status,
    )
    if not _probe(started.metadata, repository_id):
        raise _DevUnavailable()
    started.metadata_bytes = started.metadata.canonical_bytes()
    target.atomic_write(started.metadata_bytes, reject_target_races=True)
    runtime = None


def _shutdown_started_locked(started: _Started, target: SecureFile) -> None:
    """Quiesce one owned process under its repository lock, then remove its exact locator."""
    server = started.server
    thread = started.thread
    if server is not None:
        server.should_exit = True
    if thread is not None:
        thread.join(timeout=10)
        if thread.is_alive():
            raise _DevUnavailable()
    if started.service is not None:
        started.service.close()
        started.service = None
    if started.site is not None:
        started.site.close()
        started.site = None
    if started.runtime is not None:
        started.runtime.close()
        started.runtime = None
    if started.listener is not None:
        try:
            started.listener.close()
        except OSError:
            pass
        started.listener = None
    if started.metadata_bytes:
        _remove_exact_metadata(target, started.metadata_bytes)
    started.metadata = None
    started.metadata_bytes = b""
    started.bootstrap = ""
    started.server = None
    started.thread = None
    started.failures.clear()
    started.shutdown_requested.clear()


def _wait_for_exit(started: _Started) -> None:
    signal_error: BaseException | None = None
    thread = started.thread
    server = started.server
    if thread is None or server is None:
        raise _DevUnavailable()
    try:
        while thread.is_alive():
            if started.shutdown_requested.is_set():
                server.should_exit = True
            thread.join(timeout=0.25)
        if started.failures:
            failure = started.failures.pop(0)
            if isinstance(failure, Exception):
                raise _DevUnavailable() from None
            raise failure.with_traceback(None)
    except KeyboardInterrupt:
        return
    except BaseException as error:  # noqa: BLE001 - exact cancellation after owned cleanup
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        signal_error = error
    if signal_error is not None:
        caught = signal_error
        signal_error = None
        raise caught.with_traceback(None)


def _open_browser(origin: str, bootstrap: str) -> bool:
    opened = False
    try:
        opened = bool(webbrowser.open(f"{origin}/#csrf={bootstrap}", new=2, autoraise=True))
    except Exception:  # noqa: BLE001 - one fixed browser diagnostic
        opened = False
    finally:
        bootstrap = ""
    return opened


def _fixed_error(message: str = "unavailable") -> None:
    typer.echo(f"intent dev: {message}", err=True)
    raise typer.Exit(1)


def _live_repository_process(
    project_directory: SecureDirectory,
) -> ControlPlaneProcessMetadata | None:
    """Optimistically attest a published owner without waiting on its lifecycle lease."""
    workspace: SecureDirectory | None = None
    target: SecureFile | None = None
    config_bytes = b""
    repository_id = ""
    current: ControlPlaneProcessMetadata | None = None
    try:
        workspace = project_directory.subdirectory(".intent")
        target = workspace.file(_METADATA_PATH)
        config, config_bytes = _read_project_config(workspace)
        repository_id = local_repository_identity(config.project_id, project_directory.identity)
        current = _read_metadata(target)
        return current if current is not None and _probe(current, repository_id) else None
    except (FileNotFoundError, UnsafePathError):
        return None
    finally:
        current = None
        repository_id = ""
        del config_bytes
        if target is not None:
            target.close()
        if workspace is not None:
            workspace.close()


def _request_interactive_takeover(
    metadata: ControlPlaneProcessMetadata,
    expected_repository_id: str,
) -> bool:
    """Ask one freshly re-attested owner to release its repository lifecycle lease."""
    if metadata.launch_mode != "automatic" or metadata.pid == os.getpid():
        return False
    if not _probe(metadata, expected_repository_id):
        return True
    parsed = urlsplit(metadata.origin)
    connection: http.client.HTTPConnection | None = None
    content = b""
    body = _shutdown_payload(metadata)
    payload: object = None
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            cast(int, parsed.port),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        connection.request(
            "POST",
            _SHUTDOWN_PATH,
            body=body,
            headers={
                "Host": parsed.netloc,
                "Origin": metadata.origin,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        content = response.read(_MAX_SHUTDOWN_BYTES + 1)
        if response.status != 202 or len(content) > _MAX_SHUTDOWN_BYTES:
            return False
        payload = json.loads(content, object_pairs_hook=_strict_json_object)
        return payload == {
            "schema_version": 1,
            "status": "stopping",
            "instance_id": metadata.instance_id,
        }
    except (OSError, http.client.HTTPException, json.JSONDecodeError, ValueError):
        return False
    finally:
        body = content = b""
        payload = None
        if connection is not None:
            connection.close()


def _run_with_repository_lease(
    root: Path,
    project_directory: SecureDirectory,
    *,
    prd_value: str | None,
    no_open: bool,
    status: bool,
    launch_mode: _LaunchMode,
    offline: bool,
    prevalidated_restore: SharedStateRestoreResult | None,
) -> None:
    """Recheck and own one process until every repository resource is quiescent."""
    workspace_directory: SecureDirectory | None = None
    target: SecureFile | None = None
    runtime: Runtime | None = None
    started: _Started | None = None
    origin = ""
    config_bytes = b""
    repository_id = ""
    current: ControlPlaneProcessMetadata | None = None
    restore = SharedStateRestoreResult(status=SharedStateRestoreStatus.NOT_REQUIRED)
    try:
        if not status:
            if prevalidated_restore is not None:
                marker_status = _untrusted_shared_marker_status(root)
                if (
                    prevalidated_restore.status is SharedStateRestoreStatus.VERIFIED
                    and marker_status is not SharedStateRestoreStatus.UNAVAILABLE
                ):
                    raise _DevUnavailable()
                if (
                    prevalidated_restore.status is SharedStateRestoreStatus.NOT_REQUIRED
                    and marker_status is not SharedStateRestoreStatus.NOT_REQUIRED
                ):
                    raise _DevUnavailable()
                restore = prevalidated_restore
            else:
                restore = _shared_restore(root, refresh_remote=not offline)
        _ensure_workspace(root, None if status else prd_value)
        workspace_directory = project_directory.subdirectory(".intent")
        target = workspace_directory.file(_METADATA_PATH)
        with same_path_lock(target):
            config, config_bytes = _read_project_config(workspace_directory)
            repository_id = local_repository_identity(config.project_id, project_directory.identity)
            current = _read_metadata(target)
            if current is not None and _probe(current, repository_id):
                if status:
                    typer.echo(f"intent dev: running at {current.origin}")
                    return
                typer.echo(f"intent dev: already running at {current.origin}")
                return
            if status:
                if current is not None:
                    _discard_stale_metadata(target)
                _fixed_error("not running")
            if current is not None:
                _discard_stale_metadata(target)
            if prd_value is not None:
                config = _configure_prd_role(
                    workspace_directory,
                    config,
                    config_bytes,
                    prd_value,
                )
            runtime = load_runtime(root)
            if runtime.config != config:
                raise _DevUnavailable()
            started = _Started(runtime=runtime)
            runtime = None
            _start(
                started,
                target,
                repository_id,
                prd_value,
                launch_mode,
                restore.status,
            )
        if started.metadata is None:
            raise _DevUnavailable()
        origin = started.metadata.origin
        typer.echo(f"intent dev: ready at {origin}")
        if not no_open and not _open_browser(origin, started.bootstrap):
            typer.echo("intent dev: browser unavailable", err=True)
        _wait_for_exit(started)
    finally:
        if started is not None and target is not None:
            _shutdown_started_locked(started, target)
        elif runtime is not None:
            runtime.close()
        runtime = None
        started = None
        origin = ""
        current = None
        repository_id = ""
        restore = SharedStateRestoreResult(status=SharedStateRestoreStatus.NOT_REQUIRED)
        del config_bytes
        if target is not None:
            target.close()
        if workspace_directory is not None:
            workspace_directory.close()


def ensure_command(
    project: Path = typer.Option(Path("."), "--project"),
    preset: EnsurePreset = typer.Option(EnsurePreset.DEVELOPER, "--preset"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Restore, validate and make the developer control plane ready without authority."""
    root = Path(os.path.abspath(project))
    emit(_developer_readiness(root, preset), output_format)


def _readiness_result(
    status: EnsureStatus,
    route: ReadinessTarget,
    *,
    graph_version: int = 0,
    pending_proposal_ids: tuple[str, ...] = (),
    open_case_ids: tuple[str, ...] = (),
) -> EnsureResult:
    return EnsureResult(
        status=status,
        attention_route=route,
        graph_version=graph_version,
        pending_proposal_ids=pending_proposal_ids,
        open_case_ids=open_case_ids,
    )


def _repository_marker_status(root: Path) -> SharedStateRestoreStatus:
    """Inspect only the bounded repository-local marker."""
    project: SecureDirectory | None = None
    workspace: SecureDirectory | None = None
    try:
        project = SecureDirectory.open(root)
        try:
            os.stat(".intent", dir_fd=project.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return SharedStateRestoreStatus.NOT_REQUIRED
        workspace = project.subdirectory(".intent")
        try:
            os.stat("cache", dir_fd=workspace.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return SharedStateRestoreStatus.NOT_REQUIRED
        marker = _read_local_marker(workspace)
        return (
            SharedStateRestoreStatus.UNAVAILABLE
            if marker is not None
            else SharedStateRestoreStatus.NOT_REQUIRED
        )
    except (OSError, UnicodeError, ValueError, UnsafePathError):
        return SharedStateRestoreStatus.INVALID
    finally:
        if workspace is not None:
            workspace.close()
        if project is not None:
            project.close()


def _governance_context(root: Path) -> _GovernanceContext:
    """Bind durable governance evidence to the canonical remote or exact checkout."""
    project = SecureDirectory.open(root)
    try:
        repository_id: str | None = None
        try:
            repository_id = _origin_repository(root)
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
            pass
        registry = GovernanceRegistry(_governance_registry_root())
        record = registry.lookup(repository_id, project.identity).record
        if (
            record is not None
            and repository_id is not None
            and record.repository_id != repository_id
        ):
            raise UnsafePathError()
        return _GovernanceContext(registry, repository_id, project.identity, record)
    finally:
        project.close()


def _untrusted_shared_marker_status(root: Path) -> SharedStateRestoreStatus:
    """Detect governed state from repository cache or durable owner-only evidence."""
    marker_status = _repository_marker_status(root)
    if marker_status is SharedStateRestoreStatus.INVALID:
        return marker_status
    try:
        context = _governance_context(root)
    except (OSError, UnicodeError, ValueError, UnsafePathError):
        initialized = os.path.lexists(root / ".intent")
        return (
            SharedStateRestoreStatus.INVALID
            if marker_status is SharedStateRestoreStatus.UNAVAILABLE or initialized
            else marker_status
        )
    return SharedStateRestoreStatus.UNAVAILABLE if context.record is not None else marker_status


def _remember_verified_governance(root: Path, context: _GovernanceContext, project_id: str) -> None:
    """Persist only post-verification, non-secret lineage metadata."""
    if context.repository_id is None:
        raise UnsafePathError()
    project: SecureDirectory | None = None
    workspace: SecureDirectory | None = None
    try:
        project = SecureDirectory.open(root)
        workspace = project.subdirectory(".intent")
        marker = _read_local_marker(workspace)
        if marker is None:
            raise UnsafePathError()
        context.registry.remember(
            repository_id=context.repository_id,
            project_id=project_id,
            directory_identity=context.directory_identity,
            marker=marker,
        )
    finally:
        if workspace is not None:
            workspace.close()
        if project is not None:
            project.close()


def _shared_restore(
    root: Path,
    *,
    refresh_remote: bool = True,
    environment: Mapping[str, str] | None = None,
) -> SharedStateRestoreResult:
    marker_status = _untrusted_shared_marker_status(root)
    if marker_status is SharedStateRestoreStatus.INVALID:
        return SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
    trust = None
    try:
        try:
            trust = local_or_environment_trust(root, environment).load()
        except Exception as error:  # noqa: BLE001 - fixed secret-free trust failure boundary
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            return SharedStateRestoreResult(
                status=(
                    SharedStateRestoreStatus.UNAVAILABLE
                    if marker_status is SharedStateRestoreStatus.UNAVAILABLE
                    else SharedStateRestoreStatus.INVALID
                )
            )
        if trust is None:
            return SharedStateRestoreResult(
                status=(
                    SharedStateRestoreStatus.UNAVAILABLE
                    if os.path.lexists(root / ".intent/team-trust.json")
                    else marker_status
                )
            )
        context = _governance_context(root)
        if context.repository_id != trust.repository_id or (
            context.record is not None
            and (
                context.record.repository_id != trust.repository_id
                or context.record.project_id != trust.project_id
            )
        ):
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
        result = GitSharedStateRestorer(
            StaticTrustProvider(trust),
            refresh_remote=refresh_remote,
            prior_marker=context.record.marker() if context.record is not None else None,
        ).verify_and_restore_approved_baseline(root)
        if type(result) is SharedStateRestoreResult:
            if result.status is SharedStateRestoreStatus.VERIFIED:
                _remember_verified_governance(root, context, trust.project_id)
                if not refresh_remote:
                    return SharedStateRestoreResult(status=SharedStateRestoreStatus.STALE)
            return result
    except Exception as error:  # noqa: BLE001 - fixed secret-free restore boundary
        error.__traceback__ = None
        return SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
    finally:
        trust = None
    return SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)


def _background_environment() -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }


def _background_argv(root: Path) -> tuple[str, ...]:
    source_root = Path(__file__).resolve().parents[2]
    return (
        sys.executable,
        "-I",
        "-c",
        _BACKGROUND_ENTRYPOINT,
        str(source_root),
        str(root),
    )


def _background_provenance_bytes() -> bytearray:
    """Frame trust for one child without placing authority in argv, env, or disk."""
    raw = os.environ.get(TRUST_ENVIRONMENT_VARIABLE)
    kind = _PROVENANCE_LOCAL
    payload = bytearray()
    try:
        if raw is not None:
            kind = _PROVENANCE_TRUST
            try:
                payload.extend(raw.encode("utf-8"))
            except UnicodeEncodeError:
                kind = _PROVENANCE_INVALID
            if not payload or len(payload) > _MAX_PROVENANCE_BYTES:
                payload[:] = b"\x00" * len(payload)
                payload.clear()
                kind = _PROVENANCE_INVALID
        return bytearray(_PROVENANCE_MAGIC + kind + struct.pack(">I", len(payload))) + payload
    finally:
        raw = None
        payload[:] = b"\x00" * len(payload)
        payload.clear()


def _inherited_provenance_descriptors() -> tuple[int, ...]:
    """Find the sole anonymous pipe deliberately retained across the isolated exec."""
    names: list[str] | None = None
    for directory in ("/dev/fd", "/proc/self/fd"):
        try:
            names = os.listdir(directory)
            break
        except OSError:
            continue
    if names is None:
        return ()
    candidates: list[int] = []
    for name in names:
        if not name.isascii() or not name.isdigit():
            continue
        descriptor = int(name)
        if descriptor <= 2:
            continue
        try:
            metadata = os.fstat(descriptor)
        except OSError:
            continue
        if stat.S_ISFIFO(metadata.st_mode):
            candidates.append(descriptor)
    return tuple(sorted(candidates))


def _consume_background_provenance() -> tuple[bytes, bytearray]:
    """Consume and close exactly one bounded inherited automatic-launch proof."""
    descriptors = _inherited_provenance_descriptors()
    content = bytearray()
    selector: selectors.BaseSelector | None = None
    try:
        if len(descriptors) != 1:
            raise _DevUnavailable()
        descriptor = descriptors[0]
        os.set_blocking(descriptor, False)
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + _PROVENANCE_WAIT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise _DevUnavailable()
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > _PROVENANCE_HEADER_BYTES + _MAX_PROVENANCE_BYTES:
                raise _DevUnavailable()
        if len(content) < _PROVENANCE_HEADER_BYTES:
            raise _DevUnavailable()
        magic_end = len(_PROVENANCE_MAGIC)
        if bytes(content[:magic_end]) != _PROVENANCE_MAGIC:
            raise _DevUnavailable()
        kind = bytes(content[magic_end : magic_end + 1])
        declared = struct.unpack(">I", content[magic_end + 1 : magic_end + 5])[0]
        payload = bytearray(content[_PROVENANCE_HEADER_BYTES:])
        if declared != len(payload) or kind not in {
            _PROVENANCE_LOCAL,
            _PROVENANCE_TRUST,
            _PROVENANCE_INVALID,
        }:
            payload[:] = b"\x00" * len(payload)
            payload.clear()
            raise _DevUnavailable()
        if kind != _PROVENANCE_TRUST and payload:
            payload[:] = b"\x00" * len(payload)
            payload.clear()
            raise _DevUnavailable()
        return kind, payload
    except _DevUnavailable:
        raise
    except (OSError, struct.error):
        raise _DevUnavailable() from None
    finally:
        content[:] = b"\x00" * len(content)
        content.clear()
        if selector is not None:
            selector.close()
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _automatic_shared_restore(root: Path) -> SharedStateRestoreResult:
    """Derive child health from local markers or authenticated shared-state bytes."""
    kind = b""
    payload = bytearray()
    trust = None
    environment: dict[str, str] = {}
    raw = ""
    cancellation: BaseException | None = None
    result = SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
    try:
        kind, payload = _consume_background_provenance()
        if kind == _PROVENANCE_LOCAL:
            result = _shared_restore(root, environment={})
        elif kind == _PROVENANCE_INVALID:
            result = SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
        else:
            raw = payload.decode("utf-8")
            environment[TRUST_ENVIRONMENT_VARIABLE] = raw
            trust = EnvironmentTrustProvider(environment).load()
            if trust is None:
                raise ValueError("shared-state trust unavailable")
            context = _governance_context(root)
            if context.repository_id != trust.repository_id or (
                context.record is not None
                and (
                    context.record.repository_id != trust.repository_id
                    or context.record.project_id != trust.project_id
                )
            ):
                raise ValueError("shared-state governance mismatch")
            result = GitSharedStateRestorer(
                StaticTrustProvider(trust),
                refresh_remote=True,
                prior_marker=context.record.marker() if context.record is not None else None,
            ).verify_and_restore_approved_baseline(root)
    except _DevUnavailable:
        raise
    except (OSError, UnicodeError, ValueError):
        result = SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
    except Exception as error:  # noqa: BLE001 - fixed secret-free child failure boundary
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        result = SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
    except BaseException as error:  # noqa: BLE001 - scrub proof before cancellation
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        cancellation = error
    finally:
        trust = None
        environment.clear()
        raw = ""
        kind = b""
        payload[:] = b"\x00" * len(payload)
        payload.clear()
    if cancellation is not None:
        caught = cancellation
        cancellation = None
        raise caught.with_traceback(None)
    return result


def _running_service(root: Path) -> ControlPlaneProcessMetadata | None:
    project_directory: SecureDirectory | None = None
    try:
        project_directory = SecureDirectory.open(root)
        return _live_repository_process(project_directory)
    except (OSError, UnsafePathError):
        return None
    finally:
        if project_directory is not None:
            project_directory.close()


def _start_or_reuse_background_service(
    root: Path, shared_state_status: SharedStateRestoreStatus
) -> bool:
    current = _running_service(root)
    if current is not None:
        if current.shared_state_status is shared_state_status:
            return True
        if current.launch_mode != "automatic" or not _request_interactive_takeover(
            current, current.repository_id
        ):
            return False
    if os.name != "posix":
        return False
    process: subprocess.Popen[bytes] | None = None
    provenance_read = -1
    provenance_write = -1
    provenance = bytearray()
    cancellation: BaseException | None = None
    ready = False
    try:
        provenance = _background_provenance_bytes()
        provenance_read, provenance_write = os.pipe()
        os.set_inheritable(provenance_read, False)
        os.set_inheritable(provenance_write, False)
        process = subprocess.Popen(
            _background_argv(root),
            cwd="/",
            env=_background_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(provenance_read,),
            start_new_session=True,
        )
        os.close(provenance_read)
        provenance_read = -1
        view = memoryview(provenance)
        try:
            written = 0
            while written < len(view):
                count = os.write(provenance_write, view[written:])
                if count <= 0:
                    raise OSError("automatic provenance unavailable")
                written += count
        finally:
            view.release()
        os.close(provenance_write)
        provenance_write = -1
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            current = _running_service(root)
            if current is not None and current.shared_state_status is shared_state_status:
                ready = True
                return True
            if process.poll() is not None:
                current = _running_service(root)
                return current is not None and current.shared_state_status is shared_state_status
            time.sleep(0.01)
        return False
    except (OSError, subprocess.SubprocessError):
        return False
    except BaseException as error:  # noqa: BLE001 - scrub inherited proof before cancellation
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        cancellation = error
    finally:
        if provenance_read >= 0:
            os.close(provenance_read)
        if provenance_write >= 0:
            os.close(provenance_write)
        provenance[:] = b"\x00" * len(provenance)
        provenance.clear()
        if process is not None and not ready and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        process = None
    if cancellation is not None:
        caught = cancellation
        cancellation = None
        raise caught.with_traceback(None)
    return False


def _automatic_child_entrypoint(root: Path) -> int:
    """Run the descriptor-authorized background lifecycle with child-earned health."""
    restore = SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
    project_directory: SecureDirectory | None = None
    current: ControlPlaneProcessMetadata | None = None
    try:
        restore = _automatic_shared_restore(Path(os.path.abspath(root)))
        root = Path(os.path.abspath(root))
        project_directory = SecureDirectory.open(root)
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        takeover_requested: set[tuple[int, str, str]] = set()
        while True:
            current = _live_repository_process(project_directory)
            if current is not None:
                if current.shared_state_status is restore.status:
                    return 0
                if current.launch_mode != "automatic":
                    return 1
                owner = (current.pid, current.process_start_id, current.instance_id)
                if owner not in takeover_requested:
                    if not _request_interactive_takeover(current, current.repository_id):
                        return 1
                    takeover_requested.add(owner)
            with _try_repository_lifecycle_lock(project_directory) as acquired:
                if acquired:
                    _run_with_repository_lease(
                        root,
                        project_directory,
                        prd_value=None,
                        no_open=True,
                        status=False,
                        launch_mode="automatic",
                        offline=True,
                        prevalidated_restore=restore,
                    )
                    return 0
            if time.monotonic() >= deadline:
                return 1
            time.sleep(0.01)
    except BaseException as error:  # noqa: BLE001 - fixed secret-free child boundary
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        return 1
    finally:
        current = None
        restore = SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
        if project_directory is not None:
            project_directory.close()
        project_directory = None
        root = Path()


def _developer_readiness(root: Path, preset: EnsurePreset) -> EnsureResult:
    """Own the bounded zero-command restore, readiness and service lifecycle."""
    restore = _shared_restore(root)
    configured = restore.status is not SharedStateRestoreStatus.NOT_REQUIRED
    if not os.path.lexists(root / ".intent"):
        if not configured:
            return _readiness_result(EnsureStatus.ONBOARDING_REQUIRED, ReadinessTarget.ONBOARDING)
        status = {
            SharedStateRestoreStatus.UNAVAILABLE: EnsureStatus.SHARED_STATE_UNAVAILABLE,
            SharedStateRestoreStatus.UPGRADE_REQUIRED: EnsureStatus.UPGRADE_REQUIRED,
        }.get(restore.status, EnsureStatus.SHARED_STATE_INVALID)
        return _readiness_result(status, ReadinessTarget.TEAM_STATE)
    runtime = None
    try:
        runtime = load_readiness_runtime(root)
        local = ReadinessService(runtime).ensure(EnsureRequest(preset=preset))
    except Exception:  # noqa: BLE001 - fixed readiness result
        return _readiness_result(EnsureStatus.SHARED_STATE_INVALID, ReadinessTarget.TEAM_STATE)
    finally:
        if runtime is not None:
            runtime.close()
    if not _start_or_reuse_background_service(root, restore.status):
        return _readiness_result(EnsureStatus.SHARED_STATE_INVALID, ReadinessTarget.TEAM_STATE)
    if restore.status is SharedStateRestoreStatus.INVALID:
        return _readiness_result(
            EnsureStatus.SHARED_STATE_INVALID,
            ReadinessTarget.TEAM_STATE,
            graph_version=local.graph_version,
        )
    if restore.status is SharedStateRestoreStatus.UPGRADE_REQUIRED:
        return _readiness_result(
            EnsureStatus.UPGRADE_REQUIRED,
            ReadinessTarget.TEAM_STATE,
            graph_version=local.graph_version,
        )
    if restore.status is SharedStateRestoreStatus.DIVERGED:
        return _readiness_result(
            EnsureStatus.HUMAN_ATTENTION_REQUIRED,
            ReadinessTarget.TEAM_STATE,
            graph_version=local.graph_version,
            pending_proposal_ids=local.pending_proposal_ids,
            open_case_ids=local.open_case_ids,
        )
    if restore.status in {SharedStateRestoreStatus.STALE, SharedStateRestoreStatus.UNAVAILABLE}:
        return _readiness_result(
            EnsureStatus.OFFLINE_STALE,
            ReadinessTarget.TEAM_STATE,
            graph_version=local.graph_version,
            pending_proposal_ids=local.pending_proposal_ids,
            open_case_ids=local.open_case_ids,
        )
    return local


def dev_command(
    project: Path = typer.Option(Path("."), "--project"),
    prd: Path | None = typer.Option(None, "--prd"),
    no_open: bool = typer.Option(False, "--no-open"),
    offline: bool = typer.Option(False, "--offline"),
    status: bool = typer.Option(False, "--status"),
) -> None:
    """Launch or inspect one trusted repository-bound local review process."""
    root = Path(os.path.abspath(project))
    prd_value = prd.as_posix() if prd is not None else None
    offline_value = offline if type(offline) is bool else False
    launch_mode: _LaunchMode = "manual_headless" if no_open else "interactive"
    project_directory: SecureDirectory | None = None
    signal_error: BaseException | None = None
    takeover_requested: set[tuple[int, str, str]] = set()
    desired_restore: SharedStateRestoreResult | None = None
    try:
        if status and not (root / ".intent").exists():
            _fixed_error("not running")
        if not status:
            desired_restore = _shared_restore(root, refresh_remote=not offline_value)
        project_directory = SecureDirectory.open(root)
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while True:
            current = _live_repository_process(project_directory)
            if current is not None:
                if status:
                    typer.echo(f"intent dev: running at {current.origin}")
                    return
                if (
                    desired_restore is not None
                    and current.shared_state_status is not desired_restore.status
                ):
                    if current.launch_mode != "automatic":
                        raise _DevUnavailable()
                elif no_open or current.launch_mode != "automatic":
                    typer.echo(f"intent dev: already running at {current.origin}")
                    return
                owner = (current.pid, current.process_start_id, current.instance_id)
                if owner not in takeover_requested:
                    if not _request_interactive_takeover(current, current.repository_id):
                        raise _DevUnavailable()
                    takeover_requested.add(owner)
            with _try_repository_lifecycle_lock(project_directory) as acquired:
                if acquired:
                    _run_with_repository_lease(
                        root,
                        project_directory,
                        prd_value=prd_value,
                        no_open=no_open,
                        status=status,
                        launch_mode=launch_mode,
                        offline=offline_value,
                        prevalidated_restore=desired_restore,
                    )
                    return
            if time.monotonic() >= deadline:
                raise _DevUnavailable()
            time.sleep(0.01)
    except typer.Exit:
        raise
    except (
        OSError,
        ProjectAlreadyInitialized,
        ProjectNotInitialized,
        UnsafePathError,
        _DevUnavailable,
    ):
        _fixed_error()
    except Exception:  # noqa: BLE001 - fixed secret-free public lifecycle failure
        _fixed_error()
    except BaseException as error:  # noqa: BLE001 - exact cancellation after cleanup
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        signal_error = error
    finally:
        if "current" in locals():
            current = None
        prd_value = None
        if project_directory is not None:
            project_directory.close()
        project_directory = None
        root = Path()
        desired_restore = None
        takeover_requested.clear()
    if signal_error is not None:
        caught = signal_error
        signal_error = None
        raise caught.with_traceback(None)


__all__ = ["ControlPlaneProcessMetadata", "dev_command", "ensure_command"]
