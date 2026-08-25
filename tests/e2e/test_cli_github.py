"""GitHub CLI/runtime integration through injected, offline provider boundaries."""

from __future__ import annotations

import os
from asyncio import CancelledError
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from typer.testing import CliRunner

import intent_engineering.storage.secure as secure_storage
from intent_engineering.capture.github.auth import GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient, RetryPolicy
from intent_engineering.capture.github.models import GitHubRepositoryStatus
from intent_engineering.cli.app import app
from intent_engineering.cli.github import (
    GitHubDoctorDiagnostic,
    GitHubDoctorResult,
    check_github,
)
from intent_engineering.cli.runtime import (
    GitHubConfigurationError,
    Runtime,
    parse_sources,
    run_selected_sync,
)
from intent_engineering.core.models import EvidenceRecord, EvidenceSide, ReconciliationCase
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.storage.secure import SecureFile, UnsafePathError
from intent_engineering.sync.models import SyncRunResult, SyncRunStatus
from tests.helpers.cli import init_git_repo, run_intent
from tests.integration.github.conftest import FakeGitHubApi


class _RecordingClient:
    """Minimal connector-compatible client used to observe owned cleanup."""

    def __init__(self, *, close_failure: BaseException | None = None) -> None:
        self.closed = False
        self.close_failure = close_failure

    async def aclose(self) -> None:
        self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


class _RecordingSync:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.failure = failure

    async def run(self, run_id: str, connectors: object) -> SyncRunResult:
        connector_ids = tuple(item.connector_id for item in cast(tuple[object, ...], connectors))
        self.calls.append((run_id, connector_ids))
        if self.failure is not None:
            raise self.failure
        return SyncRunResult(
            run_id=run_id,
            status=SyncRunStatus.SUCCESS,
            connectors={},
            evidence_added=0,
            changes_applied=0,
            cases_created=0,
            duration_ms=0,
        )


def _runtime(tmp_path: Path, sync: _RecordingSync) -> Runtime:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    from intent_engineering.cli.runtime import load_runtime

    return replace(load_runtime(project), sync=cast(object, sync))  # type: ignore[arg-type]


def test_parse_sources_accepts_one_stable_duplicate_free_github_selection() -> None:
    """Removing GitHub from the allowlist or reordering selections breaks this contract."""
    assert parse_sources("github") == ("github",)
    assert parse_sources("markdown,git,github") == ("markdown", "git", "github")
    with pytest.raises(ValueError, match="duplicates"):
        parse_sources("github,github")


@pytest.mark.anyio
async def test_local_only_sync_never_resolves_github_configuration_or_credentials(
    tmp_path: Path,
) -> None:
    """A local-only selection must not touch any GitHub credential boundary."""
    sync = _RecordingSync()
    runtime = _runtime(tmp_path, sync)

    def fail_runner(_: list[str]) -> str:
        raise AssertionError("GitHub CLI must not run")

    def fail_factory(_: GitHubCredentials) -> GitHubClient:
        raise AssertionError("GitHub client must not be constructed")

    result = await run_selected_sync(
        runtime,
        "markdown,git",
        "run:local",
        env={},
        token_runner=fail_runner,
        client_factory=fail_factory,
    )

    assert result.status is SyncRunStatus.SUCCESS
    assert sync.calls == [("run:local", ("markdown", "git"))]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scope",
    [None, "", " acme/demo", "acme/demo ", "acme", "acme/demo/extra", 42],
)
async def test_invalid_repository_scope_fails_before_sync_or_client_construction(
    tmp_path: Path,
    scope: object,
) -> None:
    """Invalid provider scope cannot reach state mutation or network construction."""
    sync = _RecordingSync()
    runtime = _runtime(tmp_path, sync)
    before = runtime.graph_store.path.read_bytes()
    constructed = False

    def client_factory(_: GitHubCredentials) -> GitHubClient:
        nonlocal constructed
        constructed = True
        return cast(GitHubClient, _RecordingClient())

    environment: dict[str, object] = {"GH_TOKEN": "offline-token"}
    if scope is not None:
        environment["GITHUB_REPOSITORY"] = scope

    with pytest.raises(GitHubConfigurationError, match="repository scope") as caught:
        await run_selected_sync(
            runtime,
            "github",
            "run:invalid",
            env=environment,
            token_runner=lambda _: "unused",
            client_factory=client_factory,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert constructed is False
    assert sync.calls == []
    assert runtime.graph_store.path.read_bytes() == before


@pytest.mark.anyio
async def test_combined_sources_use_one_orchestrator_run_and_close_owned_client(
    tmp_path: Path,
) -> None:
    """Sequential provider runs would violate combined-delta reconciliation semantics."""
    sync = _RecordingSync()
    runtime = _runtime(tmp_path, sync)
    client = _RecordingClient()

    result = await run_selected_sync(
        runtime,
        "markdown,git,github",
        "run:combined",
        env={"GH_TOKEN": "offline-token", "GITHUB_REPOSITORY": "Acme/Demo"},
        token_runner=lambda _: "unused",
        client_factory=lambda _: cast(GitHubClient, client),
    )

    assert result.status is SyncRunStatus.SUCCESS
    assert sync.calls == [
        ("run:combined", ("markdown", "git", "github:acme/demo")),
    ]
    assert client.closed is True


@pytest.mark.anyio
async def test_github_client_is_closed_without_replacing_the_original_failure(
    tmp_path: Path,
) -> None:
    """Owned cleanup must run on failure while preserving the failing operation."""
    failure = RuntimeError("original safe failure")
    sync = _RecordingSync(failure=failure)
    runtime = _runtime(tmp_path, sync)
    cleanup_secret = "gh" + "p_discarded-cleanup-secret"
    client = _RecordingClient(close_failure=RuntimeError(cleanup_secret))

    with pytest.raises(RuntimeError, match="original safe failure") as caught:
        await run_selected_sync(
            runtime,
            "github",
            "run:failure",
            env={"GH_TOKEN": "offline-token", "GITHUB_REPOSITORY": "acme/demo"},
            token_runner=lambda _: "unused",
            client_factory=lambda _: cast(GitHubClient, client),
        )

    assert caught.value is failure
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert cleanup_secret not in repr(caught.value)
    assert client.closed is True


@pytest.mark.anyio
async def test_github_sync_uses_real_runtime_stores_and_is_byte_stable_on_retry(
    tmp_path: Path,
) -> None:
    """A fake HTTP provider still traverses production connector/orchestrator/store seams."""
    project = tmp_path / "real-github-runtime"
    project.mkdir()
    initialize_project(project)
    from intent_engineering.cli.runtime import load_runtime

    runtime = load_runtime(project)
    api = FakeGitHubApi()
    clients: list[GitHubClient] = []

    def factory(credentials: GitHubCredentials) -> GitHubClient:
        client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(api.handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        clients.append(client)
        return client

    environment = {"GH_TOKEN": "offline-token", "GITHUB_REPOSITORY": "acme/demo"}
    first = await run_selected_sync(
        runtime,
        "github",
        "run:github-first",
        env=environment,
        token_runner=lambda _: "unused",
        client_factory=factory,
    )
    evidence_before = (project / ".intent/evidence/evidence.jsonl").read_bytes()
    checkpoint_before = (project / ".intent/cache/checkpoints.yaml").read_bytes()
    second = await run_selected_sync(
        runtime,
        "github",
        "run:github-second",
        env=environment,
        token_runner=lambda _: "unused",
        client_factory=factory,
    )

    checkpoint = runtime.checkpoint_store.get("github:acme/demo")
    assert first.status is second.status is SyncRunStatus.SUCCESS
    assert first.evidence_added == 5 and second.evidence_added == 0
    assert checkpoint is not None and checkpoint.cursor is not None
    assert (project / ".intent/evidence/evidence.jsonl").read_bytes() == evidence_before
    assert (project / ".intent/cache/checkpoints.yaml").read_bytes() == checkpoint_before
    assert all(client.is_closed for client in clients)


@pytest.mark.anyio
async def test_github_cancellation_propagates_after_cleanup_even_when_cleanup_fails(
    tmp_path: Path,
) -> None:
    """Cancellation is a BaseException control signal and must remain the visible outcome."""
    cancellation = CancelledError()
    sync = _RecordingSync(failure=cancellation)
    runtime = _runtime(tmp_path, sync)
    client = _RecordingClient(close_failure=KeyboardInterrupt("discarded close failure"))

    with pytest.raises(CancelledError) as caught:
        await run_selected_sync(
            runtime,
            "github",
            "run:cancelled",
            env={"GH_TOKEN": "offline-token", "GITHUB_REPOSITORY": "acme/demo"},
            token_runner=lambda _: "unused",
            client_factory=lambda _: cast(GitHubClient, client),
        )

    assert caught.value is cancellation
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert client.closed is True


def _doctor_client_factory(
    handler: object,
    captured: list[GitHubClient],
) -> object:
    def factory(credentials: GitHubCredentials) -> GitHubClient:
        client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
            retry_policy=RetryPolicy(max_attempts=1),
        )
        captured.append(client)
        return client

    return factory


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("environment", "cli_token", "expected_source"),
    [
        (
            {"GH_TOKEN": "environment-secret", "GITHUB_REPOSITORY": "Acme/Demo"},
            "unused",
            "environment",
        ),
        ({"GITHUB_REPOSITORY": "acme/demo"}, "cli-secret", "github_cli"),
    ],
)
async def test_github_doctor_reports_safe_access_for_both_local_credential_sources(
    tmp_path: Path,
    environment: dict[str, object],
    cli_token: str,
    expected_source: str,
) -> None:
    """Doctor must report credential provenance without returning credential bytes."""
    sync = _RecordingSync()
    runtime = _runtime(tmp_path, sync)
    captured: list[GitHubClient] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"full_name": "acme/demo", "private": "provider-only"},
            headers={
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "4999",
                "X-RateLimit-Used": "1",
                "X-RateLimit-Reset": "1787659200",
                "X-RateLimit-Resource": "core",
            },
        )

    result = await check_github(
        runtime,
        env=environment,
        token_runner=lambda _: cli_token,
        client_factory=cast(object, _doctor_client_factory(handler, captured)),
    )

    assert result.healthy is True
    assert result.repository == "acme/demo"
    assert result.credential_source.value == expected_source
    assert result.access == "accessible"
    assert result.rate_remaining == 4999
    assert result.error is None
    assert all(client.is_closed for client in captured)
    serialized = result.model_dump_json()
    assert "environment-secret" not in serialized
    assert "cli-secret" not in serialized
    assert "provider-only" not in serialized


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "headers", "body", "expected_code"),
    [
        (403, {"X-GitHub-Request-Id": "safe"}, b"PRIVATE", "github.permission"),
        (
            429,
            {"X-RateLimit-Remaining": "0", "Retry-After": "60"},
            b"PRIVATE",
            "github.rate_limit",
        ),
        (200, {}, b"not-json PRIVATE", "github.protocol"),
    ],
)
async def test_github_doctor_converts_provider_failures_to_fixed_redacted_diagnostics(
    tmp_path: Path,
    status: int,
    headers: dict[str, str],
    body: bytes,
    expected_code: str,
) -> None:
    runtime = _runtime(tmp_path, _RecordingSync())
    secret = "gh" + "p_fragmented-doctor-token"
    captured: list[GitHubClient] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, content=body + secret.encode())

    result = await check_github(
        runtime,
        env={"GH_TOKEN": secret, "GITHUB_REPOSITORY": "acme/demo"},
        token_runner=lambda _: "unused",
        client_factory=cast(object, _doctor_client_factory(handler, captured)),
    )

    assert result.healthy is False
    assert result.error is not None and result.error.code == expected_code
    assert secret not in result.model_dump_json()
    assert "PRIVATE" not in result.model_dump_json()
    assert all(client.is_closed for client in captured)


@pytest.mark.anyio
async def test_github_doctor_redacts_transport_and_fragmented_request_id_tokens(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, _RecordingSync())
    secret = "gh" + "p_doctor-request-id-secret"

    def permission_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            content=("PRIVATE-" + secret).encode(),
            headers={"X-GitHub-Request-Id": f"prefix-{secret}-suffix"},
        )

    def transport_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("PRIVATE-" + secret, request=request)

    for handler, expected_code in (
        (permission_handler, "github.permission"),
        (transport_handler, "github.transport"),
    ):
        captured: list[GitHubClient] = []
        result = await check_github(
            runtime,
            env={"GH_TOKEN": secret, "GITHUB_REPOSITORY": "acme/demo"},
            token_runner=lambda _: "unused",
            client_factory=cast(object, _doctor_client_factory(handler, captured)),
        )
        assert result.error is not None and result.error.code == expected_code
        assert secret not in result.model_dump_json()
        assert "PRIVATE" not in result.model_dump_json()
        assert all(client.is_closed for client in captured)


class _DoctorLifecycleClient:
    def __init__(
        self,
        *,
        probe_failure: BaseException | None = None,
        close_failure: BaseException | None = None,
    ) -> None:
        self.probe_failure = probe_failure
        self.close_failure = close_failure
        self.closed = False

    async def get_repository_status(self, repository: str) -> GitHubRepositoryStatus:
        if self.probe_failure is not None:
            raise self.probe_failure
        return GitHubRepositoryStatus(
            repository=repository,
            accessible=True,
            rate_limit=5000,
            rate_remaining=4999,
            rate_used=1,
            rate_reset_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
            rate_resource="core",
        )

    async def aclose(self) -> None:
        self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


@pytest.mark.anyio
async def test_github_doctor_reports_fixed_close_failure_without_retaining_it(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, _RecordingSync())
    secret = "gh" + "p_close-failure-secret"
    client = _DoctorLifecycleClient(close_failure=RuntimeError(secret))

    result = await check_github(
        runtime,
        env={"GH_TOKEN": secret, "GITHUB_REPOSITORY": "acme/demo"},
        token_runner=lambda _: "unused",
        client_factory=lambda _: cast(GitHubClient, client),
    )

    assert result.error == GitHubDoctorDiagnostic(
        code="github.close",
        message="GitHub client cleanup failed.",
    )
    assert secret not in result.model_dump_json()
    assert client.closed is True


@pytest.mark.anyio
async def test_github_doctor_cancellation_propagates_after_cleanup(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _RecordingSync())
    cancellation = CancelledError()
    client = _DoctorLifecycleClient(
        probe_failure=cancellation,
        close_failure=RuntimeError("discarded cleanup"),
    )

    with pytest.raises(CancelledError) as caught:
        await check_github(
            runtime,
            env={"GH_TOKEN": "offline-token", "GITHUB_REPOSITORY": "acme/demo"},
            token_runner=lambda _: "unused",
            client_factory=lambda _: cast(GitHubClient, client),
        )

    assert caught.value is cancellation
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert client.closed is True


@pytest.mark.anyio
async def test_github_doctor_client_factory_cancellation_propagates(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _RecordingSync())
    cancellation = CancelledError()

    def cancelled_factory(_: GitHubCredentials) -> GitHubClient:
        raise cancellation

    with pytest.raises(CancelledError) as caught:
        await check_github(
            runtime,
            env={"GH_TOKEN": "offline-token", "GITHUB_REPOSITORY": "acme/demo"},
            token_runner=lambda _: "unused",
            client_factory=cancelled_factory,
        )

    assert caught.value is cancellation


@pytest.mark.anyio
async def test_github_doctor_auth_failure_is_fixed_and_never_constructs_a_client(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, _RecordingSync())
    constructed = False

    def factory(_: GitHubCredentials) -> GitHubClient:
        nonlocal constructed
        constructed = True
        raise AssertionError

    result = await check_github(
        runtime,
        env={"GITHUB_REPOSITORY": "acme/demo"},
        token_runner=lambda _: "",
        client_factory=factory,
    )

    assert result == GitHubDoctorResult.failed(
        repository="acme/demo",
        diagnostic=GitHubDoctorDiagnostic(
            code="github.authentication",
            message="GitHub authentication unavailable; set GH_TOKEN or run `gh auth login`.",
        ),
    )
    assert constructed is False


def test_doctor_github_cli_emits_versioned_json_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "doctor-project"
    project.mkdir()
    initialize_project(project)
    result_model = GitHubDoctorResult(
        healthy=True,
        repository="acme/demo",
        credential_source="environment",
        access="accessible",
        rate_limit=5000,
        rate_remaining=4999,
        rate_used=1,
        rate_reset_at="2026-08-25T12:00:00Z",
        rate_resource="core",
    )

    async def fake_check(*_: object, **__: object) -> GitHubDoctorResult:
        return result_model

    monkeypatch.setattr("intent_engineering.cli.app.check_github", fake_check)
    monkeypatch.setattr("intent_engineering.cli.app._configure_logging", lambda: None)
    secret = "gh" + "p_cli-output-secret"
    result = CliRunner().invoke(
        app,
        ["doctor", "github", "--project", str(project), "--format", "json"],
        env={"GH_TOKEN": secret, "GITHUB_REPOSITORY": "acme/demo"},
    )

    assert result.exit_code == 0
    assert '"version":"1"' in result.stdout
    assert '"credential_source":"environment"' in result.stdout
    assert secret not in result.stdout
    assert secret not in (result.stderr or "")


@pytest.mark.parametrize(
    "args",
    [
        ("sync", "--sources", "github"),
        ("doctor", "github"),
    ],
)
def test_missing_github_scope_is_usage_failure_before_project_loading(
    tmp_path: Path,
    args: tuple[str, ...],
) -> None:
    project = init_git_repo(tmp_path)

    result = run_intent(project, *args)

    assert result.returncode == 2
    assert result.stdout == ""
    assert not (project / ".intent").exists()
    assert str(project) not in result.stderr


def test_doctor_github_help_is_available_without_scope_or_credentials(tmp_path: Path) -> None:
    project = init_git_repo(tmp_path)
    result = run_intent(project, "doctor", "github", "--help")
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def _seed_cli_case(project: Path, *, acl: tuple[str, ...] = ()) -> ReconciliationCase:
    from intent_engineering.cli.runtime import load_runtime

    runtime = load_runtime(project)
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    evidence = EvidenceRecord(
        id="evidence:drift-report",
        connector_type="fixture",
        external_object_id="drift-report",
        external_version="1",
        author="fixture",
        observed_at=now,
        source_locator="fixture:drift-report",
        content_hash="sha256:drift-report",
        payload={},
        acl=acl,
    )
    runtime.evidence_store.put(evidence)
    case = ReconciliationCase(
        id="case:drift-report",
        subject_ref="requirement:export",
        case_type="CODE_LAG",
        affected_refs=("requirement:export",),
        evidence_sides=(
            EvidenceSide(
                label="requirement",
                claim="export changed",
                evidence_refs=(evidence.id,),
                observed_at=now,
                authors=("fixture",),
                confidence=0.9,
            ),
        ),
        detector_id="fixture",
        fingerprint="d" * 64,
        created_at=now,
        created_by="detector:fixture",
        impact="Update implementation and tests",
    )
    runtime.case_store.put(case)
    return case


def test_markdown_drift_stdout_matches_atomic_output_and_open_cases_exit_zero(
    tmp_path: Path,
) -> None:
    project = tmp_path / "drift-project"
    project.mkdir()
    initialize_project(project)
    _seed_cli_case(project)

    first = run_intent(project, "drift", "--format", "markdown", "--output", "intent-drift.md")
    repeated = run_intent(
        project,
        "drift",
        "--format",
        "markdown",
        "--output",
        "intent-drift.md",
    )
    second = run_intent(project, "drift", "--format", "markdown")
    required = run_intent(project, "drift", "--format", "markdown", "--require-review")

    assert first.returncode == repeated.returncode == second.returncode == 0
    assert required.returncode == 4
    assert first.stdout == repeated.stdout == second.stdout == required.stdout
    assert (project / "intent-drift.md").read_bytes() == first.stdout.encode("utf-8")
    assert first.stdout.endswith("\n") and not first.stdout.endswith("\n\n")
    assert "case:drift-report" in first.stdout


def test_markdown_drift_reuses_acl_projection_and_hides_denied_cases(tmp_path: Path) -> None:
    project = tmp_path / "acl-project"
    project.mkdir()
    initialize_project(project)
    _seed_cli_case(project, acl=("another-actor",))

    result = run_intent(project, "drift", "--format", "markdown")

    assert result.returncode == 0
    assert result.stdout == (
        "# Intent Engineering drift report\n\n_No authorized open reconciliation cases._\n"
    )
    assert "case:drift-report" not in result.stdout


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "directory", "absolute", "escape"])
def test_markdown_drift_refuses_unsafe_output_without_touching_targets(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    project = tmp_path / f"unsafe-{unsafe_kind}"
    project.mkdir()
    initialize_project(project)
    _seed_cli_case(project)
    outside = tmp_path / f"outside-{unsafe_kind}.md"
    outside.write_text("unchanged", encoding="utf-8")
    target = "intent-drift.md"
    if unsafe_kind == "symlink":
        (project / target).symlink_to(outside)
    elif unsafe_kind == "hardlink":
        os.link(outside, project / target)
    elif unsafe_kind == "directory":
        (project / target).mkdir()
    elif unsafe_kind == "absolute":
        target = str(outside)
    else:
        target = "../" + outside.name

    result = run_intent(project, "drift", "--format", "markdown", "--output", target)

    assert result.returncode != 0
    assert outside.read_text(encoding="utf-8") == "unchanged"
    if unsafe_kind == "hardlink":
        assert (project / "intent-drift.md").read_text(encoding="utf-8") == "unchanged"


def test_markdown_drift_rejects_a_final_target_swapped_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "raced-output"
    project.mkdir()
    initialize_project(project)
    _seed_cli_case(project)
    outside = tmp_path / "outside-race.md"
    outside.write_text("outside-unchanged", encoding="utf-8")
    target = project / "intent-drift.md"
    target.write_text("original-report", encoding="utf-8")
    original_assert = SecureFile._assert_safe_existing_target
    swapped = False

    def swap_after_validation(secure_file: SecureFile) -> os.stat_result | None:
        nonlocal swapped
        metadata = original_assert(secure_file)
        if secure_file.path == target and not swapped:
            target.unlink()
            target.symlink_to(outside)
            swapped = True
        return metadata

    monkeypatch.setattr(SecureFile, "_assert_safe_existing_target", swap_after_validation)
    monkeypatch.setattr("intent_engineering.cli.app._configure_logging", lambda: None)

    result = CliRunner().invoke(
        app,
        [
            "drift",
            "--project",
            str(project),
            "--format",
            "markdown",
            "--output",
            target.name,
        ],
    )

    assert swapped is True
    assert result.exit_code == 1
    assert target.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside-unchanged"


def test_verified_report_write_rejects_a_hardlink_added_at_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "intent-drift.md"
    outside = tmp_path / "raced-hardlink.md"
    report = b"private report\n"
    secure_file = SecureFile.from_path(target)
    original_stat = secure_storage.os.stat
    linked = False

    def add_hardlink_before_installed_stat(
        path: os.PathLike[str] | str | int,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal linked
        metadata = original_stat(path, *args, **kwargs)
        if path == target.name and kwargs.get("dir_fd") == secure_file.parent_fd and not linked:
            os.link(target, outside)
            linked = True
            metadata = original_stat(path, *args, **kwargs)
        return metadata

    monkeypatch.setattr(secure_storage.os, "stat", add_hardlink_before_installed_stat)
    try:
        with pytest.raises(UnsafePathError):
            secure_file.atomic_write(report, reject_target_races=True)
    finally:
        secure_file.close()

    assert linked is True
    assert not target.exists()


def test_drift_json_contract_remains_versioned_and_output_is_markdown_only(tmp_path: Path) -> None:
    project = tmp_path / "json-project"
    project.mkdir()
    initialize_project(project)
    _seed_cli_case(project)

    default = run_intent(project, "drift", "--format", "json")
    invalid_output = run_intent(
        project,
        "drift",
        "--format",
        "json",
        "--output",
        "report.json",
    )

    assert default.returncode == 0
    assert default.json()["version"] == "1"
    assert default.json()["review_required"] is True
    assert invalid_output.returncode == 2
    assert not (project / "report.json").exists()
