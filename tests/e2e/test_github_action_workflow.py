"""Parse the shipped Actions contracts at the GitHub execution boundary."""

import json
import re
from pathlib import Path

import pytest
import structlog
import yaml
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.cli.team import build_github_enable_preview
from intent_engineering.integrations.workflows import check_workflow
from tests.e2e.test_cli_team_github import _project

ROOT = Path(__file__).parents[2]


def _workflow(name):
    path = ROOT / ".github/workflows" / name
    assert path.is_file(), "required workflow must ship in the repository"
    return yaml.safe_load(path.read_text())


def test_state_workflow_is_the_exact_reviewed_setup_suggestion(tmp_path):
    preview = build_github_enable_preview(_project(tmp_path), "acme/project")
    path = ROOT / ".github/workflows/intent-state.yml"
    assert path.is_file(), "setup's required state workflow must also be checked in"
    assert path.read_bytes() == preview.workflow_suggestion.encode()


def test_state_workflow_routes_only_state_prs_to_the_restricted_runner():
    workflow = _workflow("intent-state.yml")
    assert workflow["on"] == {"pull_request_target": {"branches": ["intent-state"]}}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "intent-state-${{ github.event.pull_request.number }}",
        "cancel-in-progress": True,
    }
    assert set(workflow["jobs"]) == {"state"}
    job = workflow["jobs"]["state"]
    assert job["name"] == "Intent Engineering / state"
    assert job["runs-on"] == {"group": "intent-state", "labels": ["self-hosted", "intent-state"]}
    assert job["environment"] == "intent-ci"
    assert 1 <= job["timeout-minutes"] <= 10
    steps = job["steps"]
    checkout = steps[0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "ref": "${{ github.workflow_sha }}",
        "path": ".intent-trusted",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    assert steps[-2]["run"] == "python -I .intent-trusted/ci/launch.py fetch-state"
    assert steps[-2]["env"] == {
        "INTENT_CI_TOOLING_SHA": "${{ github.workflow_sha }}",
        "GH_TOKEN": "${{ github.token }}",
    }
    assert steps[-1]["run"] == "python -I .intent-trusted/ci/launch.py validate-state"
    assert steps[-1]["env"] == {"INTENT_CI_TOOLING_SHA": "${{ github.workflow_sha }}"}


def test_code_workflow_excludes_state_and_uses_protected_restore_then_check():
    workflow = _workflow("intent-check.yml")
    assert workflow["on"] == {
        "pull_request_target": {"branches-ignore": ["intent-state"]},
        "workflow_dispatch": None,
    }
    assert workflow["concurrency"] == {
        "group": "intent-check-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": True,
    }
    assert set(workflow["jobs"]) == {"check"}
    job = workflow["jobs"]["check"]
    assert job["name"] == "Intent Engineering / check"
    assert job["runs-on"] == {"group": "intent-state", "labels": ["self-hosted", "intent-state"]}
    assert job["environment"] == "intent-ci"
    assert 1 <= job["timeout-minutes"] <= 20
    steps = job["steps"]
    assert steps[0]["with"] == {
        "ref": "${{ github.workflow_sha }}",
        "path": ".intent-trusted",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    assert steps[-3]["run"] == "exec python -I .intent-trusted/ci/launch.py fetch"
    assert steps[-3]["env"] == {
        "INTENT_CI_TOOLING_SHA": "${{ github.workflow_sha }}",
        "GH_TOKEN": "${{ github.token }}",
    }
    # This protected entry point restores before executing in disposable containers;
    # its execution-order regression lives alongside the immutable CI runtime tests.
    assert steps[-2]["run"] == "exec python -I .intent-trusted/ci/launch.py check"
    assert steps[-2]["env"] == {"INTENT_CI_TOOLING_SHA": "${{ github.workflow_sha }}"}
    assert steps[-1] == {
        "name": "Remove owned immutable CI containers and images",
        "if": "always()",
        "run": "exec python -I .intent-trusted/ci/launch.py cleanup",
        "env": {"INTENT_CI_TOOLING_SHA": "${{ github.workflow_sha }}"},
    }


def test_code_workflow_is_the_packaged_provider_verification_contract():
    assert (ROOT / ".github/workflows/intent-check.yml").read_bytes() == check_workflow().encode()


def test_setup_stages_both_workflows_under_one_exact_review(tmp_path):
    from intent_engineering.team_state.suggestions import stage_code_suggestions
    from tests.helpers.shared_state import git

    root = _project(tmp_path)
    git(root, "init", "--initial-branch=main")
    git(root, "add", "-f", ".intent/config.yaml")
    git(root, "commit", "-qm", "code")
    preview = build_github_enable_preview(root, "acme/project")
    assert preview.check_workflow_suggestion == check_workflow()
    assert preview.check_workflow_path == ".github/workflows/intent-check.yml"
    assert preview.code_suggestions.check_workflow_preimage is None
    stage_code_suggestions(root, preview.code_suggestions)
    assert (root / preview.workflow_path).read_text() == preview.workflow_suggestion
    assert (root / preview.check_workflow_path).read_text() == preview.check_workflow_suggestion
    assert (root / preview.codeowners_path).read_text() == preview.codeowners_suggestion


def test_code_workflow_drift_invalidates_setup_before_any_write(tmp_path):
    from intent_engineering.team_state.suggestions import (
        CodeSuggestionError,
        stage_code_suggestions,
    )
    from tests.helpers.shared_state import git

    root = _project(tmp_path)
    git(root, "init", "--initial-branch=main")
    git(root, "add", "-f", ".intent/config.yaml")
    git(root, "commit", "-qm", "code")
    preview = build_github_enable_preview(root, "acme/project")
    target = root / ".github/workflows/intent-check.yml"
    target.parent.mkdir(parents=True)
    target.write_text("unreviewed workflow")
    changed = build_github_enable_preview(root, "acme/project")
    assert changed.preview_digest != preview.preview_digest
    with pytest.raises(CodeSuggestionError):
        stage_code_suggestions(root, preview.code_suggestions)
    assert target.read_text() == "unreviewed workflow"
    assert not (root / ".github/CODEOWNERS").exists()
    assert not (root / ".github/workflows/intent-state.yml").exists()


@pytest.mark.parametrize("name", ["intent-state.yml", "intent-check.yml"])
def test_required_workflows_use_only_pinned_read_only_trusted_actions(name):
    workflow = _workflow(name)
    assert set(workflow) == {"name", "on", "permissions", "concurrency", "jobs"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "env" not in workflow
    for job in workflow["jobs"].values():
        assert set(job) == {"name", "runs-on", "environment", "timeout-minutes", "steps"}
        assert len(job["steps"]) == (6 if name == "intent-check.yml" else 5)
        assert "permissions" not in job
        assert "env" not in job
        for step in job["steps"]:
            assert set(step) <= {"name", "uses", "with", "run", "env", "if"}
            assert ("uses" in step) != ("run" in step)
            if "uses" in step:
                assert re.fullmatch(r"actions/(checkout|setup-python)@[a-f0-9]{40}", step["uses"])
            if "run" in step:
                assert step["run"] in {
                    "python -I .intent-trusted/ci/launch.py fetch",
                    "python -I .intent-trusted/ci/launch.py check",
                    "python -I .intent-trusted/ci/launch.py fetch-state",
                    "python -I .intent-trusted/ci/launch.py validate-state",
                    "exec python -I .intent-trusted/ci/launch.py fetch",
                    "exec python -I .intent-trusted/ci/launch.py check",
                    "exec python -I .intent-trusted/ci/launch.py cleanup",
                    (
                        "python -I -m pip install --require-hashes --only-binary=:all: "
                        "-r .intent-trusted/src/intent_engineering/integrations/ci-runtime.lock"
                    ),
                }
            assert set(step.get("env", {})) <= {"INTENT_CI_TOOLING_SHA", "GH_TOKEN"}


# Resolved from the official actions repositories on 2026-09-08; changes require review.
_REVIEWED_ACTION_REFS = {
    "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",  # v4.4.0
    "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",  # v5.6.0
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",  # v4.6.2
}


def _workflow_action_refs() -> list[tuple[str, str, str]]:
    """Cover every workflow, including future reusable jobs and adjacent action steps."""
    references = []
    directory = Path(__file__).parents[2] / ".github/workflows"
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for name, job in workflow["jobs"].items():
            if "uses" in job:
                references.append((path.name, name, job["uses"]))
            for index, step in enumerate(job.get("steps", [])):
                if "uses" in step:
                    references.append((path.name, f"{name}.steps[{index}]", step["uses"]))
    return references


@pytest.mark.parametrize(("workflow", "location", "reference"), _workflow_action_refs())
def test_every_workflow_action_uses_a_reviewed_full_commit_sha(
    workflow: str, location: str, reference: str
) -> None:
    """A movable tag or an unreviewed action cannot supply executable CI tooling."""
    assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference), (
        f"{workflow}:{location} must use a full immutable commit SHA, got {reference}"
    )
    assert reference in _REVIEWED_ACTION_REFS, (
        f"{workflow}:{location} uses an unreviewed action commit: {reference}"
    )


@pytest.fixture(autouse=True)
def _reset_cli_logging(request: pytest.FixtureRequest) -> None:
    request.addfinalizer(structlog.reset_defaults)


def test_intent_sync_workflow_is_nightly_manual_read_only_and_ordered() -> None:
    """A workflow edit cannot gain writes, mask failures, or invent a clean baseline."""
    path = Path(__file__).parents[2] / ".github/workflows/intent-sync.yml"
    text = path.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"workflow_dispatch", "schedule"}
    assert workflow["on"]["workflow_dispatch"] == ""
    assert workflow["on"]["schedule"] == [{"cron": "17 2 * * *"}]
    job = workflow["jobs"]["drift"]
    assert job["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    assert all(value != "write" for value in job["permissions"].values())
    steps = job["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert steps[0]["with"] == {"fetch-depth": "0", "persist-credentials": "false"}
    assert steps[1] == {
        "uses": "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
        "with": {"python-version": "3.12"},
    }
    assert steps[2]["run"] == "python -m pip install ."
    assert steps[3]["run"] == "python -m intent_engineering.integrations.github_action restore"
    assert steps[4]["run"] == "intent sync --project . --sources markdown,git,github"
    assert steps[4]["env"] == {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "GITHUB_REPOSITORY": "${{ github.repository }}",
    }
    assert steps[5]["run"] == "intent validate --project ."
    assert steps[6]["run"] == (
        "intent drift --project . --format markdown --output intent-drift.md"
    )
    assert steps[7] == {
        "uses": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "with": {
            "name": "intent-drift",
            "path": "intent-drift.md",
            "retention-days": "7",
            "if-no-files-found": "error",
        },
    }
    assert steps[8]["run"] == "intent check --require-review"
    assert "continue-on-error" not in text
    assert "permissions: write" not in text
    assert "|| true" not in text
    assert "; intent" not in text
    assert "intent init" not in text


def test_required_check_is_independent_ordered_and_fail_closed() -> None:
    """Missing steps, filtered PRs, or masked failures must break the merge backstop."""
    path = Path(__file__).parents[2] / ".github/workflows/intent-check.yml"
    assert path.is_file(), "required Intent Engineering workflow is missing"
    text = path.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert workflow["name"] == "Intent Engineering"
    assert set(workflow["on"]) == {"pull_request_target", "workflow_dispatch"}
    assert workflow["on"]["pull_request_target"] == {"branches-ignore": ["intent-state"]}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "intent-check-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "true",
    }
    job = workflow["jobs"]["check"]
    assert job["name"] == "Intent Engineering / check"
    assert job["environment"] == "intent-ci"
    assert job["timeout-minutes"] == "20"
    assert job["runs-on"] == {"group": "intent-state", "labels": ["self-hosted", "intent-state"]}
    steps = job["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert steps[0]["with"] == {
        "ref": "${{ github.workflow_sha }}",
        "path": ".intent-trusted",
        "fetch-depth": "0",
        "persist-credentials": "false",
    }
    assert steps[1]["uses"].startswith("actions/setup-python@")
    assert steps[2]["run"] == (
        "python -I -m pip install --require-hashes --only-binary=:all: "
        "-r .intent-trusted/src/intent_engineering/integrations/ci-runtime.lock"
    )
    assert [step.get("run") for step in steps[3:6]] == [
        "exec python -I .intent-trusted/ci/launch.py fetch",
        "exec python -I .intent-trusted/ci/launch.py check",
        "exec python -I .intent-trusted/ci/launch.py cleanup",
    ]
    assert steps[3]["env"] == {
        "INTENT_CI_TOOLING_SHA": "${{ github.workflow_sha }}",
        "GH_TOKEN": "${{ github.token }}",
    }
    assert steps[4]["env"] == {
        "INTENT_CI_TOOLING_SHA": "${{ github.workflow_sha }}",
    }
    assert steps[5]["if"] == "always()"
    assert steps[5]["env"] == {
        "INTENT_CI_TOOLING_SHA": "${{ github.workflow_sha }}",
    }
    assert len(steps) == 6
    for forbidden in (
        "continue-on-error",
        "|| true",
        "intent init",
        "intent-advisor",
        "plugins/",
        "intent-state:",
        "write-all",
    ):
        assert forbidden not in text
    assert all("if" not in step for step in steps[:-1])


def test_trust_is_released_only_to_protected_tooling_not_a_pr_checkout() -> None:
    workflow = yaml.load(
        (Path(__file__).parents[2] / ".github/workflows/intent-check.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "pull_request_target" in workflow["on"]
    assert "pull_request" not in workflow["on"]
    steps = workflow["jobs"]["check"]["steps"]
    checkout = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
    assert len(checkout) == 1
    assert checkout[0]["with"]["ref"] == "${{ github.workflow_sha }}"
    assert checkout[0]["with"]["path"] == ".intent-trusted"
    trusted = [step for step in steps if "INTENT_CI_SHARED_STATE_TRUST" in step.get("env", {})]
    assert trusted == []
    assert all("pip install ." not in step.get("run", "") for step in steps)
    assert any("--require-hashes --only-binary=:all:" in step.get("run", "") for step in steps)


def test_assurance_guard_fails_closed_on_clean_checkout_without_creating_state(
    tmp_path: Path,
) -> None:
    project = tmp_path / "clean-checkout"
    project.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "status",
            "--project",
            str(project),
            "--format",
            "json",
            "--require-baseline",
        ],
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout) == {
        "version": "1",
        "status": "onboarding_required",
        "reason": "approved_intent_baseline_required",
    }
    assert result.stderr == ""
    assert not (project / ".intent").exists()
