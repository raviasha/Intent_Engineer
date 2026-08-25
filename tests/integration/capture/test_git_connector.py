"""Integration coverage for deterministic local Git evidence capture."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intent_engineering.capture.base import ConnectorError
from intent_engineering.capture.checkpoints import checkpoint_after_discovery
from intent_engineering.capture.git.connector import GitConnector
from intent_engineering.core.models import SyncCheckpoint
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


@pytest.mark.anyio
async def test_git_checkpoint_keeps_prior_sha_after_a_noop_incremental_discovery(tmp_path: Path) -> None:
    """An empty incremental Git pass must not erase the durable cursor."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    sha = commit(repo, "README.md", "# Intent\n", "Add intent", "First body")
    connector = GitConnector(repo)
    prior = SyncCheckpoint(connector_id="git", cursor=sha, committed_at=datetime(2026, 8, 25, tzinfo=UTC))

    discovered = await connector.discover(cursor=prior.cursor)
    checkpoint = checkpoint_after_discovery(
        connector,
        discovered,
        datetime(2026, 8, 26, tzinfo=UTC),
        prior=prior,
    )

    assert discovered == ()
    assert checkpoint.cursor == sha


@pytest.mark.anyio
async def test_git_connector_treats_an_unborn_head_as_empty_discovery(tmp_path: Path) -> None:
    """A newly initialized repository has no evidence yet, not a connector failure."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")

    assert await GitConnector(repo).discover(cursor=None) == ()


@pytest.mark.anyio
async def test_git_connector_deduplicates_merge_paths_with_unusual_names(tmp_path: Path) -> None:
    """Per-parent merge collection must retain every changed path without quoting loss."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    commit(repo, "base.md", "base\n", "Base", "Base body")
    default_branch = git(repo, "branch", "--show-current")
    git(repo, "checkout", "-b", "feature")
    feature_path = "feature\nname.md"
    commit(repo, feature_path, "feature\n", "Feature", "Feature body")
    git(repo, "checkout", default_branch)
    main_path = "main\tname.md"
    commit(repo, main_path, "main\n", "Main", "Main body")
    git(repo, "merge", "--no-ff", "feature", "-m", "Merge feature")
    merge_sha = git(repo, "rev-parse", "HEAD")
    connector = GitConnector(repo)

    evidence = connector.normalize(await connector.fetch(f"commit:{merge_sha}", merge_sha))

    assert len(evidence.payload["parents"]) == 2
    assert evidence.payload["changed_paths"] == (feature_path, main_path)


@pytest.mark.anyio
async def test_git_connector_wraps_operational_discovery_and_fetch_failures(tmp_path: Path) -> None:
    """Git command failures must become redacted connector-boundary errors."""
    with pytest.raises(ConnectorError, match="Git discovery failed") as discovery_error:
        await GitConnector(tmp_path / "not-a-repository").discover(cursor=None)
    assert "fatal:" not in str(discovery_error.value)

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    commit(repo, "README.md", "# Intent\n", "Add intent", "First body")
    with pytest.raises(ConnectorError, match="Git fetch failed") as fetch_error:
        await GitConnector(repo).fetch("commit:deadbeef", "deadbeef")
    assert "fatal:" not in str(fetch_error.value)
