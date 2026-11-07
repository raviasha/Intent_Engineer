"""End-to-end lifecycle coverage for the local ``intent dev`` control plane."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from typer.testing import CliRunner

from intent_engineering.cli import dev as dev_cli
from intent_engineering.cli.app import app
from intent_engineering.cli.dev import ControlPlaneProcessMetadata
from intent_engineering.cli.runtime import Runtime
from intent_engineering.control_plane import local_repository_identity
from intent_engineering.control_plane.service import ControlPlaneError, ControlPlaneService
from intent_engineering.control_plane.webauthn_service import (
    AuthenticationRequest,
    RegistrationRequest,
    VerifiedAuthentication,
    VerifiedRegistration,
    WebAuthnVerifier,
)
from intent_engineering.core.models import (
    ChangeSet,
    EvidenceSide,
    Node,
    NodeType,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    ResolutionAction,
    SourceMode,
)
from intent_engineering.core.policy import initialize_project
from intent_engineering.intent_workflow.bootstrap import BootstrapService, BootstrapSubmission
from intent_engineering.intent_workflow.clarification import ClarificationCoordinator
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.models import (
    ClarificationProposalSubmission,
    ClarificationQuestionInput,
    TaskEnvelope,
)
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.secure import SecureDirectory, SecureFile, UnsafePathError
from tests.integration.control_plane.test_service import (
    _Harness,
    _preview_digest,
    _registration_response,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@dataclass
class _ReleaseVerifier(WebAuthnVerifier):
    registration_requests: list[RegistrationRequest] = field(default_factory=list)
    authentication_requests: list[AuthenticationRequest] = field(default_factory=list)

    def registration_options(self, request: RegistrationRequest) -> bytes:
        self.registration_requests.append(request)
        return b'{"publicKey":{"userVerification":"required"}}'

    def verify_registration(
        self, response: bytes, request: RegistrationRequest
    ) -> VerifiedRegistration:
        if response != _registration_response(request):
            raise ValueError("registration unavailable")
        return VerifiedRegistration(
            credential_id=b"release-credential",
            public_key=b"release-public-key",
            sign_count=0,
            user_verified=True,
        )

    def authentication_options(self, request: AuthenticationRequest) -> bytes:
        self.authentication_requests.append(request)
        return b'{"publicKey":{"userVerification":"required"}}'

    def verify_authentication(
        self, response: bytes, request: AuthenticationRequest
    ) -> VerifiedAuthentication:
        if response != b"release-signed-assertion" or request != self.authentication_requests[-1]:
            raise ValueError("signature unavailable")
        return VerifiedAuthentication(
            credential_id=b"release-credential",
            new_sign_count=0,
            user_verified=True,
        )


def _project(tmp_path: Path, name: str = "project") -> Path:
    project = tmp_path / name
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PRD.md").write_text(
        "# Local export\n\nPeople need a reviewed local CSV export.\n",
        encoding="utf-8",
    )
    return project


def _command(project: Path, *arguments: str) -> list[str]:
    return [
        str(Path(sys.executable).with_name("intent")),
        "dev",
        "--project",
        str(project),
        *arguments,
    ]


def _environment() -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
    }


def _wait_for_metadata(
    project: Path,
    process: subprocess.Popen[str],
    *,
    expected_pid: int | None = None,
) -> dict[str, object]:
    metadata = project / ".intent/cache/control-plane.json"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"intent dev exited before ready: {stdout!r} {stderr!r}")
        try:
            result = json.loads(metadata.read_text(encoding="utf-8"))
            if expected_pid is None or result.get("pid") == expected_pid:
                return result
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    pytest.fail("intent dev did not publish ready metadata")


def _stop(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    return process.communicate(timeout=10)


def _repository_binding(project: Path) -> tuple[str, str]:
    project_directory = SecureDirectory.open(project)
    workspace_directory = project_directory.subdirectory(".intent")
    try:
        config, _content = dev_cli._read_project_config(workspace_directory)
        return config.project_id, local_repository_identity(
            config.project_id, project_directory.identity
        )
    finally:
        workspace_directory.close()
        project_directory.close()


def _fake_process_server(
    project_id: str,
    repository_id: str,
    *,
    instance_id: str | None,
) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/api/v1/status":
                payload = {
                    "schema_version": 1,
                    "project_id": project_id,
                    "repository_id": repository_id,
                }
                status = 200
            elif self.path == "/_intent/dev/instance" and instance_id is not None:
                payload = {
                    "schema_version": 1,
                    "instance_id": instance_id,
                    "project_id": project_id,
                    "repository_id": repository_id,
                }
                status = 200
            else:
                payload = {"schema_version": 1, "status": "unavailable"}
                status = 404
            encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, thread, f"http://localhost:{port}"


def _assert_runtime_resources_closed(runtime: Runtime) -> None:
    for directory in (runtime.project_directory, runtime.workspace_directory):
        with pytest.raises(UnsafePathError):
            _ = directory.descriptor
    files = (
        runtime.graph_store._file,
        runtime.graph_store._history_store._file,
        runtime.evidence_store._file,
        runtime.case_store._file,
        runtime.intent_proposals._file,
        runtime.webauthn_credentials._file,
        runtime.webauthn_challenges._file,
        runtime.checkpoint_store._file,
        runtime.transactions._journal,
        *runtime.transactions._targets.values(),
        *runtime.sync._snapshot_files.values(),
    )
    for file in files:
        with pytest.raises(UnsafePathError):
            _ = file.parent_fd


def test_process_metadata_is_strict_and_secret_free() -> None:
    metadata = ControlPlaneProcessMetadata.model_validate(
        {
            "schema_version": 1,
            "pid": 123,
            "process_start_id": "sha256:" + "a" * 64,
            "instance_id": "instance:" + "b" * 64,
            "project_id": "project",
            "repository_id": "repo:sha256:" + "c" * 64,
            "origin": "http://localhost:43127",
            "launch_mode": "automatic",
            "shared_state_status": "verified",
        }
    )

    assert metadata.origin == "http://localhost:43127"
    assert metadata.launch_mode == "automatic"
    assert metadata.shared_state_status.value == "verified"
    with pytest.raises(ValueError):
        ControlPlaneProcessMetadata.model_validate(
            {**metadata.model_dump(), "csrf_secret": "must-not-be-persisted"}
        )


def test_legacy_process_metadata_is_reusable_but_not_replaceable() -> None:
    """Catches an old locator being silently promoted to replaceable authority."""
    metadata = ControlPlaneProcessMetadata.model_validate(
        {
            "schema_version": 1,
            "pid": 123,
            "process_start_id": "sha256:" + "a" * 64,
            "instance_id": "instance:" + "b" * 64,
            "project_id": "project",
            "repository_id": "repo:sha256:" + "c" * 64,
            "origin": "http://localhost:43127",
        }
    )

    assert metadata.launch_mode == "legacy"
    assert dev_cli._request_interactive_takeover(metadata, metadata.repository_id) is False


def test_instance_rebound_shutdown_refuses_takeover_without_pid_signaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a stale probe stopping a rebound endpoint or an unrelated reused PID."""
    requests: list[tuple[str, str, bytes, dict[str, str]]] = []

    class Response:
        status = 409

        @staticmethod
        def read(_maximum: int) -> bytes:
            return b'{"schema_version":1,"status":"rejected"}'

    class Connection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            requests.append((method, path, body, headers))

        @staticmethod
        def getresponse() -> Response:
            return Response()

        @staticmethod
        def close() -> None:
            pass

    metadata = ControlPlaneProcessMetadata(
        pid=123,
        process_start_id="sha256:" + "a" * 64,
        instance_id="instance:" + "b" * 64,
        project_id="project",
        repository_id="repo:sha256:" + "c" * 64,
        origin="http://localhost:43127",
        launch_mode="automatic",
    )
    monkeypatch.setattr(dev_cli, "_probe", lambda *_args: True)
    monkeypatch.setattr(dev_cli.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(
        dev_cli.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("PID signaling is forbidden")),
    )
    assert dev_cli._request_interactive_takeover(metadata, metadata.repository_id) is False
    assert len(requests) == 1
    method, path, body, headers = requests[0]
    assert (method, path) == ("POST", dev_cli._SHUTDOWN_PATH)
    assert json.loads(body) == {
        "schema_version": 1,
        "instance_id": metadata.instance_id,
        "project_id": metadata.project_id,
        "repository_id": metadata.repository_id,
        "launch_mode": "automatic",
    }
    assert headers == {
        "Host": "localhost:43127",
        "Origin": metadata.origin,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Accept": "application/json",
    }


def test_uninitialized_repository_requires_an_explicit_prd_without_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result = subprocess.run(
        _command(project, "--no-open", "--offline"),
        env=_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent dev: unavailable\n"
    assert not (project / ".intent").exists()


def test_fresh_repository_starts_reuses_serves_assets_and_cleans_up(tmp_path: Path) -> None:
    project = _project(tmp_path)
    process = subprocess.Popen(
        _command(project, "--prd", "docs/PRD.md", "--no-open", "--offline"),
        env=_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        raw = _wait_for_metadata(project, process)
        metadata = ControlPlaneProcessMetadata.model_validate(raw)

        status = subprocess.run(
            _command(project, "--status", "--no-open", "--offline"),
            env=_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        reuse = subprocess.run(
            _command(project, "--no-open", "--offline"),
            env=_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        request = Request(
            metadata.origin, headers={"Host": metadata.origin.removeprefix("http://")}
        )
        with urlopen(request, timeout=3) as response:
            page = response.read().decode("utf-8")
            cookie = response.headers.get("Set-Cookie", "")

        assert status.returncode == 0
        assert status.stdout == f"intent dev: running at {metadata.origin}\n"
        assert status.stderr == ""
        assert reuse.returncode == 0
        assert reuse.stdout == f"intent dev: already running at {metadata.origin}\n"
        assert reuse.stderr == ""
        assert "<title>Intent Engineering review</title>" in page
        assert cookie == ""
        assert metadata.pid == process.pid
        assert (project / ".intent/evidence/evidence.jsonl").read_bytes()
        persisted = b"".join(
            path.read_bytes()
            for path in (project / ".intent").rglob("*")
            if path.is_file() and not path.name.startswith(".control-plane.json.lock")
        )
        assert b"csrf" not in persisted.lower()
    finally:
        stdout, stderr = _stop(process)

    assert stdout == f"intent dev: ready at {metadata.origin}\n"
    assert stderr == ""
    assert not (project / ".intent/cache/control-plane.json").exists()


def test_ordinary_dev_replaces_automatic_owner_and_opens_with_ephemeral_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    initialize_project(project)
    assert dev_cli._start_or_reuse_background_service(
        project,
        dev_cli.SharedStateRestoreStatus.NOT_REQUIRED,
    )
    automatic_pid = 0
    opened: list[str] = []
    monkeypatch.setattr(
        dev_cli.webbrowser,
        "open",
        lambda url, **_kwargs: opened.append(url) or True,
    )
    authenticated_results: list[tuple[int, str]] = []

    def exercise_authenticated_browser(_started: object) -> None:
        opened_url = opened[0]
        origin, _separator, bootstrap = opened_url.partition("/#csrf=")
        host = origin.removeprefix("http://")
        with urlopen(Request(origin, headers={"Host": host}), timeout=3) as response:
            assert response.headers.get("Set-Cookie") is None
        exchange_body = json.dumps(
            {"token": bootstrap}, separators=(",", ":"), sort_keys=True
        ).encode()
        untrusted_body = json.dumps(
            {"token": "x" * len(bootstrap)}, separators=(",", ":"), sort_keys=True
        ).encode()
        untrusted_exchange = Request(
            origin + dev_cli._BROWSER_BOOTSTRAP_PATH,
            data=untrusted_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(untrusted_body)),
                "Host": host,
                "Origin": origin,
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as untrusted:
            urlopen(untrusted_exchange, timeout=3)
        assert untrusted.value.code == 403
        assert untrusted.value.headers.get("Set-Cookie") is None
        exchange = Request(
            origin + dev_cli._BROWSER_BOOTSTRAP_PATH,
            data=exchange_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(exchange_body)),
                "Host": host,
                "Origin": origin,
            },
            method="POST",
        )
        with urlopen(exchange, timeout=3) as response:
            cookie = response.headers["Set-Cookie"].partition(";")[0]
            assert response.status == 204
        with pytest.raises(HTTPError) as replay:
            urlopen(exchange, timeout=3)
        assert replay.value.code == 403
        request = Request(
            origin + "/api/v1/development/tests/run",
            data=json.dumps({"command_id": "test:sha256:" + "f" * 64}).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Host": host,
                "Origin": origin,
                "X-Intent-CSRF": bootstrap,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                authenticated_results.append(
                    (response.status, json.loads(response.read())["status"])
                )
        except HTTPError as error:
            authenticated_results.append((error.code, "http_error"))

    monkeypatch.setattr(dev_cli, "_wait_for_exit", exercise_authenticated_browser)
    try:
        automatic = ControlPlaneProcessMetadata.model_validate(
            json.loads((project / ".intent/cache/control-plane.json").read_bytes())
        )
        automatic_pid = automatic.pid

        result = CliRunner().invoke(app, ["dev", "--project", str(project)])

        assert result.exit_code == 0, repr(result.exception)
        assert result.stdout.startswith("intent dev: ready at http://localhost:")
        assert result.stderr == ""
        assert len(opened) == 1
        assert opened[0].startswith("http://localhost:")
        assert "/#csrf=" in opened[0]
        assert automatic.origin not in opened[0]
        # This project has no reviewed command, so behavior is unavailable only
        # after the authenticated write boundary accepts the ephemeral token.
        assert authenticated_results == [(503, "http_error")]
        deadline = time.monotonic() + 3
        exited = False
        while time.monotonic() < deadline:
            try:
                waited, _status = os.waitpid(automatic.pid, os.WNOHANG)
            except ChildProcessError:
                waited = automatic.pid
            if waited == automatic.pid:
                exited = True
                break
            time.sleep(0.02)
        assert exited is True
        persisted = b"".join(
            path.read_bytes()
            for path in (project / ".intent").rglob("*")
            if path.is_file() and not path.name.startswith(".control-plane.json.lock")
        )
        assert opened[0].partition("#csrf=")[2].encode() not in persisted
    finally:
        if automatic_pid:
            try:
                os.kill(automatic_pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(automatic_pid, os.WNOHANG)
            except ChildProcessError:
                pass


def test_ordinary_dev_reuses_a_manually_headless_owner_without_signaling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches takeover broadening from attested automatic owners to manual services."""
    project = _project(tmp_path)
    owner = subprocess.Popen(
        _command(project, "--prd", "docs/PRD.md", "--no-open", "--offline"),
        env=_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    opened: list[str] = []
    monkeypatch.setattr(
        dev_cli.webbrowser, "open", lambda url, **_kwargs: opened.append(url) or True
    )
    try:
        metadata = ControlPlaneProcessMetadata.model_validate(_wait_for_metadata(project, owner))

        result = CliRunner().invoke(app, ["dev", "--project", str(project)])

        assert result.exit_code == 0, repr(result.exception)
        assert result.stdout == f"intent dev: already running at {metadata.origin}\n"
        assert result.stderr == ""
        assert metadata.launch_mode == "manual_headless"
        assert owner.poll() is None
        assert opened == []
    finally:
        _stop(owner)


def test_direct_dev_refuses_to_reuse_a_service_with_contradictory_shared_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches direct dev bypassing restore by returning an old local-only owner."""
    project = _project(tmp_path)
    initialize_project(project)
    _project_id, repository_id = _repository_binding(project)
    metadata = ControlPlaneProcessMetadata(
        pid=os.getpid(),
        process_start_id="sha256:" + "a" * 64,
        instance_id="instance:" + "b" * 64,
        project_id=project.name,
        repository_id=repository_id,
        origin="http://localhost:43127",
        launch_mode="manual_headless",
        shared_state_status=dev_cli.SharedStateRestoreStatus.NOT_REQUIRED,
    )
    monkeypatch.setattr(
        dev_cli,
        "_shared_restore",
        lambda _root, **_kwargs: dev_cli.SharedStateRestoreResult(
            status=dev_cli.SharedStateRestoreStatus.INVALID
        ),
    )
    monkeypatch.setattr(dev_cli, "_live_repository_process", lambda _directory: metadata)

    result = CliRunner().invoke(
        app,
        ["dev", "--project", str(project), "--no-open"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "intent dev: unavailable\n"


def test_stale_and_foreign_pid_metadata_is_not_reused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    initialize_project(project)
    metadata_path = project / ".intent/cache/control-plane.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pid": os.getpid(),
                "process_start_id": "sha256:" + "0" * 64,
                "instance_id": "instance:" + "1" * 64,
                "project_id": project.name,
                "repository_id": "repo:sha256:" + "2" * 64,
                "origin": "http://localhost:9",
            }
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        _command(project, "--prd", "docs/PRD.md", "--no-open"),
        env=_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        replacement = ControlPlaneProcessMetadata.model_validate(
            _wait_for_metadata(project, process, expected_pid=process.pid)
        )
        assert replacement.pid == process.pid
        assert replacement.origin != "http://localhost:9"
    finally:
        _stop(process)


def test_repository_shaped_server_is_not_reused_for_a_different_live_process(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    initialize_project(project)
    project_id, repository_id = _repository_binding(project)
    instance_id = "instance:" + "7" * 64
    server, thread, origin = _fake_process_server(
        project_id,
        repository_id,
        instance_id=instance_id,
    )
    foreign = subprocess.Popen(["/bin/sleep", "30"])
    try:
        process_start_id = dev_cli._process_start_id(foreign.pid)
        assert process_start_id is not None
        metadata = ControlPlaneProcessMetadata(
            pid=foreign.pid,
            process_start_id=process_start_id,
            instance_id=instance_id,
            project_id=project_id,
            repository_id=repository_id,
            origin=origin,
        )
        (project / ".intent/cache/control-plane.json").write_bytes(metadata.canonical_bytes())

        result = CliRunner().invoke(
            app,
            ["dev", "--project", str(project), "--status", "--no-open"],
        )

        assert result.exit_code == 1
        assert result.stdout == ""
        assert result.stderr == "intent dev: not running\n"
    finally:
        foreign.terminate()
        foreign.wait(timeout=3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_repository_shaped_foreign_server_without_instance_attestation_is_not_reused(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    initialize_project(project)
    project_id, repository_id = _repository_binding(project)
    server, thread, origin = _fake_process_server(
        project_id,
        repository_id,
        instance_id=None,
    )
    try:
        process_start_id = dev_cli._process_start_id(os.getpid())
        assert process_start_id is not None
        metadata = ControlPlaneProcessMetadata(
            pid=os.getpid(),
            process_start_id=process_start_id,
            instance_id="instance:" + "8" * 64,
            project_id=project_id,
            repository_id=repository_id,
            origin=origin,
        )
        (project / ".intent/cache/control-plane.json").write_bytes(metadata.canonical_bytes())

        result = CliRunner().invoke(
            app,
            ["dev", "--project", str(project), "--status", "--no-open"],
        )

        assert result.exit_code == 1
        assert result.stdout == ""
        assert result.stderr == "intent dev: not running\n"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_concurrent_first_starts_publish_only_one_process(tmp_path: Path) -> None:
    project = _project(tmp_path)
    command = _command(project, "--prd", "docs/PRD.md", "--no-open", "--offline")
    first = subprocess.Popen(
        command,
        env=_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second = subprocess.Popen(
        command,
        env=_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    owner: subprocess.Popen[str] | None = None
    peer: subprocess.Popen[str] | None = None
    try:
        metadata_path = project / ".intent/cache/control-plane.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if metadata_path.is_file():
                raw = json.loads(metadata_path.read_text(encoding="utf-8"))
                if raw.get("pid") in {first.pid, second.pid}:
                    owner = first if raw["pid"] == first.pid else second
                    peer = second if owner is first else first
                    break
            time.sleep(0.02)
        assert owner is not None and peer is not None
        peer_stdout, peer_stderr = peer.communicate(timeout=10)
        assert peer.returncode == 0
        assert peer_stdout.startswith("intent dev: already running at http://localhost:")
        assert peer_stderr == ""
        assert owner.poll() is None
    finally:
        for process in (first, second):
            if process.poll() is None:
                _stop(process)


def test_copied_live_metadata_is_not_reused_across_repositories(tmp_path: Path) -> None:
    first_project = _project(tmp_path, "first")
    second_project = _project(tmp_path, "second")
    process = subprocess.Popen(
        _command(first_project, "--prd", "docs/PRD.md", "--no-open"),
        env=_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_metadata(first_project, process)
        initialize_project(second_project)
        copied = (first_project / ".intent/cache/control-plane.json").read_bytes()
        (second_project / ".intent/cache/control-plane.json").write_bytes(copied)

        status = subprocess.run(
            _command(second_project, "--status", "--no-open"),
            env=_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )

        assert status.returncode == 1
        assert status.stdout == ""
        assert status.stderr == "intent dev: not running\n"
        assert process.poll() is None
        assert not (second_project / ".intent/cache/control-plane.json").exists()
    finally:
        _stop(process)


@pytest.mark.parametrize("replacement", [False, True])
def test_cancellation_immediately_after_publication_exactly_cleans_owned_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bool,
) -> None:
    project = _project(tmp_path)
    metadata_path = project / ".intent/cache/control-plane.json"
    replacement_bytes = b'{"replacement":"PRIVATE-REPLACEMENT"}\n'
    cancellation = BaseException("PRIVATE-PUBLISH-CANCELLED")
    bootstrap = "PRIVATE_PUBLISH_BOOTSTRAP"
    original_atomic_write = SecureFile.atomic_write

    def interrupt_after_publication(
        target: SecureFile,
        content: bytes,
        *,
        reject_target_races: bool = False,
    ) -> None:
        original_atomic_write(
            target,
            content,
            reject_target_races=reject_target_races,
        )
        if target.path == metadata_path:
            if replacement:
                metadata_path.write_bytes(replacement_bytes)
            raise cancellation

    monkeypatch.setattr(SecureFile, "atomic_write", interrupt_after_publication)
    monkeypatch.setattr(dev_cli.secrets, "token_urlsafe", lambda _size: bootstrap)

    with pytest.raises(BaseException) as raised:
        CliRunner().invoke(
            app,
            [
                "dev",
                "--project",
                str(project),
                "--prd",
                "docs/PRD.md",
                "--no-open",
            ],
        )

    traceback_values: list[str] = []
    traceback = cancellation.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            traceback_values.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert raised.value is cancellation
    if replacement:
        assert metadata_path.read_bytes() == replacement_bytes
    else:
        assert not metadata_path.exists()
    retained = "\n".join(traceback_values)
    assert bootstrap not in retained
    assert "Runtime(" not in retained


def test_runtime_resources_close_on_normal_dev_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    original_load_runtime = dev_cli.load_runtime
    runtimes: list[Runtime] = []

    def tracked_load_runtime(path: Path) -> Runtime:
        runtime = original_load_runtime(path)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(dev_cli, "load_runtime", tracked_load_runtime)
    monkeypatch.setattr(dev_cli, "_wait_for_exit", lambda _started: None)

    result = CliRunner().invoke(
        app,
        [
            "dev",
            "--project",
            str(project),
            "--prd",
            "docs/PRD.md",
            "--no-open",
        ],
    )

    assert result.exit_code == 0, repr(result.exception)
    assert len(runtimes) == 1
    _assert_runtime_resources_closed(runtimes[0])


def test_shutdown_holds_startup_lock_until_runtime_quiesces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    original_load_runtime = dev_cli.load_runtime
    original_service_close = ControlPlaneService.close
    runtimes: list[Runtime] = []
    launch_errors: list[BaseException] = []
    state_lock = threading.Lock()
    stop_owner = threading.Event()
    service_close_entered = threading.Event()
    allow_service_close = threading.Event()
    second_loaded = threading.Event()
    wait_calls = 0
    first_closed_when_second_loaded = False

    def tracked_load_runtime(path: Path) -> Runtime:
        nonlocal first_closed_when_second_loaded
        runtime = original_load_runtime(path)
        with state_lock:
            if runtimes:
                try:
                    _ = runtimes[0].project_directory.descriptor
                except UnsafePathError:
                    first_closed_when_second_loaded = True
                second_loaded.set()
            runtimes.append(runtime)
        return runtime

    def controlled_wait(_started: object) -> None:
        nonlocal wait_calls
        with state_lock:
            wait_calls += 1
            call = wait_calls
        if call == 1:
            assert stop_owner.wait(timeout=5)

    def held_service_close(service: ControlPlaneService) -> None:
        if not service_close_entered.is_set():
            service_close_entered.set()
            assert allow_service_close.wait(timeout=5)
        original_service_close(service)

    def launch(prd: Path | None) -> None:
        try:
            dev_cli.dev_command(
                project=project,
                prd=prd,
                no_open=True,
                offline=True,
                status=False,
            )
        except BaseException as error:  # noqa: BLE001 - transferred to the test thread
            launch_errors.append(error)

    monkeypatch.setattr(dev_cli, "load_runtime", tracked_load_runtime)
    monkeypatch.setattr(dev_cli, "_wait_for_exit", controlled_wait)
    monkeypatch.setattr(ControlPlaneService, "close", held_service_close)
    owner = threading.Thread(target=launch, args=(Path("docs/PRD.md"),), daemon=True)
    contender = threading.Thread(target=launch, args=(None,), daemon=True)
    owner.start()
    try:
        metadata_path = project / ".intent/cache/control-plane.json"
        deadline = time.monotonic() + 10
        while not metadata_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert metadata_path.is_file()
        stop_owner.set()
        assert service_close_entered.wait(timeout=5)
        metadata_held_until_service_close = metadata_path.is_file()
        contender.start()
        loaded_during_quiescence = second_loaded.wait(timeout=0.5)
    finally:
        allow_service_close.set()
        owner.join(timeout=10)
        contender.join(timeout=10)

    assert not owner.is_alive()
    assert not contender.is_alive()
    assert launch_errors == []
    assert metadata_held_until_service_close
    assert not loaded_during_quiescence
    assert first_closed_when_second_loaded
    assert len(runtimes) == 2
    for runtime in runtimes:
        _assert_runtime_resources_closed(runtime)


def test_server_quiescence_cannot_open_pre_cleanup_start_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    original_load_runtime = dev_cli.load_runtime
    runtimes: list[Runtime] = []
    launch_errors: list[BaseException] = []
    state_lock = threading.Lock()
    server_quiesced = threading.Event()
    allow_owner_wait_return = threading.Event()
    second_loaded = threading.Event()
    wait_calls = 0
    first_closed_when_second_loaded = False

    def tracked_load_runtime(path: Path) -> Runtime:
        nonlocal first_closed_when_second_loaded
        runtime = original_load_runtime(path)
        with state_lock:
            if runtimes:
                try:
                    _ = runtimes[0].project_directory.descriptor
                except UnsafePathError:
                    first_closed_when_second_loaded = True
                second_loaded.set()
            runtimes.append(runtime)
        return runtime

    def quiesce_before_owner_cleanup(started: object) -> None:
        nonlocal wait_calls
        with state_lock:
            wait_calls += 1
            call = wait_calls
        if call != 1:
            return
        assert isinstance(started, dev_cli._Started)
        owned = started
        assert owned.server is not None
        assert owned.thread is not None
        owned.server.should_exit = True
        owned.thread.join(timeout=5)
        assert not owned.thread.is_alive()
        server_quiesced.set()
        assert allow_owner_wait_return.wait(timeout=5)

    def launch(prd: Path | None) -> None:
        try:
            dev_cli.dev_command(
                project=project,
                prd=prd,
                no_open=True,
                offline=True,
                status=False,
            )
        except BaseException as error:  # noqa: BLE001 - transferred to the test thread
            launch_errors.append(error)

    monkeypatch.setattr(dev_cli, "load_runtime", tracked_load_runtime)
    monkeypatch.setattr(dev_cli, "_wait_for_exit", quiesce_before_owner_cleanup)
    owner = threading.Thread(target=launch, args=(Path("docs/PRD.md"),), daemon=True)
    contender = threading.Thread(target=launch, args=(None,), daemon=True)
    owner.start()
    try:
        assert server_quiesced.wait(timeout=10)
        contender.start()
        loaded_before_owner_cleanup = second_loaded.wait(timeout=0.5)
    finally:
        allow_owner_wait_return.set()
        owner.join(timeout=10)
        if contender.ident is not None:
            contender.join(timeout=10)

    assert not owner.is_alive()
    assert not contender.is_alive()
    assert launch_errors == []
    assert not loaded_before_owner_cleanup
    assert first_closed_when_second_loaded
    assert len(runtimes) == 2
    for runtime in runtimes:
        _assert_runtime_resources_closed(runtime)


def test_browser_open_failure_is_fixed_and_server_cleanup_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    opened: list[str] = []

    def fail_open(url: str, **_kwargs: object) -> bool:
        opened.append(url)
        return False

    monkeypatch.setattr(dev_cli.webbrowser, "open", fail_open)
    monkeypatch.setattr(dev_cli, "_wait_for_exit", lambda _started: None)

    result = CliRunner().invoke(
        app,
        ["dev", "--project", str(project), "--prd", "docs/PRD.md", "--offline"],
    )

    assert result.exit_code == 0, repr(result.exception)
    assert result.stdout.startswith("intent dev: ready at http://localhost:")
    assert result.stderr == "intent dev: browser unavailable\n"
    assert len(opened) == 1 and "/#csrf=" in opened[0]
    assert not (project / ".intent/cache/control-plane.json").exists()


def test_real_dev_start_activates_passive_observation_before_reporting_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches passive observation existing only as a directly-invoked Python method."""
    project = _project(tmp_path)
    started: list[ControlPlaneService] = []

    def record_start(service: ControlPlaneService) -> None:
        started.append(service)

    monkeypatch.setattr(
        ControlPlaneService,
        "start_development_observation",
        record_start,
        raising=False,
    )
    monkeypatch.setattr(dev_cli, "_wait_for_exit", lambda _started: None)

    result = CliRunner().invoke(
        app,
        ["dev", "--project", str(project), "--prd", "docs/PRD.md", "--no-open"],
    )

    assert result.exit_code == 0, repr(result.exception)
    assert len(started) == 1
    assert result.stdout.startswith("intent dev: ready at http://localhost:")


def test_cancelled_wait_preserves_identity_cleans_metadata_and_scrubs_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    cancellation = BaseException("PRIVATE-CANCELLED-DEV")
    bootstrap = "PRIVATE_BOOTSTRAP_DEV_VALUE"
    original_load_runtime = dev_cli.load_runtime
    runtimes: list[Runtime] = []

    def tracked_load_runtime(path: Path) -> Runtime:
        runtime = original_load_runtime(path)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(dev_cli, "load_runtime", tracked_load_runtime)
    monkeypatch.setattr(dev_cli.secrets, "token_urlsafe", lambda _size: bootstrap)

    def cancel(_started: object) -> None:
        raise cancellation

    monkeypatch.setattr(dev_cli, "_wait_for_exit", cancel)

    with pytest.raises(BaseException) as raised:
        CliRunner().invoke(
            app,
            [
                "dev",
                "--project",
                str(project),
                "--prd",
                "docs/PRD.md",
                "--no-open",
            ],
        )

    traceback_values: list[str] = []
    traceback = cancellation.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            traceback_values.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert raised.value is cancellation
    assert not (project / ".intent/cache/control-plane.json").exists()
    retained = "\n".join(traceback_values)
    assert bootstrap not in retained
    assert "Runtime(" not in retained
    assert len(runtimes) == 1
    _assert_runtime_resources_closed(runtimes[0])


def test_fresh_repository_milestone_one_release_journey_uses_one_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove the complete authenticated M1 journey from a repository without ``.intent``."""
    project = _project(tmp_path)
    original_load_runtime = dev_cli.load_runtime
    load_calls: list[Path] = []
    captured: list[tuple[object, ControlPlaneService, _ReleaseVerifier]] = []
    journey_errors: list[BaseException] = []
    private_assertion = b"release-signed-assertion"

    def tracked_load_runtime(path: Path):
        load_calls.append(path)
        return original_load_runtime(path)

    def service_factory(
        runtime: object,
        *,
        origin: str,
        shared_state_status: dev_cli.SharedStateRestoreStatus,
    ) -> ControlPlaneService:
        verifier = _ReleaseVerifier()
        nonce = iter(range(1, 64))
        service = ControlPlaneService(
            runtime,  # type: ignore[arg-type]
            origin=origin,
            clock=lambda: NOW,
            challenge_source=lambda: bytes([next(nonce)]) * 32,
            webauthn_verifier=verifier,
            shared_state_status=shared_state_status,
        )
        captured.append((runtime, service, verifier))
        return service

    monkeypatch.setattr(dev_cli, "load_runtime", tracked_load_runtime)
    monkeypatch.setattr(dev_cli, "ControlPlaneService", service_factory)

    def journey(_started: object) -> None:
        try:
            assert len(captured) == 1
            runtime, service, verifier = captured[0]
            harness = _Harness(runtime, service, verifier, project)  # type: ignore[arg-type]
            actor = harness.runtime.config.local_actor

            # Fresh PRD capture is a preview only; fake WebAuthn enrollment establishes
            # the independent human verifier before any semantic activation.
            assert len(harness.runtime.evidence()) == 1
            assert harness.service.status()["status"] == "onboarding_required"
            harness.service.registration_options()
            registration_request = verifier.registration_requests[-1]
            registered = harness.service.register(_registration_response(registration_request))
            assert registered.actor == actor
            assert (
                harness.runtime.webauthn_credentials.list()[-1].credential_id
                == registered.credential_id
            )

            evidence_ref = harness.runtime.evidence()[0].id
            baseline_node = Node(
                id="intent-local-export",
                type=NodeType.PRODUCT_INTENT,
                label="Keep exports local",
                status="proposed",
                created_by="agent:codex",
                created_at=NOW,
                last_modified_by="agent:codex",
                last_modified_at=NOW,
                source_mode=SourceMode.INFERRED,
                intent_fidelity_confidence=0.82,
                confidence_basis="Inferred from the captured PRD",
                last_reassessed_at=NOW,
                evidence_refs=(evidence_ref,),
            )
            bootstrap_service = BootstrapService(
                graph_store=harness.runtime.graph_store,
                evidence_store=harness.runtime.evidence_store,
                proposal_store=harness.runtime.intent_proposals,
                changeset_executor=LocalChangeSetExecutor(
                    harness.runtime.graph_store,
                    harness.runtime.case_store,
                    harness.runtime.transactions,
                ),
                transactions=harness.runtime.transactions,
                config=harness.runtime.config,
            )
            bootstrap_review = bootstrap_service.propose(
                BootstrapSubmission(
                    baseline_graph_version=0,
                    actor="agent:codex",
                    timestamp=NOW,
                    evidence_refs=(evidence_ref,),
                    source_roles=harness.runtime.config.source_roles,
                    candidate_nodes=(baseline_node,),
                    candidate_edges=(),
                    core_node_ids=(baseline_node.id,),
                    provisional_node_ids=(),
                ),
                frozenset({actor}),
            )
            proposal_id = bootstrap_review.proposal_id
            baseline_preview = harness.service.proposal_preview(proposal_id)
            assert baseline_preview["preview_digest"] == _preview_digest(baseline_preview)
            baseline_payload = harness.sign(baseline_preview)
            before_unsigned = harness.state()
            with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
                harness.service.apply_decision(b"agent-only-without-signature", baseline_payload)
            assert harness.state() == before_unsigned

            baseline = harness.service.apply_decision(private_assertion, baseline_payload)
            assert baseline["status"] == "activated"
            after_baseline = harness.state()
            with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
                harness.service.apply_decision(private_assertion, baseline_payload)
            assert harness.state() == after_baseline

            # Open and answer a clarification through the authenticated answer preview.
            capture = ConversationCapture(harness.runtime.evidence_store)
            request = capture.record_turn(
                conversation_ref="codex:m1-release",
                role="human",
                author=actor,
                content="Add read-only sharing",
                captured_at=NOW - timedelta(microseconds=3),
                acl=("agent:codex", actor),
            )
            classification = capture.record_turn(
                conversation_ref="codex:m1-release",
                role="agent",
                author="agent:codex",
                content={"classification": "new_or_ambiguous"},
                captured_at=NOW - timedelta(microseconds=2),
                acl=("agent:codex", actor),
            )
            coordinator = ClarificationCoordinator(
                graph_store=harness.runtime.graph_store,
                evidence_store=harness.runtime.evidence_store,
                proposal_store=harness.runtime.intent_proposals,
                transactions=harness.runtime.transactions,
                config=harness.runtime.config,
                capture=capture,
            )
            task = TaskEnvelope(
                repository_id=harness.runtime.config.project_id,
                actor=actor,
                conversation_ref="codex:m1-release",
                request="Add read-only sharing",
                request_evidence_ref=request.id,
                graph_version=harness.runtime.graph_store.load().version,
                created_at=NOW - timedelta(microseconds=3),
            )
            session = coordinator.open(
                task,
                classification_evidence_ref=classification.id,
                questions=(
                    ClarificationQuestionInput(
                        id="audience", prompt="Who may share reports?", required=True
                    ),
                ),
                opened_by="agent:codex",
                opened_at=NOW - timedelta(microseconds=1),
                principals=frozenset({"agent:codex", actor}),
            )
            answer = "Workspace owners may share read-only reports."
            answer_preview = harness.service.answer_preview(session.id, "audience", answer)
            answer_payload = harness.sign(answer_preview)
            answer_result = harness.service.apply_decision(private_assertion, answer_payload)
            assert answer_result["status"] == "open"
            answered = harness.runtime.intent_proposals.session(session.id)
            answer_record = harness.runtime.evidence_store.get(answered.answers[0].evidence_ref)
            assert answer_record.author == actor
            assert answer_record.payload == {"role": "human", "content": answer}

            evidence_refs = (
                answered.request_evidence_ref,
                answered.classification_evidence_ref,
                *(item.evidence_ref for item in answered.questions),
                *(item.evidence_ref for item in answered.answers),
            )
            proposed = Node(
                id="req-read-only-sharing",
                type=NodeType.REQUIREMENT,
                label="Workspace owners may share read-only reports",
                status="proposed",
                created_by=actor,
                created_at=NOW,
                last_modified_by=actor,
                last_modified_at=NOW,
                source_mode=SourceMode.INFERRED,
                intent_fidelity_confidence=0.8,
                confidence_basis="Clarified human answer",
                last_reassessed_at=NOW,
                evidence_refs=evidence_refs,
            )
            proposal = coordinator.propose(
                ClarificationProposalSubmission(
                    session_id=answered.id,
                    task_id=answered.task_id,
                    baseline_graph_version=answered.baseline_graph_version,
                    actor=actor,
                    timestamp=NOW,
                    evidence_refs=evidence_refs,
                    changeset=ChangeSet(
                        id="",
                        actor=actor,
                        timestamp=NOW,
                        baseline_graph_version=answered.baseline_graph_version,
                        evidence_refs=evidence_refs,
                        nodes_added=(proposed,),
                        nodes_updated=(),
                        nodes_superseded=(),
                        edges_added=(),
                        edges_updated=(),
                        edges_superseded=(),
                        confidence_changes=(),
                        implementation_status_changes=(),
                        reconciliation_cases_created=(),
                        reconciliation_cases_resolved=(),
                        validation_status="validated",
                    ),
                    core_node_ids=(proposed.id,),
                ),
                principals=frozenset({"agent:codex", actor}),
            )
            proposal_preview = harness.service.proposal_preview(proposal.id)
            proposal_payload = harness.sign(proposal_preview)

            # Exact parent-state drift invalidates the signed preview without consuming it.
            config_path = project / ".intent/config.yaml"
            config_bytes = config_path.read_bytes()
            config_path.write_bytes(config_bytes + b"\n")
            before_drift_rejection = harness.state()
            with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
                harness.service.apply_decision(private_assertion, proposal_payload)
            assert harness.state() == before_drift_rejection
            config_path.write_bytes(config_bytes)

            proposal_result = harness.service.apply_decision(private_assertion, proposal_payload)
            assert proposal_result["status"] == "applied"

            # A cross-source disagreement remains explicit until the authenticated case decision.
            conflict = ReconciliationCase(
                id="case:m1-release-conflict",
                subject_ref="intent-local-export",
                case_type=ReconciliationCaseType.CONFLICTING_SOURCES,
                affected_refs=("intent-local-export",),
                evidence_sides=(
                    EvidenceSide(
                        label="current",
                        claim="Exports stay local",
                        evidence_refs=(evidence_ref,),
                        observed_at=NOW,
                        authors=(actor,),
                        confidence=0.9,
                        source_mode=SourceMode.EXPLICIT,
                        current=True,
                    ),
                ),
                detector_id="m1-release",
                fingerprint="e" * 64,
                created_at=NOW,
                created_by="agent:codex",
            )
            harness.runtime.case_store.put(conflict)
            proposed_case = transition_case(conflict, ReconciliationStatus.PROPOSED, actor, NOW)
            harness.runtime.case_store.put(proposed_case)
            human_case = transition_case(
                proposed_case, ReconciliationStatus.NEEDS_HUMAN, actor, NOW
            )
            harness.runtime.case_store.put(human_case)
            conflict_preview = harness.service.proposal_preview(conflict.id)
            conflict_payload = harness.sign(conflict_preview)
            conflict_result = harness.service.apply_decision(private_assertion, conflict_payload)

            graph = harness.runtime.graph_store.load()
            history = (project / ".intent/history/changesets.jsonl").read_text(encoding="utf-8")
            resolved = harness.runtime.case_store.get(conflict.id)
            assert conflict_result["action"] == ResolutionAction.UPDATE_REQUIREMENT.value
            assert resolved.status is ReconciliationStatus.RESOLVED
            assert resolved.history[-1].actor == actor
            assert graph.version == 3
            assert {node.last_modified_by for node in graph.nodes} == {actor}
            assert len(history.splitlines()) == 3
            assert private_assertion not in b"".join(
                path.read_bytes()
                for path in project.joinpath(".intent").rglob("*")
                if path.is_file()
            )
        except BaseException as error:  # noqa: BLE001 - retain diagnostics outside CLI boundary
            journey_errors.append(error)

    monkeypatch.setattr(dev_cli, "_wait_for_exit", journey)

    result = CliRunner().invoke(
        app,
        [
            "dev",
            "--project",
            str(project),
            "--prd",
            "docs/PRD.md",
            "--no-open",
            "--offline",
        ],
    )

    if journey_errors:
        raise journey_errors[0]
    assert result.exit_code == 0, repr(result.exception)
    assert len(load_calls) == 1
    assert result.stdout.startswith("intent dev: ready at http://localhost:")
    assert result.stderr == ""
    assert "release-signed-assertion" not in result.stdout + result.stderr
    assert not (project / ".intent/cache/control-plane.json").exists()


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_status_rejects_special_metadata_without_blocking_or_replacing(
    tmp_path: Path, kind: str
) -> None:
    project = _project(tmp_path)
    initialize_project(project)
    metadata = project / ".intent/cache/control-plane.json"
    if kind == "fifo":
        os.mkfifo(metadata)
    else:
        outside = tmp_path / "outside.json"
        outside.write_text("PRIVATE-OUTSIDE", encoding="utf-8")
        metadata.symlink_to(outside)
    before = metadata.lstat()

    result = subprocess.run(
        _command(project, "--status", "--no-open"),
        env=_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )

    after = metadata.lstat()
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent dev: unavailable\n"
    assert (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
    )
