"""Real immutable Git material and opt-in Docker execution regressions."""

import io
import os
import tarfile
from pathlib import Path

import pytest

from tests.e2e.test_test_evidence_bindings import _restored
from tests.helpers.shared_state import NOW, git


def test_build_context_uses_git_objects_and_authenticated_baseline(tmp_path, monkeypatch):
    from intent_engineering.integrations.immutable_ci import build_context

    repo, _trust = _restored(tmp_path, monkeypatch)
    (repo / "feature.py").write_bytes(b"uncommitted contaminating code")
    (repo / ".intent/graph.yaml").write_bytes(b"unapproved contaminating intent")
    context = build_context(repo, at=NOW)
    with tarfile.open(fileobj=io.BytesIO(context), mode="r:") as archive:
        assert "repository/.git/HEAD" in archive.getnames()
        assert (
            archive.extractfile("repository/feature.py").read()
            == b"def enabled():\n    return True\n"
        )
        assert (
            archive.extractfile("baseline/graph.yaml").read() != b"unapproved contaminating intent"
        )
        assert not any(name.startswith("project/") for name in archive.getnames())
        assert "Dockerfile" in archive.getnames()
        assert '"-I", "-m"' in archive.extractfile("Dockerfile").read().decode()
    assert b"uncommitted contaminating code" not in context
    assert b"unapproved contaminating intent" not in context


def test_missing_docker_clears_stale_output_and_fails_closed(tmp_path, monkeypatch):
    from intent_engineering.integrations import immutable_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    output = repo / ".intent-ci/test-results.json"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(b"stale result")
    monkeypatch.setattr(immutable_ci.shutil, "which", lambda _name: None)
    with pytest.raises(ValueError, match="^immutable CI unavailable$"):
        immutable_ci.run_immutable_check(repo, at=NOW)
    assert not output.exists()


@pytest.mark.parametrize("replacement", ["changed", "noncanonical"])
def test_only_exact_accepted_canonical_result_can_leave_container(
    tmp_path, monkeypatch, replacement
):
    import anyio

    from intent_engineering.integrations import immutable_ci
    from intent_engineering.integrations.github_action import run_tests, write_results
    from intent_engineering.intent_workflow.check import TestResultArtifact

    repo, _trust = _restored(tmp_path, monkeypatch)
    anyio.run(run_tests, repo, NOW)
    write_results(repo, NOW)
    raw = (repo / ".intent-ci/test-results.json").read_bytes()
    artifact = TestResultArtifact.model_validate_json(raw)
    accepted_id = artifact.evidence().id
    assert immutable_ci._accepted_result(raw, accepted_id) == raw
    altered = (
        artifact.model_copy(update={"author": "different producer"}).canonical_bytes()
        if replacement == "changed"
        else b" " + raw
    )
    with pytest.raises(ValueError, match="^immutable CI unavailable$"):
        immutable_ci._accepted_result(altered, accepted_id)


@pytest.mark.skipif(
    os.environ.get("INTENT_DOCKER_TEST") != "1", reason="explicit Docker release gate"
)
def test_real_read_only_image_runs_and_consumes_evidence(tmp_path: Path, monkeypatch):
    from intent_engineering.integrations.immutable_ci import run_immutable_check

    repo, _trust = _restored(tmp_path, monkeypatch)
    runner = repo / "tools/test-runner"
    runner.write_text(
        "#!/usr/bin/python3\nimport errno, mmap, os\n"
        "for path in ('feature.py', '.intent/config.yaml', '.intent/graph.yaml'):\n"
        "    assert os.statvfs(path).f_flag & os.ST_RDONLY\n"
        "    try:\n"
        "        with open(path, 'r+b') as stream:\n"
        "            with mmap.mmap(stream.fileno(), 0) as memory: memory[0:1] = b'X'\n"
        "    except OSError as error: assert error.errno in (errno.EACCES, errno.EROFS)\n"
        "    else: raise AssertionError('mutable evidence material')\n"
    )
    shadow = repo / "intent_engineering/__init__.py"
    shadow.parent.mkdir()
    shadow.write_text("raise AssertionError('project code shadowed trusted entrypoint')\n")
    git(repo, "add", "tools/test-runner", "intent_engineering/__init__.py")
    git(repo, "commit", "-qm", "assert kernel-enforced immutable material")
    artifact = run_immutable_check(repo, at=NOW)
    assert artifact.status == "passed"
    assert (repo / ".intent-ci/test-results.json").read_bytes() == artifact.canonical_bytes()


@pytest.mark.skipif(
    os.environ.get("INTENT_DOCKER_TEST") != "1", reason="explicit Docker release gate"
)
def test_dirty_passing_checkout_cannot_override_failing_commit_in_real_image(tmp_path, monkeypatch):
    from intent_engineering.integrations.immutable_ci import run_immutable_check

    repo, _trust = _restored(tmp_path, monkeypatch)
    feature = repo / "feature.py"
    feature.write_text("def enabled():\n    return False\n")
    git(repo, "add", "feature.py")
    git(repo, "commit", "-qm", "failing committed behavior")
    feature.write_text("def enabled():\n    return True\n")
    with pytest.raises(ValueError, match="immutable CI unavailable"):
        run_immutable_check(repo, at=NOW)
    assert not (repo / ".intent-ci/test-results.json").exists()


@pytest.mark.skipif(
    os.environ.get("INTENT_DOCKER_TEST") != "1", reason="explicit Docker release gate"
)
def test_read_only_source_bind_mount_is_not_an_immutable_image(tmp_path, monkeypatch):
    from intent_engineering.integrations import immutable_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    command = immutable_ci._command

    def bind_mutable_source(argv, **options):
        if argv[1] == "run":
            argv = [
                *argv[:2],
                "--mount",
                f"type=bind,source={repo / 'feature.py'},target=/project/feature.py,readonly",
                *argv[2:],
            ]
        return command(argv, **options)

    monkeypatch.setattr(immutable_ci, "_command", bind_mutable_source)
    with pytest.raises(ValueError, match="immutable CI unavailable"):
        immutable_ci.run_immutable_check(repo, at=NOW)
    assert not (repo / ".intent-ci/test-results.json").exists()


@pytest.mark.skipif(
    os.environ.get("INTENT_DOCKER_TEST") != "1", reason="explicit Docker release gate"
)
def test_consumer_independently_rejects_a_network_enabled_container(tmp_path, monkeypatch):
    from intent_engineering.integrations import immutable_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    command = immutable_ci._command

    def network_enabled(argv, **options):
        argv = ["--network=bridge" if value == "--network=none" else value for value in argv]
        return command(argv, **options)

    monkeypatch.setattr(immutable_ci, "_command", network_enabled)
    with pytest.raises(ValueError, match="immutable CI unavailable"):
        immutable_ci.run_immutable_check(repo, at=NOW)
    assert not (repo / ".intent-ci/test-results.json").exists()
