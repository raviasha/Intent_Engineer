"""End-to-end contracts for the machine-facing ``intent ensure`` command."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import intent_engineering.cli.runtime as runtime_module
from intent_engineering.cli import dev as dev_cli
from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import Graph, Node, NodeType
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from tests.helpers.readiness import apply_baseline
from tests.helpers.shared_state import git, init_repository, keys, trust_environment

NOW = datetime(2026, 9, 7, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reuse_bounded_service(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """The real process lifecycle is covered by the automation journey."""
    if request.node.name in {
        "test_background_service_launch_is_argv_only_secret_free_and_bounded",
        "test_cancelled_background_exec_closes_both_pipe_ends_and_scrubs_trust",
    } or request.node.name.startswith(
        "test_background_readiness_fails_closed_on_a_reused_service_with_stale_health"
    ):
        return
    monkeypatch.setattr(dev_cli, "_start_or_reuse_background_service", lambda _root, _status: True)


def _ready_project(project: Path) -> None:
    initialize_project(project)
    runtime = load_runtime(project)
    try:
        apply_baseline(
            runtime,
            Graph(
                id="graph:ensure-cli",
                version=1,
                nodes=(
                    Node(
                        id="intent:ensure-cli",
                        type=NodeType.PRODUCT_INTENT,
                        label="Developer readiness starts from an approved baseline",
                        status="active",
                        created_by="local:owner",
                        created_at=NOW,
                        last_modified_by="local:owner",
                        last_modified_at=NOW,
                    ),
                ),
                edges=(),
            ),
        )
    finally:
        runtime.close()


def test_ensure_routes_an_uninitialized_repository_to_onboarding_without_writes(
    tmp_path: Path,
) -> None:
    """Catches the automatic developer entry point creating an empty graph or failing closed."""
    project = tmp_path / "uninitialized-project"
    project.mkdir()

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "version": "1",
        "schema_version": 1,
        "status": "onboarding_required",
        "attention_route": "onboarding",
        "graph_version": 0,
        "pending_proposal_ids": [],
        "open_case_ids": [],
    }
    assert not (project / ".intent").exists()


def test_ensure_reports_a_ready_initialized_repository(tmp_path: Path) -> None:
    """Catches the CLI failing to expose an aligned baseline to the developer preset."""
    project = tmp_path / "ready-project"
    project.mkdir()
    _ready_project(project)

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "version": "1",
        "schema_version": 1,
        "status": "ready",
        "attention_route": "home",
        "graph_version": 1,
        "pending_proposal_ids": [],
        "open_case_ids": [],
    }


@pytest.mark.parametrize(
    ("marker_kind", "expected_status"),
    [
        ("valid", "offline_stale"),
        ("corrupt", "shared_state_invalid"),
        ("fifo", "shared_state_invalid"),
    ],
)
def test_ensure_never_downgrades_a_governed_checkout_when_trust_is_missing(
    tmp_path: Path,
    marker_kind: str,
    expected_status: str,
) -> None:
    """Catches a prior team checkout becoming local-ready when trust is absent or unusable."""
    project = tmp_path / "governed-project"
    project.mkdir()
    _ready_project(project)
    marker = project / ".intent/cache/shared-state.json"
    if marker_kind == "valid":
        marker.write_text(
            json.dumps(
                {
                    "bundle_digest": "sha256:" + "a" * 64,
                    "graph_version": 1,
                    "ref_commit": "b" * 40,
                    "schema_version": 1,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    elif marker_kind == "corrupt":
        marker.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    else:
        os.mkfifo(marker)

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == expected_status
    assert payload["attention_route"] == "team_state"


def test_ensure_treats_unusable_trust_for_a_governed_checkout_as_offline_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches malformed trust masking the valid marker that proves prior team governance."""
    project = tmp_path / "governed-project"
    project.mkdir()
    _ready_project(project)
    (project / ".intent/cache/shared-state.json").write_text(
        json.dumps(
            {
                "bundle_digest": "sha256:" + "a" * 64,
                "graph_version": 1,
                "ref_commit": "b" * 40,
                "schema_version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", "{")

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "offline_stale"
    assert payload["attention_route"] == "team_state"


def test_ensure_reports_invalid_existing_state_without_leaking_details(tmp_path: Path) -> None:
    """Catches malformed local state being mistaken for a new repository or exposing its data."""
    project = tmp_path / "invalid-project"
    project.mkdir()
    (project / ".intent").mkdir()

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "version": "1",
        "schema_version": 1,
        "status": "shared_state_invalid",
        "attention_route": "team_state",
        "graph_version": 0,
        "pending_proposal_ids": [],
        "open_case_ids": [],
    }
    assert str(project) not in result.stdout + result.stderr


def test_ensure_redacts_malformed_configuration_without_leaking_details(tmp_path: Path) -> None:
    """Catches YAML parser errors escaping the machine-facing readiness contract."""
    project = tmp_path / "broken-config-project"
    project.mkdir()
    initialize_project(project)
    marker = "PRIVATE-INVALID-CONFIG-8197"
    (project / ".intent" / "config.yaml").write_text(f"local_actor: [{marker}", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "shared_state_invalid"
    assert marker not in result.stdout + result.stderr


def test_ensure_rejects_pending_transaction_without_recovery_writes(tmp_path: Path) -> None:
    """Catches a supposedly read-only readiness call replaying a torn transaction."""
    project = tmp_path / "prepared-transaction-project"
    project.mkdir()
    _ready_project(project)
    runtime = load_runtime(project)

    def crash(stage: str) -> None:
        if stage == "target:graph":
            raise SystemExit()

    coordinator = LocalTransactionCoordinator(
        runtime.workspace_directory.file("history/.local-transaction.json"),
        {
            "graph": runtime.workspace_directory.file("graph.yaml"),
            "history": runtime.workspace_directory.file("history/changesets.jsonl"),
            "cases": runtime.workspace_directory.file("reconciliation/cases.jsonl"),
            "evidence": runtime.workspace_directory.file("evidence/evidence.jsonl"),
            "receipts": runtime.workspace_directory.file("approvals/receipts.jsonl"),
            "approvals": runtime.workspace_directory.file("approvals/approvals.jsonl"),
            "intent_proposals": runtime.workspace_directory.file("history/intent-proposals.jsonl"),
            "webauthn_credentials": runtime.workspace_directory.file(
                "approvals/webauthn-credentials.jsonl"
            ),
            "webauthn_challenges": runtime.workspace_directory.file(
                "approvals/webauthn-challenges.jsonl"
            ),
        },
        fault_hook=crash,
        legacy_target_sets=(
            frozenset({"graph", "history", "cases"}),
            frozenset({"graph", "history", "cases", "evidence", "receipts"}),
            frozenset(
                {
                    "graph",
                    "history",
                    "cases",
                    "evidence",
                    "receipts",
                    "intent_proposals",
                }
            ),
            frozenset(
                {
                    "graph",
                    "history",
                    "cases",
                    "evidence",
                    "receipts",
                    "intent_proposals",
                    "webauthn_credentials",
                    "webauthn_challenges",
                }
            ),
        ),
    )
    try:
        with pytest.raises(SystemExit), coordinator.transaction() as transaction:
            transaction.write("graph", b"not: [a graph")
        before = {
            str(path.relative_to(project / ".intent")): path.read_bytes()
            for path in sorted((project / ".intent").rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
    finally:
        coordinator.close()
        runtime.close()

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "shared_state_invalid"
    assert {
        str(path.relative_to(project / ".intent")): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.is_symlink()
    } == before


def test_ensure_rejects_when_the_readiness_snapshot_changes_before_validation_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a mixed cross-store snapshot being treated as a trustworthy ready result."""
    project = tmp_path / "changing-project"
    project.mkdir()
    _ready_project(project)
    original = runtime_module._capture_readiness_snapshot
    marker = "PRIVATE-CHANGED-GRAPH-8197"
    calls = 0

    def changed_snapshot(workspace):
        nonlocal calls
        captured = original(workspace)
        calls += 1
        if calls == 1:
            (project / ".intent" / "graph.yaml").write_text(f"graph: [{marker}", encoding="utf-8")
        return captured

    monkeypatch.setattr(runtime_module, "_capture_readiness_snapshot", changed_snapshot)

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert calls == 1
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "shared_state_invalid"
    assert marker not in result.stdout + result.stderr


def test_ensure_redacts_graph_parse_failure_from_machine_output(tmp_path: Path) -> None:
    """Catches later graph reads leaking malformed state after runtime assembly succeeds."""
    project = tmp_path / "broken-graph-project"
    project.mkdir()
    initialize_project(project)
    marker = "PRIVATE-INVALID-GRAPH-8197"
    (project / ".intent" / "graph.yaml").write_text(f"graph: [{marker}", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "shared_state_invalid"
    assert marker not in result.stdout + result.stderr


def test_background_service_launch_is_argv_only_secret_free_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches lifecycle cleanup blocking or forwarding shared-state trust to the server."""
    project = tmp_path / "project"
    project.mkdir()
    calls: dict[str, object] = {}

    class NeverExits:
        def __init__(self) -> None:
            self.wait_timeouts: list[float] = []
            self.terminated = False
            self.killed = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> None:
            assert timeout is not None, "background cleanup must never wait without a deadline"
            self.wait_timeouts.append(timeout)
            raise subprocess.TimeoutExpired("intent dev", timeout)

    process = NeverExits()

    provenance_read = -1

    def popen(argv: tuple[str, ...], **kwargs: object) -> NeverExits:
        nonlocal provenance_read
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        pass_fds = kwargs.get("pass_fds")
        assert type(pass_fds) is tuple and len(pass_fds) == 1
        provenance_read = os.dup(pass_fds[0])
        return process

    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", "PRIVATE-TRUST-MARKER")
    monkeypatch.setattr(dev_cli, "_running_service", lambda _root: None)
    monkeypatch.setattr(dev_cli, "_STARTUP_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(dev_cli.subprocess, "Popen", popen)

    assert (
        dev_cli._start_or_reuse_background_service(
            project, dev_cli.SharedStateRestoreStatus.VERIFIED
        )
        is False
    )
    assert process.terminated is True
    assert process.killed is True
    assert process.wait_timeouts == [1, 1]
    assert type(calls["argv"]) is tuple
    kwargs = calls["kwargs"]
    assert type(kwargs) is dict
    assert kwargs["cwd"] == "/"
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert set(kwargs["env"]) == {"LANG", "LC_ALL", "PATH"}
    assert "shell" not in kwargs
    argv = calls["argv"]
    assert "--automatic" not in argv
    assert "--shared-status" not in argv
    assert "verified" not in argv
    assert provenance_read >= 0
    try:
        inherited = os.read(provenance_read, 64 * 1024)
        os.set_blocking(provenance_read, False)
        assert os.read(provenance_read, 1) == b""
    finally:
        os.close(provenance_read)
    assert b"PRIVATE-TRUST-MARKER" in inherited
    assert "PRIVATE-TRUST-MARKER" not in repr(argv)
    assert "PRIVATE-TRUST-MARKER" not in repr(kwargs["env"])


@pytest.mark.parametrize(
    "arguments",
    [
        ("--automatic", "--no-open"),
        ("--shared-status", "verified", "--no-open"),
        ("--automatic", "--shared-status", "verified", "--no-open"),
    ],
)
def test_dev_rejects_caller_supplied_launch_and_shared_health_authority(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    """Catches ordinary argv being able to mint automatic/verified process metadata."""
    project = tmp_path / "project"
    project.mkdir()
    _ready_project(project)

    result = CliRunner().invoke(
        app,
        ["dev", "--project", str(project), "--offline", *arguments],
    )

    assert result.exit_code == 2
    assert not (project / ".intent/cache/control-plane.json").exists()


def _install_provenance_pipe(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes | bytearray,
) -> int:
    read_descriptor, write_descriptor = os.pipe()
    try:
        os.write(write_descriptor, content)
    finally:
        os.close(write_descriptor)
    monkeypatch.setattr(
        dev_cli,
        "_inherited_provenance_descriptors",
        lambda: (read_descriptor,),
    )
    return read_descriptor


def test_fabricated_status_pipe_cannot_claim_verified_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an inherited caller-authored status being treated as proof of verification."""
    project = tmp_path / "project"
    project.mkdir()
    _ready_project(project)
    descriptor = _install_provenance_pipe(monkeypatch, b"verified")

    with pytest.raises(dev_cli._DevUnavailable):
        dev_cli._automatic_shared_restore(project)

    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_wrong_repository_trust_pipe_derives_invalid_instead_of_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches well-formed trust being accepted without binding the destination repository."""
    project = init_repository(tmp_path / "project")
    _recipient, _signer, trust = keys()
    git(project, "remote", "set-url", "origin", "https://github.com/acme/other.git")
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    descriptor = _install_provenance_pipe(
        monkeypatch,
        dev_cli._background_provenance_bytes(),
    )

    result = dev_cli._automatic_shared_restore(project)

    assert result.status is dev_cli.SharedStateRestoreStatus.INVALID
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_local_automatic_provenance_is_consumed_once_and_derives_marker_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches replayable descriptors or local launch packets carrying invented health."""
    project = tmp_path / "project"
    project.mkdir()
    _ready_project(project)
    monkeypatch.delenv("INTENT_CI_SHARED_STATE_TRUST", raising=False)
    descriptor = _install_provenance_pipe(
        monkeypatch,
        dev_cli._background_provenance_bytes(),
    )

    result = dev_cli._automatic_shared_restore(project)

    assert result.status is dev_cli.SharedStateRestoreStatus.NOT_REQUIRED
    with pytest.raises(dev_cli._DevUnavailable):
        dev_cli._automatic_shared_restore(project)
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_cancelled_provenance_read_closes_descriptor_and_scrubs_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cancellation retaining inherited private trust in descriptors or tracebacks."""
    project = tmp_path / "project"
    project.mkdir()
    marker = "PRIVATE-INHERITED-TRUST-90210"
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", marker)
    packet = bytes(dev_cli._background_provenance_bytes())
    descriptor = _install_provenance_pipe(monkeypatch, packet)
    original_read = os.read
    calls = 0

    class Cancelled(BaseException):
        pass

    cancellation = Cancelled("stop")

    def cancel_after_content(candidate: int, count: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_read(candidate, count)
        raise cancellation

    monkeypatch.setattr(dev_cli.os, "read", cancel_after_content)

    with pytest.raises(Cancelled) as raised:
        dev_cli._automatic_shared_restore(project)

    retained: list[str] = []
    traceback = raised.value.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            retained.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert raised.value is cancellation
    assert marker not in "\n".join(retained)
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_cancelled_background_exec_closes_both_pipe_ends_and_scrubs_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches parent-side exec cancellation leaking the one-use trust channel."""
    project = tmp_path / "project"
    project.mkdir()
    marker = "PRIVATE-PARENT-TRUST-41982"
    cancellation = BaseException("stop")
    descriptors: tuple[int, int] = ()
    original_pipe = os.pipe

    def capture_pipe() -> tuple[int, int]:
        nonlocal descriptors
        descriptors = original_pipe()
        return descriptors

    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", marker)
    monkeypatch.setattr(dev_cli, "_running_service", lambda _root: None)
    monkeypatch.setattr(dev_cli.os, "pipe", capture_pipe)
    monkeypatch.setattr(
        dev_cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(cancellation),
    )

    with pytest.raises(BaseException) as raised:
        dev_cli._start_or_reuse_background_service(
            project,
            dev_cli.SharedStateRestoreStatus.VERIFIED,
        )

    retained: list[str] = []
    traceback = raised.value.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            retained.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert raised.value is cancellation
    assert marker not in "\n".join(retained)
    assert len(descriptors) == 2
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("launch_mode", ["manual_headless", "interactive", "legacy"])
def test_background_readiness_fails_closed_on_a_reused_service_with_stale_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_mode: str,
) -> None:
    """Catches a manual or legacy UI silently retaining a contradictory shared status."""
    project = tmp_path / "project"
    project.mkdir()
    metadata = dev_cli.ControlPlaneProcessMetadata(
        pid=123,
        process_start_id="sha256:" + "a" * 64,
        instance_id="instance:" + "b" * 64,
        project_id="project",
        repository_id="repo:sha256:" + "c" * 64,
        origin="http://localhost:43127",
        launch_mode=launch_mode,
        shared_state_status=dev_cli.SharedStateRestoreStatus.NOT_REQUIRED,
    )
    monkeypatch.setattr(dev_cli, "_running_service", lambda _root: metadata)
    monkeypatch.setattr(
        dev_cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a non-automatic owner cannot be replaced")
        ),
    )

    assert (
        dev_cli._start_or_reuse_background_service(
            project, dev_cli.SharedStateRestoreStatus.INVALID
        )
        is False
    )
