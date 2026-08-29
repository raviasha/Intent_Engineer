"""Offline structural contract for the read-only GitHub drift workflow."""

from __future__ import annotations

import json
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from intent_engineering.cli.app import app


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
    assert steps[0] == {"uses": "actions/checkout@v4", "with": {"fetch-depth": "0"}}
    assert steps[1] == {
        "uses": "actions/setup-python@v5",
        "with": {"python-version": "3.12"},
    }
    assert steps[2]["run"] == "python -m pip install ."
    assert steps[3] == {
        "name": "Require restored approved intent baseline",
        "run": "intent status --project . --format json --require-baseline",
    }
    assert steps[4]["run"] == "intent validate --project ."
    assert steps[5]["run"] == "intent sync --project . --sources markdown,git,github"
    assert steps[5]["env"] == {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "GITHUB_REPOSITORY": "${{ github.repository }}",
    }
    assert steps[6]["run"] == (
        "intent drift --project . --format markdown --output intent-drift.md"
    )
    assert steps[7] == {
        "uses": "actions/upload-artifact@v4",
        "with": {"name": "intent-drift", "path": "intent-drift.md"},
    }
    assert "continue-on-error" not in text
    assert "permissions: write" not in text
    assert "|| true" not in text
    assert "; intent" not in text
    assert "intent init" not in text


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
