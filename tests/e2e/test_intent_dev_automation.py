"""Offline release proof through real Git, signed state, reviewed tests and the CLI."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import anyio
import pytest
import structlog
import yaml  # type: ignore[import-untyped]
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from typer.testing import CliRunner

from intent_engineering.cli import dev as dev_cli
from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import (
    ChangeSet,
    EvidenceSide,
    ReconciliationCase,
    ReconciliationCaseType,
)
from intent_engineering.intent_workflow.check import CheckService
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.team_state import restore as restore_module
from intent_engineering.team_state.restore import (
    GitSharedStateRestorer,
    SharedStateTrust,
    StaticTrustProvider,
    TrustedSigningKey,
    build_state_payload,
    seal_state_payload,
)
from tests.helpers.shared_state import (
    NOW,
    RECIPIENT_ID,
    REPOSITORY_ID,
    SIGNER_ID,
    canonical_files,
    git,
    init_repository,
    install_state_ref,
    ready_project,
    trust_environment,
)


@pytest.fixture(autouse=True)
def _reset_cli_logging(request: pytest.FixtureRequest) -> None:
    """CLI capture streams must not remain installed for later library test modules."""
    request.addfinalizer(structlog.reset_defaults)


def _action():
    path = Path(__file__).parents[2] / "src/intent_engineering/integrations/github_action.py"
    assert path.is_file(), "reviewed GitHub check runner is missing"
    return importlib.import_module("intent_engineering.integrations.github_action")


def _clone(
    tmp_path: Path,
    *,
    case_type: str | None = None,
    failing: bool = False,
    commands: list[list[str]] | None = None,
    result_paths: list[str] | None = None,
):
    source = init_repository(tmp_path / "source" / "project")
    ready_project(source)
    (source / ".gitignore").write_text(".intent/\n.intent-ci/\n", encoding="utf-8")
    (source / "feature.py").write_text("def enabled():\n    return True\n", encoding="utf-8")
    runner = source / "tools/test-runner"
    runner.parent.mkdir()
    runner.write_text(
        "#!/usr/bin/python3\nimport runpy,sys\nassert runpy.run_path('feature.py')['enabled']()\n"
        "if sys.argv[-1] == 'fail': raise SystemExit(1)\n"
        + ("raise SystemExit(1)\n" if failing else ""),
        encoding="utf-8",
    )
    runner.chmod(0o755)
    config_path = source / ".intent/config.yaml"
    config = yaml.safe_load(config_path.read_bytes())
    config["test_commands"] = [["tools/test-runner"]] if commands is None else commands
    config["test_result_paths"] = result_paths or []
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    if case_type is not None:
        runtime = load_runtime(source)
        try:
            evidence = runtime.evidence()[0]
            case = ReconciliationCase(
                id="case:release-proof",
                subject_ref="requirement:approved",
                case_type=ReconciliationCaseType(case_type),
                affected_refs=("requirement:approved",),
                evidence_sides=(
                    EvidenceSide(
                        label="Unresolved approved evidence",
                        claim="Human review is required",
                        evidence_refs=(evidence.id,),
                        observed_at=NOW,
                        authors=(evidence.author,),
                        confidence=0.8,
                    ),
                ),
                detector_id="release-proof",
                fingerprint="a" * 64,
                created_at=NOW,
                created_by="local:owner",
            )
            LocalChangeSetExecutor(
                runtime.graph_store, runtime.case_store, runtime.transactions
            ).apply(
                ChangeSet(
                    id="changeset:review-required",
                    actor="local:owner",
                    timestamp=NOW,
                    baseline_graph_version=1,
                    evidence_refs=(evidence.id,),
                    nodes_added=(),
                    nodes_updated=(),
                    nodes_superseded=(),
                    edges_added=(),
                    edges_updated=(),
                    edges_superseded=(),
                    confidence_changes=(),
                    implementation_status_changes=(),
                    reconciliation_cases_created=(case.id,),
                    reconciliation_cases_resolved=(),
                    validation_status="validated",
                ),
                created_cases=(case,),
            )
        finally:
            runtime.close()
    git(source, "add", ".gitignore", "feature.py", "tools/test-runner")
    git(source, "commit", "-qm", "feat: implement approved behavior with a verifying test")
    recipient = X25519PrivateKey.from_private_bytes(b"r" * 32)
    signer = Ed25519PrivateKey.from_private_bytes(b"s" * 32)
    trust = SharedStateTrust(
        project_id="project",
        repository_id=REPOSITORY_ID,
        recipient_key_id=RECIPIENT_ID,
        recipient_private_key=recipient.private_bytes_raw(),
        signing_keys=(
            TrustedSigningKey(
                signature_id=SIGNER_ID,
                public_key=signer.public_key().public_bytes_raw(),
            ),
        ),
    )
    release = seal_state_payload(
        build_state_payload(canonical_files(source)),
        project_id="project",
        repository_id=REPOSITORY_ID,
        graph_version=2 if case_type else 1,
        parent_bundle_digest=None,
        created_at=NOW,
        recipient_public_keys={RECIPIENT_ID: recipient.public_key().public_bytes_raw()},
        signing_private_keys={SIGNER_ID: signer.private_bytes_raw()},
    )
    state_commit = install_state_ref(source, release)
    git(source, "update-ref", "refs/heads/intent-state", state_commit)
    target = tmp_path / "clone" / "project"
    target.parent.mkdir()
    git(source, "clone", "--quiet", "--no-hardlinks", str(source), str(target))
    git(target, "remote", "set-url", "origin", "https://github.com/acme/project.git")
    assert not (target / ".intent").exists()
    return target, trust


def _check(repo: Path, trust: SharedStateTrust, *, ci: bool = True):
    with patch(
        "intent_engineering.cli.app.CheckService",
        lambda adapter: CheckService(adapter, clock=lambda: NOW),
    ):
        return CliRunner().invoke(
            app,
            [
                "check",
                "--project",
                str(repo),
                *(["--ci"] if ci else []),
                "--require-review",
                "--test-results",
                ".intent-ci/test-results.json",
            ],
            env={"INTENT_CI_SHARED_STATE_TRUST": trust_environment(trust)},
        )


def _stop_background_dev(repo: Path) -> None:
    metadata = repo / ".intent/cache/control-plane.json"
    try:
        pid = json.loads(metadata.read_bytes())["pid"]
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return
    if type(pid) is not int or pid < 1:
        return
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            waited = 0
        if waited == pid:
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)


def test_fresh_onboarded_clone_first_prompt_and_plugin_disabled_ci_backstop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Removing restore, reviewed execution, canonical binding or CLI assurance breaks this."""
    action = _action()
    repo, trust = _clone(tmp_path)
    request.addfinalizer(lambda: _stop_background_dev(repo))
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    monkeypatch.setattr(
        restore_module,
        "_refresh_state_ref",
        lambda repository_id: (
            restore_module._GitRefReader(repo)
            if repository_id == trust.repository_id
            else (_ for _ in ()).throw(AssertionError("wrong trusted repository"))
        ),
    )
    original_head = git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    first_prompt = CliRunner().invoke(
        app,
        ["agent-prompt-hook", "--preset", "developer"],
        input=json.dumps(
            {
                "session_id": "release-proof",
                "turn_id": "first",
                "transcript_path": None,
                "cwd": str(repo),
                "hook_event_name": "UserPromptSubmit",
                "model": "test-host",
                "permission_mode": "default",
                "prompt": "Implement the approved behavior and its test",
            }
        ),
    )
    assert first_prompt.exit_code == 0, first_prompt.stdout
    context = json.loads(first_prompt.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "intent_advisory_preflight" in context
    assert "onboarding" not in context.lower()
    metadata = json.loads((repo / ".intent/cache/control-plane.json").read_bytes())
    assert metadata["pid"] != os.getpid()
    assert metadata["shared_state_status"] == "verified"
    with urlopen(
        Request(
            metadata["origin"] + dev_cli._INSTANCE_PATH,
            headers={
                "Host": metadata["origin"].removeprefix("http://"),
                "Origin": metadata["origin"],
            },
        ),
        timeout=3,
    ) as response:
        assert json.loads(response.read())["shared_state_status"] == "verified"
    status = CliRunner().invoke(app, ["dev", "--project", str(repo), "--status"])
    assert status.exit_code == 0, (status.stdout, status.stderr, repr(status.exception))
    assert status.stdout == f"intent dev: running at {metadata['origin']}\n"
    assert git(repo, "rev-parse", "HEAD") == original_head
    # The GitHub path has no plugin invocation and restores from the signed ref again.
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    artifact = json.loads((repo / ".intent-ci/test-results.json").read_bytes())
    assert artifact["commit_sha"] == original_head.decode().strip()
    assert artifact["status"] == "passed"
    result = _check(repo, trust)
    assert result.exit_code == 1, (result.stdout, result.stderr, repr(result.exception))
    assert json.loads(result.stdout)["reason"] == "test_environment_unsupported"
    assert _check(repo, trust, ci=False).exit_code == 0
    assert not (repo / "plugins").exists()
    assert git(repo, "rev-parse", "HEAD") == original_head


@pytest.mark.parametrize("offline", [False, True], ids=("refreshed", "offline-cached"))
def test_direct_dev_restores_team_state_before_starting_the_control_plane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: bool,
) -> None:
    """Catches the primary CLI retaining a hidden manual restore prerequisite."""
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    refreshes: list[str] = []

    def refresh(repository_id: str):
        refreshes.append(repository_id)
        if offline:
            raise AssertionError("offline intent dev must not fetch")
        return restore_module._GitRefReader(repo)

    monkeypatch.setattr(restore_module, "_refresh_state_ref", refresh)
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        dev_cli,
        "_wait_for_exit",
        lambda started: observed.append(started.service.status()),
    )

    result = CliRunner().invoke(
        app,
        ["dev", "--project", str(repo), "--no-open", *(["--offline"] if offline else [])],
    )

    assert result.exit_code == 0, (result.stdout, result.stderr, repr(result.exception))
    assert (repo / ".intent/cache/shared-state.json").is_file()
    assert observed[0]["status"] == "ready"
    assert refreshes == ([] if offline else [trust.repository_id])


def test_direct_offline_dev_preserves_missing_trust_as_offline_stale_team_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches direct dev downgrading a previously governed checkout to local-only."""
    repo, trust = _clone(tmp_path)
    assert (
        GitSharedStateRestorer(StaticTrustProvider(trust))
        .verify_and_restore_approved_baseline(repo)
        .status.value
        == "verified"
    )
    monkeypatch.delenv("INTENT_CI_SHARED_STATE_TRUST", raising=False)
    monkeypatch.setattr(
        restore_module,
        "_refresh_state_ref",
        lambda _repository_id: (_ for _ in ()).throw(
            AssertionError("offline intent dev must not fetch")
        ),
    )
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        dev_cli,
        "_wait_for_exit",
        lambda started: observed.append(started.service.status()),
    )

    result = CliRunner().invoke(
        app,
        ["dev", "--project", str(repo), "--no-open", "--offline"],
    )

    assert result.exit_code == 0, (result.stdout, result.stderr, repr(result.exception))
    assert observed[0]["status"] == "offline_stale"
    assert observed[0]["attention_route"] == "team_state"


@pytest.mark.parametrize("case_type", ["AMBIGUOUS_DIVERGENCE", "CONFLICTING_SOURCES"])
def test_required_check_blocks_unresolved_approved_cases_without_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_type: str,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path, case_type=case_type)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    result = _check(repo, trust)
    assert result.exit_code == 4, (result.stdout, repr(result.exception))
    assert json.loads(result.stdout)["readiness_status"] == "human_attention_required"
    runtime = load_runtime(repo)
    try:
        assert runtime.cases()[0].case_type.value == case_type
    finally:
        runtime.close()


def test_failing_reviewed_test_cannot_produce_passing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path, failing=True)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    with pytest.raises(ValueError, match="reviewed tests failed"):
        anyio.run(action.run_tests, repo, NOW)
    assert not (repo / ".intent-ci/test-results.json").exists()


def test_ci_restore_requires_real_trust_even_for_locally_onboarded_state(tmp_path: Path) -> None:
    action = _action()
    repo = init_repository(tmp_path / "project")
    ready_project(repo)
    with pytest.raises(ValueError, match="shared state unavailable"):
        action.restore(repo)


@pytest.mark.parametrize("commands", [[], [["tools/test-runner"], ["tools/test-runner", "fail"]]])
def test_all_reviewed_commands_must_pass_before_staging_ci_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commands: list[list[str]],
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path, commands=commands)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    with pytest.raises(ValueError, match="reviewed tests failed"):
        anyio.run(action.run_tests, repo, NOW)
    assert not (repo / ".intent-ci/reviewed-tests.json").exists()


def test_canonical_result_step_rejects_a_different_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    anyio.run(action.run_tests, repo, NOW)
    (repo / "feature.py").write_text("def enabled():\n    return False\n", encoding="utf-8")
    git(repo, "add", "feature.py")
    git(repo, "commit", "-qm", "change after reviewed tests")
    with pytest.raises(ValueError, match="invalid test result binding"):
        action.write_results(repo, NOW)
    assert not (repo / ".intent-ci/test-results.json").exists()


def test_workflow_module_failure_is_nonzero_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    action = _action()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["github_action", "restore"])
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", "PRIVATE-MALFORMED-TRUST")
    assert action.main() == 1
    assert capsys.readouterr() == ("", "Intent workflow step failed\n")


def test_failed_rerun_invalidates_earlier_passing_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    (repo / "tools/test-runner").write_text(
        "#!/usr/bin/python3\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="clean commit snapshot unavailable"):
        anyio.run(action.run_tests, repo, NOW)
    assert not (repo / ".intent-ci/reviewed-tests.json").exists()
    assert not (repo / ".intent-ci/test-results.json").exists()


@pytest.mark.parametrize(
    "kind", ["unstaged", "staged", "assume-unchanged", "ignored-untracked", "replace-object"]
)
def test_reviewed_tests_reject_code_that_does_not_match_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    if kind == "ignored-untracked":
        (repo / ".gitignore").write_text("*.py\n.intent/\n.intent-ci/\n", encoding="utf-8")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "ignore generated Python")
        (repo / "unreviewed.py").write_text("value = True\n", encoding="utf-8")
    else:
        (repo / "feature.py").write_text("def enabled():\n    return False\n", encoding="utf-8")
        git(repo, "add", "feature.py")
        git(repo, "commit", "-qm", "failing committed behavior")
        original_head = git(repo, "rev-parse", "HEAD").decode().strip()
        if kind == "assume-unchanged":
            git(repo, "update-index", "--assume-unchanged", "feature.py")
        (repo / "feature.py").write_text("def enabled():\n    return True\n", encoding="utf-8")
        if kind == "staged":
            git(repo, "add", "feature.py")
        if kind == "replace-object":
            git(repo, "add", "feature.py")
            git(repo, "commit", "-qm", "replacement passing behavior")
            replacement = git(repo, "rev-parse", "HEAD").decode().strip()
            git(repo, "update-ref", "HEAD", original_head)
            git(repo, "replace", original_head, replacement)
    with pytest.raises(ValueError):
        anyio.run(action.run_tests, repo, NOW)
    assert not (repo / ".intent-ci/reviewed-tests.json").exists()


@pytest.mark.parametrize("restore_original", [False, True])
def test_reviewed_test_cannot_change_code_during_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_original: bool,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    runner = repo / "tools/test-runner"
    runner.write_text(
        "#!/usr/bin/python3\nfrom pathlib import Path\np = Path('feature.py')\n"
        "original = p.read_text()\np.write_text('def enabled():\\n    return False\\n')\n"
        + ("p.write_text(original)\n" if restore_original else ""),
        encoding="utf-8",
    )
    git(repo, "add", "tools/test-runner")
    git(repo, "commit", "-qm", "runner mutates code")
    with pytest.raises(ValueError):
        anyio.run(action.run_tests, repo, NOW)
    assert not (repo / ".intent-ci/reviewed-tests.json").exists()


@pytest.mark.parametrize("stage", ["after-tests", "staged-write", "final-write"])
def test_code_changes_at_result_boundaries_cannot_publish_passing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    original_write = action._write

    def change_code(root, path, content):
        original_write(root, path, content)
        (repo / "feature.py").write_text("def enabled():\n    return False\n", encoding="utf-8")

    if stage == "staged-write":
        monkeypatch.setattr(action, "_write", change_code)
        with pytest.raises(ValueError):
            anyio.run(action.run_tests, repo, NOW)
        assert not (repo / ".intent-ci/reviewed-tests.json").exists()
    else:
        anyio.run(action.run_tests, repo, NOW)
        if stage == "after-tests":
            (repo / "feature.py").write_text("def enabled():\n    return False\n", encoding="utf-8")
        else:
            monkeypatch.setattr(action, "_write", change_code)
        with pytest.raises(ValueError):
            action.write_results(repo, NOW)
    assert not (repo / ".intent-ci/test-results.json").exists()


def test_scheduled_capture_uploads_a_report_before_failing_for_pending_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, trust = _clone(tmp_path, case_type="CONFLICTING_SOURCES")
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action = _action()
    action.restore(repo)
    monkeypatch.chdir(repo)
    (repo / "new-source.md").write_text("A newly captured source version\n", encoding="utf-8")
    workflow = yaml.safe_load(
        (Path(__file__).parents[2] / ".github/workflows/intent-sync.yml").read_text()
    )
    uploaded = False
    for step in workflow["jobs"]["drift"]["steps"][4:]:
        if "uses" in step:
            assert "upload-artifact" in step["uses"]
            report = (repo / step["with"]["path"]).read_text()
            assert "CONFLICTING" in report
            uploaded = True
            continue
        arguments = shlex.split(step["run"])[1:]
        if "--sources" in arguments:
            # GitHub connector behavior is separately covered by its offline API fixtures.
            arguments[arguments.index("--sources") + 1] = "markdown,git"
        result = CliRunner().invoke(app, arguments)
        assert result.exit_code == (4 if uploaded else 0), result.stdout
    assert uploaded
    runtime = load_runtime(repo)
    try:
        assert any(record.source_locator == "new-source.md" for record in runtime.evidence())
    finally:
        runtime.close()


def test_clean_commit_boundary_allows_only_declared_untracked_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path, result_paths=["reports/reviewed.json"])
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    (repo / ".venv").mkdir()
    (repo / ".venv/dependency").write_text("installed dependency", encoding="utf-8")
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    assert (repo / "reports/reviewed.json").is_file()
    assert (repo / ".intent-ci/test-results.json").is_file()


def test_approved_output_path_cannot_exempt_tracked_code_from_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path, result_paths=["feature.py"])
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    with pytest.raises(ValueError, match="clean commit snapshot"):
        anyio.run(action.run_tests, repo, NOW)
    assert not (repo / ".intent-ci/reviewed-tests.json").exists()


@pytest.mark.parametrize("attack", ["symlink", "byte-bound"])
def test_commit_snapshot_rejects_unsafe_or_oversized_tracked_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    if attack == "symlink":
        outside = tmp_path / "outside.py"
        outside.write_bytes((repo / "feature.py").read_bytes())
        (repo / "feature.py").unlink()
        (repo / "feature.py").symlink_to(outside)
    else:
        monkeypatch.setattr(
            "intent_engineering.intent_workflow.dev_observer.MAX_COMMIT_SNAPSHOT_BYTES", 1
        )
    with pytest.raises((ValueError, OSError)):
        anyio.run(action.run_tests, repo, NOW)
    assert not (repo / ".intent-ci/reviewed-tests.json").exists()


@pytest.mark.parametrize("failure", ["capture", "render"])
def test_scheduled_pipeline_does_not_mask_operational_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repo, trust = _clone(tmp_path, case_type="CONFLICTING_SOURCES")
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    _action().restore(repo)
    monkeypatch.chdir(repo)
    if failure == "capture":
        (repo / ".git").rename(repo / "git-metadata")
    else:
        (repo / "intent-drift.md").symlink_to(tmp_path / "outside.md")
    workflow = yaml.safe_load(
        (Path(__file__).parents[2] / ".github/workflows/intent-sync.yml").read_text()
    )
    for step in workflow["jobs"]["drift"]["steps"][4:]:
        assert "uses" not in step, "an operational failure must stop before upload"
        arguments = shlex.split(step["run"])[1:]
        if "--sources" in arguments:
            arguments[arguments.index("--sources") + 1] = "markdown,git"
        result = CliRunner().invoke(app, arguments)
        if result.exit_code:
            assert result.exit_code == (3 if failure == "capture" else 1)
            break
    else:
        pytest.fail("the operational failure was masked")


@pytest.mark.parametrize("cache_kind", ["untracked", "tracked", "tracked-casefold"])
def test_repository_bytecode_cannot_turn_failing_committed_source_into_green_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cache_kind: str,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    (repo / "feature.py").write_text("def enabled():\n    return False\n", encoding="utf-8")
    (repo / "tools/test-runner").write_text(
        "#!/usr/bin/python3\nimport feature\nassert feature.enabled()\n", encoding="utf-8"
    )
    git(repo, "add", "feature.py", "tools/test-runner")
    git(repo, "commit", "-qm", "test committed failing Python source")
    # Construct valid interpreter-native bytecode with the source's current size and mtime.
    # The code object deliberately disagrees with the committed source it will shadow.
    subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            (
                "import importlib.util, importlib._bootstrap_external as b; from pathlib import Path; "
                "p=Path('feature.py'); s=p.stat(); "
                "c=compile('def enabled():\\n    return True\\n', str(p), 'exec'); "
                "out=Path(importlib.util.cache_from_source(str(p))); out.parent.mkdir(); "
                "out.write_bytes(b._code_to_timestamp_pyc(c, int(s.st_mtime), s.st_size))"
            ),
        ],
        cwd=repo,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        check=True,
    )
    cache = next((repo / "__pycache__").glob("*.pyc"))
    original_cache = cache.read_bytes()
    if cache_kind == "tracked-casefold":
        cache.rename(cache.with_suffix(".PYC"))
        cache.parent.rename(repo / "__PYCACHE__")
        cache = repo / "__PYCACHE__" / cache.with_suffix(".PYC").name
    if cache_kind != "untracked":
        git(repo, "add", "-f", str(cache.relative_to(repo)))
        git(repo, "commit", "-qm", "commit stale bytecode beside source")
    try:
        anyio.run(action.run_tests, repo, NOW)
        action.write_results(repo, NOW)
    except ValueError:
        pass
    result = _check(repo, trust)
    assert result.exit_code != 0, "cached passing code received complete CI acceptance"
    assert not (repo / ".intent-ci/test-results.json").exists()
    assert cache.read_bytes() == original_cache, "rejected user caches must remain untouched"


@pytest.mark.parametrize("package", ["package", "package/nested"])
def test_restored_parent_directory_cannot_turn_failing_committed_source_into_green_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package: str,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    (repo / package).mkdir(parents=True)
    module = repo / package / "feature.py"
    module.write_text("def enabled():\n    return False\n", encoding="utf-8")
    (repo / "tools/test-runner").write_text(
        "#!/usr/bin/python3\nimport os,runpy,shutil\nfrom pathlib import Path\n"
        f"package = {package!r}\n"
        "os.rename(package, '.intent-ci/original-package')\n"
        "try:\n"
        "    Path(package).mkdir()\n"
        "    (Path(package) / 'feature.py').write_text('def enabled():\\n    return True\\n')\n"
        "    assert runpy.run_path(package + '/feature.py')['enabled']()\n"
        "finally:\n"
        "    shutil.rmtree(package)\n"
        "    os.rename('.intent-ci/original-package', package)\n",
        encoding="utf-8",
    )
    git(repo, "add", str(module.relative_to(repo)), "tools/test-runner")
    git(repo, "commit", "-qm", "test restored parent directory substitution")
    before = module.stat()
    try:
        anyio.run(action.run_tests, repo, NOW)
        action.write_results(repo, NOW)
    except ValueError:
        pass
    after = module.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    result = _check(repo, trust)
    assert result.exit_code != 0, "restored parent substitution received complete CI acceptance"
    assert not (repo / ".intent-ci/test-results.json").exists()


@pytest.mark.parametrize("interpreter", ["python", "shell"])
def test_clean_python_imports_pass_ci_without_creating_repository_bytecode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interpreter: str,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    runner = (
        "#!/usr/bin/python3\nimport feature\nassert feature.enabled()\n"
        if interpreter == "python"
        else "#!/bin/sh\nexec /usr/bin/python3 -c 'import feature; assert feature.enabled()'\n"
    )
    (repo / "tools/test-runner").write_text(runner, encoding="utf-8")
    git(repo, "add", "tools/test-runner")
    git(repo, "commit", "-qm", "verify ordinary clean Python imports")
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    assert not (repo / "__pycache__").exists()
    result = _check(repo, trust, ci=False)
    assert result.exit_code == 0, (result.stdout, repr(result.exception))


@pytest.mark.parametrize("package", [".", "package/nested"])
@pytest.mark.parametrize("cache_name", ["__pycache__", "__PYCACHE__"])
def test_empty_cache_directory_cannot_hide_temporary_passing_bytecode_from_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    cache_name: str,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    parent = repo / package
    parent.mkdir(parents=True, exist_ok=True)
    module = parent / "feature.py"
    module.write_text("def enabled():\n    return False\n", encoding="utf-8")
    (repo / "tools/test-runner").write_text(
        "#!/usr/bin/python3\nimport importlib.util, importlib._bootstrap_external as b\n"
        "from pathlib import Path\n"
        f"p=Path({str(module.relative_to(repo))!r})\n"
        "s=p.stat()\n"
        "code=compile('def enabled():\\n    return True\\n', str(p), 'exec')\n"
        "out=Path(importlib.util.cache_from_source(str(p)))\n"
        "out.write_bytes(b._code_to_timestamp_pyc(code, int(s.st_mtime), s.st_size))\n"
        "try:\n"
        "    spec=importlib.util.spec_from_file_location('observed_feature', str(p))\n"
        "    observed=importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(observed)\n"
        "    assert observed.enabled()\n"
        "finally:\n"
        "    out.unlink()\n",
        encoding="utf-8",
    )
    git(repo, "add", str(module.relative_to(repo)), "tools/test-runner")
    git(repo, "commit", "-qm", "test transient bytecode inside an existing empty cache")
    cache = parent / cache_name
    cache.mkdir()
    before = module.stat()
    try:
        anyio.run(action.run_tests, repo, NOW)
        action.write_results(repo, NOW)
    except ValueError:
        pass
    after = module.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    assert cache.is_dir() and not tuple(cache.iterdir())
    result = _check(repo, trust)
    assert result.exit_code != 0, "transient bytecode received complete CI acceptance"
    assert not (repo / ".intent-ci/test-results.json").exists()


def test_empty_cache_under_untracked_nested_directories_is_rejected_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    cache = repo / "untracked/empty/nested/__PYCACHE__"
    cache.mkdir(parents=True)
    before = cache.stat()
    with pytest.raises(ValueError):
        anyio.run(action.run_tests, repo, NOW)
    after = cache.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    assert not tuple(cache.iterdir())


def test_cache_directories_in_explicit_generated_dependency_and_external_scopes_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    caches = (
        repo / ".venv/lib/package/__pycache__",
        repo / ".intent-ci/generated/__pycache__",
        tmp_path / "external/__pycache__",
    )
    for cache in caches:
        cache.mkdir(parents=True)
        (cache / "owned.pyc").write_bytes(b"preserved scope")
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    assert _check(repo, trust, ci=False).exit_code == 0
    assert all((cache / "owned.pyc").read_bytes() == b"preserved scope" for cache in caches)


def test_cache_directory_inspection_fails_closed_at_entry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    action.restore(repo)
    monkeypatch.setattr(
        "intent_engineering.intent_workflow.dev_observer.MAX_SNAPSHOT_TREE_ENTRIES",
        1,
        raising=False,
    )
    with pytest.raises(ValueError):
        anyio.run(action.run_tests, repo, NOW)
    assert not (repo / ".intent-ci/test-results.json").exists()
