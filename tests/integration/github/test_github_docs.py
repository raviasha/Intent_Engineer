"""Executable contracts for the public local GitHub integration guide."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.github.auth import CredentialSource, GitHubCredentials

ROOT = Path(__file__).parents[3]


def test_documented_intent_commands_expose_their_real_help() -> None:
    """Removing or renaming a documented CLI surface makes this release guide stale."""
    for arguments in (
        ("init", "--help"),
        ("validate", "--help"),
        ("doctor", "github", "--help"),
        ("sync", "--help"),
        ("drift", "--help"),
    ):
        completed = subprocess.run(
            [str(Path(sys.executable).with_name("intent")), *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr
        assert "Usage:" in completed.stdout


def test_documented_credential_precedence_matches_the_real_resolver() -> None:
    """A blank environment value must defer to the existing GitHub CLI credential."""
    calls: list[list[str]] = []

    def token_runner(arguments: list[str]) -> str:
        calls.append(arguments)
        return "cli-token"

    environment = GitHubCredentials.resolve({"GH_TOKEN": "env-token"}, token_runner)
    fallback = GitHubCredentials.resolve({"GH_TOKEN": "  "}, token_runner)

    assert environment.source is CredentialSource.ENVIRONMENT
    assert fallback.source is CredentialSource.GITHUB_CLI
    assert calls == [["gh", "auth", "token"]]


def test_github_guide_keeps_release_boundaries_and_action_contract_visible() -> None:
    """Removing a safety boundary would make the executable read-only release misleading."""
    guide = (ROOT / "docs/github.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    workflow_text = (ROOT / ".github/workflows/intent-sync.yml").read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)

    for value in (
        "GITHUB_REPOSITORY=owner/repository",
        "GH_TOKEN",
        "gh auth login",
        "intent doctor github --project . --format json",
        "intent sync --project . --sources markdown,git,github --format json",
        "intent drift --project . --format markdown --output intent-drift.md",
        "intent drift --project . --format markdown --require-review",
        ".github/workflows/intent-sync.yml",
        "GitHub App installation",
        "webhooks",
        "MCP write-back",
        "cron `17 2 * * *`",
        "contents: read",
        "issues: read",
        "pull-requests: read",
        "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
        "GITHUB_REPOSITORY: ${{ github.repository }}",
        "`intent-drift` artifact",
        "GitHub-only failed sync exits with",
        "partial result and exit with code 3",
    ):
        assert value in guide
    assert "GitHub ingestion is opt-in" in readme
    assert set(workflow["on"]) == {"workflow_dispatch", "schedule"}
    assert workflow["on"]["schedule"] == [{"cron": "17 2 * * *"}]
    job = workflow["jobs"]["drift"]
    assert job["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    steps = job["steps"]
    assert [step["run"] for step in steps if "run" in step] == [
        "python -m pip install .",
        "intent init",
        "intent validate",
        "intent sync --sources markdown,git,github",
        "intent drift --format markdown --output intent-drift.md",
    ]
    assert steps[5]["env"] == {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "GITHUB_REPOSITORY": "${{ github.repository }}",
    }
    assert steps[7]["with"] == {"name": "intent-drift", "path": "intent-drift.md"}
