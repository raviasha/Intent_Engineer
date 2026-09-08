"""Exact, code-branch-only staging for GitHub setup suggestions."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from intent_engineering.storage.secure import UnsafePathError
from intent_engineering.team_state import suggestions
from intent_engineering.team_state.suggestions import (
    CodeSuggestionError,
    preview_code_suggestions,
    stage_code_suggestions,
)

CODEOWNERS = "/.intent/ @acme\n/.github/workflows/intent-state.yml @acme\n"
WORKFLOW = "name: Intent state\non: pull_request\n"
CHECK_WORKFLOW = "name: Intent check\n"


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Tests")
    _git(root, "config", "user.email", "tests@example.invalid")
    (root / "README.md").write_text("project\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    return root


def test_preview_binds_exact_json_safe_contents_preimages_and_code_head(tmp_path: Path) -> None:
    """Catches a handoff omitting the reviewed bytes, preimages, branch, or commit."""
    root = _repository(tmp_path)
    (root / ".github" / "workflows").mkdir(parents=True)
    existing = b"\xffexisting\x00"
    (root / ".github" / "CODEOWNERS").write_bytes(existing)

    preview = preview_code_suggestions(root, CODEOWNERS, WORKFLOW)

    assert preview.branch == "main"
    assert preview.head_commit == _git(root, "rev-parse", "HEAD")
    assert preview.codeowners_content == CODEOWNERS
    assert preview.workflow_content == WORKFLOW
    assert preview.codeowners_preimage == "_2V4aXN0aW5nAA"
    assert preview.workflow_preimage is None
    assert preview.digest.startswith("sha256:")
    assert json.loads(json.dumps(preview.model_dump(mode="json")))["digest"] == preview.digest


def test_stage_writes_three_exact_files_on_the_code_branch(tmp_path: Path) -> None:
    """Catches staging on an unbound path or altering the reviewed UTF-8 bytes."""
    root = _repository(tmp_path)
    preview = preview_code_suggestions(root, CODEOWNERS, WORKFLOW, CHECK_WORKFLOW)

    stage_code_suggestions(root, preview)

    assert (root / ".github" / "CODEOWNERS").read_bytes() == CODEOWNERS.encode()
    assert (root / ".github" / "workflows" / "intent-state.yml").read_bytes() == (WORKFLOW.encode())
    assert (root / ".github/workflows/intent-check.yml").read_bytes() == CHECK_WORKFLOW.encode()
    assert _git(root, "branch", "--show-current") == "main"


def test_compatible_existing_files_are_an_exact_noop(tmp_path: Path) -> None:
    """Catches replacement of already-compatible files or an unnecessary metadata change."""
    root = _repository(tmp_path)
    codeowners = root / ".github" / "CODEOWNERS"
    workflow = root / ".github" / "workflows" / "intent-state.yml"
    check = root / ".github/workflows/intent-check.yml"
    workflow.parent.mkdir(parents=True)
    codeowners.write_text(CODEOWNERS, encoding="utf-8")
    workflow.write_text(WORKFLOW, encoding="utf-8")
    check.write_text(CHECK_WORKFLOW, encoding="utf-8")
    preview = preview_code_suggestions(root, CODEOWNERS, WORKFLOW, CHECK_WORKFLOW)
    before = (
        (codeowners.stat().st_ino, codeowners.stat().st_mtime_ns),
        (workflow.stat().st_ino, workflow.stat().st_mtime_ns),
        (check.stat().st_ino, check.stat().st_mtime_ns),
    )

    stage_code_suggestions(root, preview)

    after = (
        (codeowners.stat().st_ino, codeowners.stat().st_mtime_ns),
        (workflow.stat().st_ino, workflow.stat().st_mtime_ns),
        (check.stat().st_ino, check.stat().st_mtime_ns),
    )
    assert after == before


def test_incompatible_existing_file_rejects_without_partial_write(tmp_path: Path) -> None:
    """Catches overwriting user configuration or writing one suggestion before finding conflict."""
    root = _repository(tmp_path)
    workflow = root / ".github" / "workflows" / "intent-state.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("user workflow\n", encoding="utf-8")
    preview = preview_code_suggestions(root, CODEOWNERS, WORKFLOW)

    with pytest.raises(CodeSuggestionError, match="incompatible code suggestion"):
        stage_code_suggestions(root, preview)

    assert not (root / ".github" / "CODEOWNERS").exists()
    assert workflow.read_text(encoding="utf-8") == "user workflow\n"


def test_absent_target_race_never_overwrites_an_incompatible_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a file created after preflight being replaced by the reviewed suggestion."""
    root = _repository(tmp_path)
    preview = preview_code_suggestions(root, CODEOWNERS, WORKFLOW)
    target = root / ".github" / "CODEOWNERS"
    original_link = suggestions.os.link
    raced = False

    def race_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        nonlocal raced
        if destination == "CODEOWNERS" and not raced:
            raced = True
            target.write_text("user content\n", encoding="utf-8")
        original_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(suggestions.os, "link", race_link)

    with pytest.raises(CodeSuggestionError, match="stale code suggestion preview"):
        stage_code_suggestions(root, preview)

    assert target.read_text(encoding="utf-8") == "user content\n"
    assert not (root / ".github" / "workflows" / "intent-state.yml").exists()


def test_stage_rechecks_code_head_immediately_before_a_target_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the checked-out branch changing after file preflight but before installation."""
    root = _repository(tmp_path)
    preview = preview_code_suggestions(root, CODEOWNERS, WORKFLOW)
    original_code_head = suggestions._code_head
    calls = 0

    def drifting_code_head(project: object) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            return "other", preview.head_commit
        return original_code_head(project)  # type: ignore[arg-type]

    monkeypatch.setattr(suggestions, "_code_head", drifting_code_head)

    with pytest.raises(CodeSuggestionError, match="stale code suggestion preview"):
        stage_code_suggestions(root, preview)

    assert not (root / ".github").exists()


@pytest.mark.parametrize("stale", ["branch", "head", "preimage"])
def test_stage_rejects_stale_code_tree_bindings(tmp_path: Path, stale: str) -> None:
    """Catches applying reviewed suggestions after branch, commit, or file drift."""
    root = _repository(tmp_path)
    target = root / ".github" / "CODEOWNERS"
    target.parent.mkdir()
    target.write_text(CODEOWNERS, encoding="utf-8")
    preview = preview_code_suggestions(root, CODEOWNERS, WORKFLOW)
    if stale == "branch":
        _git(root, "switch", "-c", "other")
    elif stale == "head":
        (root / "README.md").write_text("changed\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "-m", "changed")
    else:
        target.write_text("changed\n", encoding="utf-8")

    with pytest.raises(CodeSuggestionError, match="stale code suggestion preview"):
        stage_code_suggestions(root, preview)

    assert not (root / ".github" / "workflows" / "intent-state.yml").exists()


def test_preview_rejects_state_branch_and_detached_head(tmp_path: Path) -> None:
    """Catches suggestions entering intent-state or a branchless checkout."""
    root = _repository(tmp_path)
    _git(root, "switch", "-c", "intent-state")
    with pytest.raises(CodeSuggestionError, match="developer code branch required"):
        preview_code_suggestions(root, CODEOWNERS, WORKFLOW)
    _git(root, "switch", "main")
    _git(root, "checkout", "--detach")
    with pytest.raises(CodeSuggestionError, match="developer code branch required"):
        preview_code_suggestions(root, CODEOWNERS, WORKFLOW)


@pytest.mark.parametrize("unsafe", ["symlink-target", "hardlink-target", "symlink-parent"])
@pytest.mark.parametrize("path", ["CODEOWNERS", "workflows/intent-check.yml"])
def test_preview_rejects_linked_targets_and_parents(tmp_path: Path, unsafe: str, path: str) -> None:
    """Catches suggestion reads or writes escaping/reusing attacker-controlled inodes."""
    root = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("outside\n", encoding="utf-8")
    github = root / ".github"
    if unsafe == "symlink-parent":
        github.symlink_to(tmp_path)
    else:
        github.mkdir()
        target = github / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if unsafe == "symlink-target":
            target.symlink_to(outside)
        else:
            os.link(outside, target)

    with pytest.raises(UnsafePathError):
        preview_code_suggestions(root, CODEOWNERS, WORKFLOW)

    assert outside.read_text(encoding="utf-8") == "outside\n"
