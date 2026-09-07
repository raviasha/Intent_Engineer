from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.control_plane.service import ControlPlaneService
from intent_engineering.core.models import EvidenceRecord, ProjectConfig
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.check import TestResultArtifact
from intent_engineering.intent_workflow.dev_observer import (
    DevObserver,
    DevObserverError,
    TestRunStatus,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
REPOSITORY_ID = "repo:sha256:" + "1" * 64


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.test")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def _runner(repo: Path, source: str) -> None:
    runner = repo / "tools" / "test-runner"
    runner.parent.mkdir(exist_ok=True)
    runner.write_text("#!/usr/bin/python3\n" + source, encoding="utf-8")
    runner.chmod(0o755)


def _observer(repo: Path, **updates: object) -> DevObserver:
    config = ProjectConfig(
        project_id="demo",
        local_actor="local:asha",
        test_commands=(("tools/test-runner",),),
        test_result_paths=("test-results.json",),
        **updates,
    )
    return DevObserver(
        repo,
        config,
        repository_id=REPOSITORY_ID,
        principals=frozenset({"local:asha"}),
    )


def test_observer_rejects_a_git_worktree_owned_by_a_parent_repository(tmp_path: Path) -> None:
    parent, _revision = _repo(tmp_path)
    nested = parent / "nested"
    nested.mkdir()

    with pytest.raises(DevObserverError, match="development observation unavailable"):
        DevObserver(
            nested,
            ProjectConfig(project_id="demo", local_actor="local:asha"),
            repository_id=REPOSITORY_ID,
            principals=frozenset({"local:asha"}),
        )


def test_poll_emits_only_evidence_candidates_for_git_changes_and_is_idempotent(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    observer = _observer(repo)

    first = observer.poll(at=NOW)
    replay = observer.poll(at=NOW)
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    changed = observer.poll(at=NOW)

    assert first.current_revision == revision
    assert first.changed_paths == ()
    assert all(type(item) is EvidenceRecord for item in first.evidence_candidates)
    assert replay.evidence_candidates == ()
    assert changed.changed_paths == ("tracked.txt",)
    assert len(changed.evidence_candidates) == 1
    assert changed.evidence_candidates[0].payload["observation_kind"] == "git_paths"
    assert "implementation_status" not in changed.evidence_candidates[0].payload


def test_poll_ingests_only_fresh_bound_passing_canonical_result_artifacts(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    observer = _observer(repo)
    artifact = TestResultArtifact(
        repository_id=REPOSITORY_ID,
        commit_sha=revision,
        observed_at=NOW,
        status="passed",
        test_ids=(observer.command_ids[0],),
        author="local:asha",
        acl=("local:asha",),
    )
    (repo / "test-results.json").write_bytes(artifact.canonical_bytes())

    result = observer.poll(at=NOW)
    replay = observer.poll(at=NOW)

    assert artifact.evidence().id in {item.id for item in result.evidence_candidates}
    assert replay.evidence_candidates == ()


def test_poll_rejects_an_oversized_or_wrongly_bound_result_artifact(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    observer = _observer(repo)
    (repo / "test-results.json").write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(DevObserverError, match="development observation unavailable"):
        observer.poll(at=NOW)

    artifact = TestResultArtifact(
        repository_id=REPOSITORY_ID,
        commit_sha="2" * len(revision),
        observed_at=NOW,
        status="passed",
        test_ids=(observer.command_ids[0],),
        author="local:asha",
        acl=("local:asha",),
    )
    (repo / "test-results.json").write_bytes(artifact.canonical_bytes())
    with pytest.raises(DevObserverError, match="development observation unavailable"):
        observer.poll(at=NOW)


@pytest.mark.anyio
async def test_explicit_reviewed_command_runs_without_shell_and_captures_canonical_result(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    _runner(
        repo,
        "import os, pathlib\n"
        "assert os.environ['LANG'] == 'C.UTF-8'\n"
        "assert os.environ['LC_ALL'] == 'C.UTF-8'\n"
        "assert os.environ['PATH'] == '/usr/bin:/bin'\n"
        "assert 'HOME' not in os.environ\n"
        "pathlib.Path('literal-$HOME;*').write_text('argv-only')\n",
    )
    observer = _observer(repo)

    result = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)

    assert result.status is TestRunStatus.PASSED
    assert result.artifact is not None
    assert result.artifact.repository_id == REPOSITORY_ID
    assert result.artifact.commit_sha == revision
    assert result.artifact.test_ids == (observer.command_ids[0],)
    assert json.loads((repo / "test-results.json").read_bytes()) == json.loads(
        result.artifact.canonical_bytes()
    )


@pytest.mark.anyio
async def test_unreviewed_command_id_and_changed_executable_never_run(tmp_path: Path) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(repo, "from pathlib import Path\nPath('ran').write_text('yes')\n")
    observer = _observer(repo)

    unknown = await observer.run_reviewed_tests("test:sha256:" + "f" * 64, at=NOW)
    assert unknown.status is TestRunStatus.REJECTED
    assert not (repo / "ran").exists()

    _runner(repo, "from pathlib import Path\nPath('ran').write_text('changed')\n")
    changed = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)
    assert changed.status is TestRunStatus.REJECTED
    assert not (repo / "ran").exists()


@pytest.mark.anyio
async def test_output_limit_and_cancellation_clean_up_the_child(tmp_path: Path) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(repo, "print('x' * 70000)\n")
    observer = _observer(repo)
    oversized = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)
    assert oversized.status is TestRunStatus.OUTPUT_LIMIT
    assert oversized.artifact is None
    assert len(oversized.stdout) <= 64 * 1024

    _runner(
        repo,
        "import pathlib, time\n"
        "pathlib.Path('started').write_text(str(__import__('os').getpid()))\n"
        "time.sleep(30)\n"
        "pathlib.Path('finished').write_text('bad')\n",
    )
    cancellable = _observer(repo)
    with anyio.move_on_after(0.2) as scope:
        await cancellable.run_reviewed_tests(cancellable.command_ids[0], at=NOW)
    assert scope.cancel_called
    await anyio.sleep(0.1)
    assert (repo / "started").exists()
    assert not (repo / "finished").exists()


@pytest.mark.anyio
async def test_control_plane_exposes_passive_observation_and_explicit_test_action(
    tmp_path: Path,
) -> None:
    repo, _revision = _repo(tmp_path)
    initialize_project(repo)
    _runner(repo, "print('passed')\n")
    config = ProjectConfig(
        project_id="demo",
        local_actor="local:asha",
        source_exclusions=(".intent/**", ".git/**"),
        test_commands=(("tools/test-runner",),),
        test_result_paths=(".intent/cache/test-results.json",),
    )
    (repo / ".intent/config.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    runtime = load_runtime(repo)
    service = ControlPlaneService(runtime, origin="http://localhost:43127", clock=lambda: NOW)
    try:
        observed = service.observe_development()
        result = await service.run_reviewed_tests(observed.command_ids[0])
    finally:
        service.close()
        runtime.close()

    assert all(type(item) is EvidenceRecord for item in observed.evidence_candidates)
    assert result.status is TestRunStatus.PASSED
