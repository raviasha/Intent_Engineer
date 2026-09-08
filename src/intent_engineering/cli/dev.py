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
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Iterator
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
from intent_engineering.team_state.restore import (
    TRUST_ENVIRONMENT_VARIABLE,
    EnvironmentTrustProvider,
    GitSharedStateRestorer,
)

_METADATA_PATH = "cache/control-plane.json"
_MAX_METADATA_BYTES = 4096
_STARTUP_TIMEOUT_SECONDS = 10.0
_PROBE_TIMEOUT_SECONDS = 1.0
_MAX_PROCESS_INSPECTION_BYTES = 1_048_576
_INSTANCE_PATH = "/_intent/dev/instance"
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
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module('intent_engineering.cli.app',run_name='__main__')"
)


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
    instance = _probe_json(metadata, _INSTANCE_PATH, 512)
    if instance != {"schema_version": 1, "instance_id": metadata.instance_id}:
        return False
    status = _probe_json(metadata, "/api/v1/status", 65_536)
    return bool(
        type(status) is dict
        and status.get("schema_version") == 1
        and status.get("project_id") == metadata.project_id
        and status.get("repository_id") == expected_repository_id
    )


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
    ) -> None:
        self._api = cast(Any, api)
        self._origin = origin
        self._host = origin.removeprefix("http://").encode("ascii")
        self._csrf = csrf_secret
        self._instance = json.dumps(
            {"schema_version": 1, "instance_id": instance_id},
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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if type(scope) is dict and scope.get("type") == "lifespan":
            await self._api(scope, receive, send)
            return
        path = scope.get("path") if type(scope) is dict else None
        if type(path) is str and path.startswith("/api/"):
            await self._api(scope, receive, send)
            return
        status = 404
        body = b'{"schema_version":1,"status":"rejected","reason":"request_unavailable"}'
        media_type = b"application/json"
        trusted = False
        set_cookie = False
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
            trusted = True
            asset = (
                (self._instance, b"application/json")
                if path == _INSTANCE_PATH
                else self._assets.get(path)
            )
            if asset is not None:
                body, media_type = asset
                status = 200
                set_cookie = path != _INSTANCE_PATH
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
        if trusted and set_cookie:
            response_headers.append(
                (
                    b"set-cookie",
                    f"intent_csrf={self._csrf}; Path=/; HttpOnly; SameSite=Strict".encode("ascii"),
                )
            )
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": body})

    def close(self) -> None:
        self._api = None
        self._origin = ""
        self._host = b""
        self._csrf = ""
        self._instance = b""
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
    started.service = ControlPlaneService(runtime, origin=origin)
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


def _wait_for_exit(started: _Started) -> None:
    signal_error: BaseException | None = None
    thread = started.thread
    server = started.server
    if thread is None or server is None:
        raise _DevUnavailable()
    try:
        while thread.is_alive():
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


def _run_with_repository_lease(
    root: Path,
    project_directory: SecureDirectory,
    *,
    prd_value: str | None,
    no_open: bool,
    status: bool,
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
    try:
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
            _start(started, target, repository_id, prd_value)
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


def _shared_restore(root: Path) -> SharedStateRestoreResult:
    if TRUST_ENVIRONMENT_VARIABLE not in os.environ:
        return SharedStateRestoreResult(status=SharedStateRestoreStatus.NOT_REQUIRED)
    try:
        result = GitSharedStateRestorer(
            EnvironmentTrustProvider(),
        ).verify_and_restore_approved_baseline(root)
        if type(result) is SharedStateRestoreResult:
            return result
    except Exception as error:  # noqa: BLE001 - fixed secret-free restore boundary
        error.__traceback__ = None
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
        "dev",
        "--project",
        str(root),
        "--no-open",
    )


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


def _start_or_reuse_background_service(root: Path) -> bool:
    if _running_service(root) is not None:
        return True
    process: subprocess.Popen[bytes] | None = None
    ready = False
    try:
        process = subprocess.Popen(
            _background_argv(root),
            cwd="/",
            env=_background_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _running_service(root) is not None:
                ready = True
                return True
            if process.poll() is not None:
                return _running_service(root) is not None
            time.sleep(0.01)
        return False
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
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
    if not _start_or_reuse_background_service(root):
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
    del offline  # The process is local-only in both modes; the flag makes that intent explicit.
    root = Path(os.path.abspath(project))
    prd_value = prd.as_posix() if prd is not None else None
    project_directory: SecureDirectory | None = None
    signal_error: BaseException | None = None
    try:
        if status and not (root / ".intent").exists():
            _fixed_error("not running")
        project_directory = SecureDirectory.open(root)
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while True:
            current = _live_repository_process(project_directory)
            if current is not None:
                if status:
                    typer.echo(f"intent dev: running at {current.origin}")
                else:
                    typer.echo(f"intent dev: already running at {current.origin}")
                return
            with _try_repository_lifecycle_lock(project_directory) as acquired:
                if acquired:
                    _run_with_repository_lease(
                        root,
                        project_directory,
                        prd_value=prd_value,
                        no_open=no_open,
                        status=status,
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
    if signal_error is not None:
        caught = signal_error
        signal_error = None
        raise caught.with_traceback(None)


__all__ = ["ControlPlaneProcessMetadata", "dev_command", "ensure_command"]
