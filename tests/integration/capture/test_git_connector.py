"""Integration coverage for deterministic local Git evidence capture."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from intent_engineering.capture.git.connector import GitConnector
from tests.contract.capture.test_connector_contract import assert_connector_is_stable


def git(repo: Path, *args: str) -> str:
    """Run Git against the temporary fixture repository."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def commit(repo: Path, path: str, content: str, subject: str, body: str) -> str:
    """Create a locally authored commit with known evidence fields."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", path)
    git(
        repo,
        "-c",
        "user.name=Intent Tester",
        "-c",
        "user.email=intent@example.test",
        "commit",
        "-m",
        subject,
        "-m",
        body,
    )
    return git(repo, "rev-parse", "HEAD")


@pytest.mark.anyio
async def test_git_connector_captures_stable_commit_evidence_without_diffs(tmp_path: Path) -> None:
    """Commit identity, metadata, and incremental discovery must stay deterministic."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    first_sha = commit(repo, "README.md", "# Intent\n", "Add intent", "First body")
    second_sha = commit(repo, "notes/design.md", "# Design\n", "Add design", "Second body")
    connector = GitConnector(repo)

    sources = await assert_connector_is_stable(connector)

    assert [source.external_object_id for source in sources] == [
        f"commit:{first_sha}",
        f"commit:{second_sha}",
    ]
    latest_raw = await connector.fetch(sources[-1].external_object_id, sources[-1].external_version)
    latest = connector.normalize(latest_raw)
    assert latest.author == "Intent Tester <intent@example.test>"
    assert latest.payload["subject"] == "Add design"
    assert latest.payload["body"] == "Second body"
    assert latest.payload["parents"] == (first_sha,)
    assert latest.payload["changed_paths"] == ("notes/design.md",)
    assert "diff" not in latest.payload
    assert connector.next_checkpoint(sources) == second_sha

    assert await connector.discover(cursor=first_sha) == (sources[-1],)
