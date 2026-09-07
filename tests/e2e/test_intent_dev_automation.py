"""Offline release proof through real Git, signed state, reviewed tests and the CLI."""

from __future__ import annotations

import importlib
import json
import shlex
from pathlib import Path
from unittest.mock import patch

import anyio
import pytest
import structlog
import yaml  # type: ignore[import-untyped]
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from typer.testing import CliRunner

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
from intent_engineering.team_state.restore import (
    SharedStateTrust,
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


def _check(repo: Path, trust: SharedStateTrust):
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
                "--ci",
                "--require-review",
                "--test-results",
                ".intent-ci/test-results.json",
            ],
            env={"INTENT_CI_SHARED_STATE_TRUST": trust_environment(trust)},
        )


def test_fresh_onboarded_clone_first_prompt_and_plugin_disabled_ci_backstop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing restore, reviewed execution, canonical binding or CLI assurance breaks this."""
    action = _action()
    repo, trust = _clone(tmp_path)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    original_head = git(repo, "rev-parse", "HEAD")
    action.restore(repo)
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
    assert git(repo, "rev-parse", "HEAD") == original_head
    # The GitHub path has no plugin invocation and restores from the signed ref again.
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    artifact = json.loads((repo / ".intent-ci/test-results.json").read_bytes())
    assert artifact["commit_sha"] == original_head.decode().strip()
    assert artifact["status"] == "passed"
    result = _check(repo, trust)
    assert result.exit_code == 0, (result.stdout, result.stderr, repr(result.exception))
    assert json.loads(result.stdout)["reason"] == "checks_passed"
    assert not (repo / "plugins").exists()
    assert git(repo, "rev-parse", "HEAD") == original_head


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
