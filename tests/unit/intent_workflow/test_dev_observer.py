from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
import yaml  # type: ignore[import-untyped]

import intent_engineering.intent_workflow.dev_observer as observer_module
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


def test_git_observation_ignores_hostile_ambient_repository_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, revision = _repo(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git(foreign, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "hostile.gitconfig"))

    result = _observer(repo).poll(at=NOW)

    assert result.current_revision == revision


def test_git_path_observation_fails_at_the_fixed_path_count_bound(tmp_path: Path) -> None:
    repo, _revision = _repo(tmp_path)
    observer = _observer(repo)
    for index in range(4097):
        (repo / f"untracked-{index:04d}").touch()

    with pytest.raises(DevObserverError, match="development observation unavailable"):
        observer.poll(at=NOW)


def test_bounded_git_runner_stops_oversized_output_and_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_git = tmp_path / "fake-git"
    fake_git.write_text("#!/usr/bin/python3\nprint('x' * 70000)\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setattr(observer_module, "_GIT_EXECUTABLE", fake_git)
    with pytest.raises(ValueError, match="oversized"):
        observer_module._run_git_bounded(tmp_path, ("status",), max_bytes=1024)

    fake_git.write_text(
        "#!/usr/bin/python3\nimport pathlib,time\ntime.sleep(1)\npathlib.Path('late').touch()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(observer_module, "GIT_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(TimeoutError, match="timed out"):
        observer_module._run_git_bounded(tmp_path, ("status",), max_bytes=1024)
    assert not (tmp_path / "late").exists()


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
async def test_reviewed_python_unittest_failure_does_not_produce_passing_evidence(
    tmp_path: Path,
) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(
        repo,
        "import unittest\n"
        "class ReviewedTests(unittest.TestCase):\n"
        "    def test_reviewed_failure(self):\n"
        "        self.fail('reviewed test failed')\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
    )
    observer = _observer(repo)

    result = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)

    assert result.status is TestRunStatus.FAILED, result
    assert result.exit_code == 1
    assert "Ran 1 test" in result.stderr
    assert "reviewed test failed" in result.stderr
    assert "FAILED (failures=1)" in result.stderr
    assert result.artifact is None
    assert not (repo / "test-results.json").exists()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("ending", "status", "exit_code"),
    [
        ("", TestRunStatus.PASSED, 0),
        ("raise SystemExit(37)\n", TestRunStatus.FAILED, 37),
        ("raise RuntimeError('reviewed failure')\n", TestRunStatus.FAILED, 1),
    ],
)
async def test_reviewed_python_main_namespace_preserves_script_metadata_and_exit_semantics(
    tmp_path: Path, ending: str, status: TestRunStatus, exit_code: int
) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(
        repo,
        "import __main__, json, sys\n"
        "print(json.dumps({\n"
        "    'same_namespace': globals() is vars(__main__),\n"
        "    'name': __name__,\n"
        "    'file': __file__,\n"
        "    'module_file': getattr(__main__, '__file__', None),\n"
        "    'package': __package__,\n"
        "    'cached': __cached__,\n"
        "    'argv': sys.argv,\n"
        "    'stdin': sys.stdin.read(),\n"
        "}))\n" + ending,
    )
    observer = DevObserver(
        repo,
        ProjectConfig(
            project_id="demo",
            local_actor="local:asha",
            test_commands=(("tools/test-runner", "literal argument", "--reviewed-option"),),
            test_result_paths=("test-results.json",),
        ),
        repository_id=REPOSITORY_ID,
        principals=frozenset({"local:asha"}),
    )
    expected_file = (
        "intent-reviewed-test:"
        + hashlib.sha256((repo / "tools/test-runner").read_bytes()).hexdigest()
    )

    result = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)

    assert json.loads(result.stdout) == {
        "same_namespace": True,
        "name": "__main__",
        "file": expected_file,
        "module_file": expected_file,
        "package": None,
        "cached": None,
        "argv": [expected_file, "literal argument", "--reviewed-option"],
        "stdin": "",
    }
    assert result.status is status
    assert result.exit_code == exit_code
    assert (result.artifact is not None) is (status is TestRunStatus.PASSED)
    assert (repo / "test-results.json").exists() is (status is TestRunStatus.PASSED)
    if "RuntimeError" in ending:
        assert expected_file in result.stderr
        assert "RuntimeError: reviewed failure" in result.stderr
    else:
        assert "Traceback" not in result.stderr


@pytest.mark.anyio
async def test_unreviewed_command_id_and_changed_executable_never_run(tmp_path: Path) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(repo, "from pathlib import Path\nPath('ran').write_text('yes')\n")
    observer = _observer(repo)

    unknown = await observer.run_reviewed_tests("test:sha256:" + "f" * 64, at=NOW)
    assert unknown.status is TestRunStatus.REJECTED
    assert not (repo / "ran").exists()


@pytest.mark.anyio
async def test_executable_swap_during_launch_cannot_execute_unreviewed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(repo, "from pathlib import Path\nPath('reviewed').write_text('yes')\n")
    observer = _observer(repo)
    original_open_process = anyio.open_process

    async def swap_then_open(*args: object, **kwargs: object) -> anyio.abc.Process:
        _runner(repo, "from pathlib import Path\nPath('unreviewed').write_text('bad')\n")
        return await original_open_process(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(observer_module.anyio, "open_process", swap_then_open)

    result = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)

    assert result.status is TestRunStatus.REJECTED
    assert not (repo / "unreviewed").exists()
    assert (repo / "reviewed").exists()
    assert result.artifact is None

    _runner(repo, "from pathlib import Path\nPath('ran').write_text('changed')\n")
    changed = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)
    assert changed.status is TestRunStatus.REJECTED
    assert not (repo / "ran").exists()


@pytest.mark.anyio
async def test_staged_executable_path_replacement_cannot_change_launched_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(repo, "from pathlib import Path\nPath('reviewed').write_text('yes')\n")
    observer = _observer(repo)
    real_open_process = anyio.open_process
    real_unlink = os.unlink
    staged_paths: list[Path] = []
    attacked: list[Path] = []

    def record_staged_unlink(path: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        candidate = Path(path)
        if candidate.name == "executable" and candidate.parent.name.startswith(
            "intent-reviewed-test-"
        ):
            staged_paths.append(candidate)
        real_unlink(path)

    async def replace_staged_path(*args: object, **kwargs: object) -> anyio.abc.Process:
        visible = list(Path(tempfile.gettempdir()).glob("intent-reviewed-test-*/executable"))
        for staged in (*visible, *staged_paths):
            if staged in attacked:
                continue
            staged.parent.mkdir(mode=0o700, exist_ok=True)
            if staged.exists():
                staged.parent.chmod(0o700)
                staged.chmod(0o700)
            attacked.append(staged)
            staged.write_text(
                "#!/usr/bin/python3\nfrom pathlib import Path\n"
                "Path('unreviewed').write_text('bad')\n",
                encoding="utf-8",
            )
        return await real_open_process(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(observer_module.os, "unlink", record_staged_unlink)
    monkeypatch.setattr(observer_module.anyio, "open_process", replace_staged_path)

    try:
        result = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)
    finally:
        for staged in attacked:
            staged.unlink(missing_ok=True)
            staged.parent.rmdir()

    assert result.status is TestRunStatus.PASSED, (result, attacked)
    assert not staged_paths
    assert not attacked
    assert (repo / "reviewed").exists(), (result, attacked)
    assert not (repo / "unreviewed").exists()


@pytest.mark.anyio
async def test_missing_descriptor_backed_launch_support_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(repo, "from pathlib import Path\nPath('reviewed').write_text('yes')\n")
    observer = _observer(repo)
    real_open_process = anyio.open_process

    async def reject_passed_descriptors(*args: object, **kwargs: object) -> anyio.abc.Process:
        if kwargs.get("pass_fds"):
            raise OSError("descriptor passing unavailable")
        return await real_open_process(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(observer_module.anyio, "open_process", reject_passed_descriptors)

    result = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)

    assert result.status is TestRunStatus.REJECTED
    assert not (repo / "reviewed").exists()


@pytest.mark.anyio
async def test_retained_writer_cannot_change_source_after_supervisor_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(
        repo,
        "from pathlib import Path\nPath('reviewed').write_text('yes')\n" + "# pad\n" * 256,
    )
    observer = _observer(repo)
    executable = repo / "tools/test-runner"
    reviewed_source = executable.read_bytes()
    malicious_prefix = (
        b"#!/usr/bin/python3\nfrom pathlib import Path\nPath('unreviewed').write_text('bad')\n"
    )
    malicious_source = malicious_prefix + b"#" * (len(reviewed_source) - len(malicious_prefix))
    retained_writers: list[int] = []
    real_open = os.open
    real_source_pipe = os.pipe

    def retain_staged_writer(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if Path(path).name == "executable" and flags & os.O_ACCMODE == os.O_WRONLY:
            retained_writers.append(os.dup(descriptor))
        return descriptor

    def retain_source_pipe_writer() -> tuple[int, int]:
        reader, writer = real_source_pipe()
        retained_writers.append(os.dup(writer))
        return reader, writer

    monkeypatch.setattr(observer_module.os, "open", retain_staged_writer)
    monkeypatch.setattr(
        observer_module,
        "_open_source_pipe",
        retain_source_pipe_writer,
        raising=False,
    )
    boundary = repo / "source-verified-boundary"
    source = observer_module._PROCESS_SUPERVISOR
    old_boundary = "os.lseek(reviewed_descriptor, 0, os.SEEK_SET)\nspawned = None"
    new_boundary = "# source-verified-boundary\nspawned = None"
    selected_boundary = old_boundary if old_boundary in source else new_boundary
    source = source.replace(
        selected_boundary,
        selected_boundary.split("\n", 1)[0]
        + f"\nopen({str(boundary)!r}, 'w').close()\ntime.sleep(0.5)\nspawned = None",
        1,
    )
    assert source != observer_module._PROCESS_SUPERVISOR
    monkeypatch.setattr(observer_module, "_PROCESS_SUPERVISOR", source)
    completed = anyio.Event()
    results: list[object] = []

    async def run() -> None:
        try:
            results.append(await observer.run_reviewed_tests(observer.command_ids[0], at=NOW))
        finally:
            completed.set()

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run)
            with anyio.fail_after(2):
                while not boundary.exists() and not completed.is_set():
                    await anyio.sleep(0.01)
            assert boundary.exists(), results
            assert retained_writers
            for descriptor in retained_writers:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                except OSError:
                    try:
                        os.write(descriptor, malicious_source)
                    except OSError:
                        pass
                else:
                    os.write(descriptor, malicious_source)
        result = results[0]
    finally:
        for descriptor in retained_writers:
            os.close(descriptor)

    assert result.status is TestRunStatus.PASSED
    assert (repo / "reviewed").exists()
    assert not (repo / "unreviewed").exists()


@pytest.mark.anyio
async def test_shell_source_cannot_consume_failing_lines_as_command_stdin(tmp_path: Path) -> None:
    repo, _revision = _repo(tmp_path)
    runner = repo / "tools/test-runner"
    runner.parent.mkdir(exist_ok=True)
    runner.write_text("#!/bin/sh\nread swallowed\nexit 1\n", encoding="utf-8")
    runner.chmod(0o755)
    observer = _observer(repo)

    result = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)

    assert result.status is TestRunStatus.FAILED
    assert result.artifact is None
    assert not (repo / "test-results.json").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("module_name", ["signal", "subprocess"])
async def test_supervisor_imports_cannot_resolve_from_repository(
    tmp_path: Path, module_name: str
) -> None:
    repo, _revision = _repo(tmp_path)
    marker = repo / f"hostile-{module_name}-ran"
    (repo / f"{module_name}.py").write_text(
        f"open({str(marker)!r}, 'w').close()\nraise RuntimeError('hostile import')\n",
        encoding="utf-8",
    )
    _runner(repo, "from pathlib import Path\nPath('reviewed').write_text('yes')\n")
    observer = _observer(repo)

    result = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)

    assert result.status is TestRunStatus.PASSED
    assert not marker.exists()
    assert (repo / "reviewed").exists()


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

    async def run_cancellable() -> None:
        await cancellable.run_reviewed_tests(cancellable.command_ids[0], at=NOW)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run_cancellable)
        with anyio.fail_after(2):
            while not (repo / "started").exists():
                await anyio.sleep(0.01)
        tasks.cancel_scope.cancel()
    await anyio.sleep(0.1)
    assert (repo / "started").exists()
    assert not (repo / "finished").exists()


@pytest.mark.anyio
async def test_cancellation_kills_a_real_grandchild_process_tree(tmp_path: Path) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(
        repo,
        "import pathlib, subprocess, time\n"
        "subprocess.Popen(['/usr/bin/python3', '-c', "
        "'import pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        'pathlib.Path("grandchild-started").touch(); time.sleep(1); '
        'pathlib.Path("grandchild-finished").write_text("bad")\'])\n'
        "pathlib.Path('parent-started').touch()\n"
        "time.sleep(30)\n",
    )
    observer = _observer(repo)

    with anyio.move_on_after(0.4) as scope:
        await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)

    assert scope.cancel_called
    assert (repo / "parent-started").exists()
    assert (repo / "grandchild-started").exists()
    await anyio.sleep(1)
    assert not (repo / "grandchild-finished").exists()


@pytest.mark.anyio
async def test_cancellation_at_spawn_assignment_boundary_cannot_orphan_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(
        repo,
        "import os, pathlib, signal, subprocess, time\n"
        "pathlib.Path('parent-pid').write_text(str(os.getpid()))\n"
        "subprocess.Popen(['/usr/bin/python3', '-I', '-S', '-c', "
        "'import os,pathlib,signal,time; "
        'pathlib.Path("grandchild-pid").write_text(str(os.getpid())); '
        'pathlib.Path("grandchild-started").touch(); '
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(0.8); "
        'pathlib.Path("grandchild-orphaned").write_text("bad")\'])\n'
        "time.sleep(30)\n",
    )
    observer = _observer(repo)
    source = observer_module._PROCESS_SUPERVISOR
    old_launch = "child = subprocess.Popen(sys.argv[1:], start_new_session=True)"
    if old_launch in source:
        source = source.replace(
            old_launch,
            "spawned = subprocess.Popen(sys.argv[1:], start_new_session=True)\n"
            "time.sleep(0.5)\nchild = spawned",
            1,
        )
    else:
        source = source.replace(
            "    child = spawned",
            "    time.sleep(0.5)\n    child = spawned",
            1,
        )
    monkeypatch.setattr(observer_module, "_PROCESS_SUPERVISOR", source)

    completed = anyio.Event()
    results: list[object] = []

    async def run() -> None:
        try:
            results.append(await observer.run_reviewed_tests(observer.command_ids[0], at=NOW))
        finally:
            completed.set()

    orphaned = False
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run)
            with anyio.fail_after(2):
                while not (repo / "grandchild-started").exists() and not completed.is_set():
                    await anyio.sleep(0.01)
            assert not completed.is_set(), results
            tasks.cancel_scope.cancel()
        await anyio.sleep(0.9)
        orphaned = (repo / "grandchild-orphaned").exists()
    finally:
        for name in ("parent-pid", "grandchild-pid"):
            path = repo / name
            if path.exists():
                try:
                    os.kill(int(path.read_text(encoding="utf-8")), signal.SIGKILL)
                except ProcessLookupError:
                    pass
    assert not orphaned


@pytest.mark.anyio
async def test_missing_process_group_ownership_fails_closed_before_running_grandchild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _revision = _repo(tmp_path)
    _runner(
        repo,
        "import subprocess, time\n"
        "subprocess.Popen(['/usr/bin/python3', '-c', "
        '\'import pathlib,time; time.sleep(1); pathlib.Path("grandchild-finished").write_text("bad")\'])\n'
        "time.sleep(30)\n",
    )
    observer = _observer(repo)
    monkeypatch.setattr(
        observer_module.os,
        "killpg",
        lambda _pid, _signal: (_ for _ in ()).throw(PermissionError()),
    )

    result = None
    with anyio.move_on_after(0.2) as scope:
        result = await observer.run_reviewed_tests(observer.command_ids[0], at=NOW)
    await anyio.sleep(1.2)
    assert not scope.cancel_called
    assert result is not None and result.status is TestRunStatus.REJECTED
    assert not (repo / "grandchild-finished").exists()


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
