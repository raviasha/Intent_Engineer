"""Offline structural contract for the read-only GitHub drift workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from intent_engineering.cli.app import app


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
        "uses": "actions/setup-python@v5",
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
        "uses": "actions/upload-artifact@v4",
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
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["on"]["pull_request"] == ""
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "intent-check-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "true",
    }
    job = workflow["jobs"]["check"]
    assert job["name"] == "Intent Engineering / check"
    assert job["environment"] == "intent-ci"
    assert job["timeout-minutes"] == "20"
    assert job["runs-on"] == "ubuntu-24.04"
    steps = job["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert steps[0]["with"] == {"fetch-depth": "0", "persist-credentials": "false"}
    assert steps[1]["uses"].startswith("actions/setup-python@")
    assert steps[2]["run"] == "python -I -m pip install '.[dev]'"
    assert [step.get("run") for step in steps[3:5]] == [
        "python -I -m intent_engineering.integrations.github_action restore",
        "python -I -m intent_engineering.integrations.immutable_ci",
    ]
    for index in (3, 4):
        assert steps[index]["env"] == {
            "INTENT_CI_SHARED_STATE_TRUST": "${{ secrets.INTENT_CI_SHARED_STATE_TRUST }}",
        }
    assert steps[5]["uses"].startswith("actions/upload-artifact@")
    assert steps[5]["with"] == {
        "name": "intent-test-results",
        "path": ".intent-ci/test-results.json",
        "retention-days": "7",
        "if-no-files-found": "error",
        "include-hidden-files": "true",
    }
    for forbidden in (
        "continue-on-error",
        "|| true",
        "always()",
        "pull_request_target",
        "intent init",
        "intent-advisor",
        "plugins/",
        "intent-state:",
        "write-all",
    ):
        assert forbidden not in text
    assert all("if" not in step for step in steps)


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
