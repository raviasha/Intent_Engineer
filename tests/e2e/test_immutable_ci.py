"""Real immutable Git material and opt-in Docker execution regressions."""

import io
import json
import os
import sys
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


def test_context_pins_base_and_locked_wheels_without_a_trust_secret(tmp_path, monkeypatch):
    from intent_engineering.integrations.immutable_ci import build_context

    repo, _trust = _restored(tmp_path, monkeypatch)
    secret = json.loads(os.environ["INTENT_CI_SHARED_STATE_TRUST"])[
        "recipient_private_key_base64"
    ].encode()
    context = build_context(repo, at=NOW)
    assert secret not in context
    with tarfile.open(fileobj=io.BytesIO(context)) as archive:
        dockerfile = archive.extractfile("Dependency.Dockerfile").read().decode()
        assert "FROM python@sha256:" in dockerfile
        assert "--require-hashes" in dockerfile
        assert "--only-binary=:all:" in dockerfile
        assert "pip install" not in archive.extractfile("Dockerfile").read().decode()
        locked = archive.extractfile("requirements.txt").read().decode()
        assert "--hash=sha256:" in locked
        assert all(
            "==" in line.split(";", 1)[0]
            for line in locked.splitlines()
            if line and not line.startswith((" ", "#"))
        )


def test_host_subprocesses_never_inherit_trust_or_github_credentials(monkeypatch):
    from intent_engineering.integrations.immutable_ci import _command

    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", "fixture-secret")
    monkeypatch.setenv("GH_TOKEN", "fixture-github-token")
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-github-token")
    assert (
        _command(
            [
                sys.executable,
                "-I",
                "-c",
                "import os; assert not {'INTENT_CI_SHARED_STATE_TRUST','GH_TOKEN','GITHUB_TOKEN'} & os.environ.keys(); print('isolated')",
            ]
        )
        == b"isolated\n"
    )


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


@pytest.mark.skipif(
    os.environ.get("INTENT_DOCKER_TEST") != "1", reason="explicit Docker release gate"
)
def test_test_container_is_destroyed_before_fresh_consumer_receives_trust(tmp_path, monkeypatch):
    from intent_engineering.integrations import immutable_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    command = immutable_ci._command
    observed = []
    images = []
    builds = []
    secret = os.environ["INTENT_CI_SHARED_STATE_TRUST"].encode()

    def inspect_real_boundary(argv, **options):
        if argv[1] == "build":
            context = options["content"]
            assert secret not in context
            with tarfile.open(fileobj=io.BytesIO(context)) as archive:
                if not builds:
                    assert set(archive.getnames()) == {"Dockerfile", "requirements.txt"}
                else:
                    assert "--network=none" in argv
                    assert "repository/.git/HEAD" in archive.getnames()
            builds.append(argv)
        if argv[1] == "run":
            assert {
                "--read-only",
                "--network=none",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user=65532:65532",
            }.issubset(argv)
            images.append(argv[-2])
            name = argv[argv.index("--name") + 1]
            if not observed:
                assert secret not in options["content"]
                assert b'"trust"' not in options["content"]
            else:
                remaining = command(
                    [argv[0], "ps", "--all", "--quiet", "--filter", "name=^/" + observed[0] + "$"]
                )
                assert remaining == b"", "test container survived into final assurance"
                assert name != observed[0]
                assert b'"trust"' in options["content"]
                assert b'"result"' in options["content"]
            observed.append(name)
        return command(argv, **options)

    monkeypatch.setattr(immutable_ci, "_command", inspect_real_boundary)
    assert immutable_ci.run_immutable_check(repo, at=NOW).status == "passed"
    assert len(observed) == 2
    assert len(builds) == 2
    assert len(set(images)) == 1


@pytest.mark.skipif(
    os.environ.get("INTENT_DOCKER_TEST") != "1", reason="explicit Docker release gate"
)
def test_same_verified_image_can_be_consumed_with_a_different_mount_identity(tmp_path, monkeypatch):
    from intent_engineering.integrations import immutable_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    command = immutable_ci._command
    keeper = None

    def occupy_previous_mount_identity(argv, **options):
        nonlocal keeper
        if argv[1] == "run" and argv[-1] == "consume":
            keeper = (
                command(
                    [
                        argv[0],
                        "run",
                        "--detach",
                        "--read-only",
                        "--network=none",
                        "--cap-drop=ALL",
                        "--security-opt=no-new-privileges",
                        "--user=65532:65532",
                        "--entrypoint=/bin/sleep",
                        argv[-2],
                        "60",
                    ]
                )
                .decode()
                .strip()
            )
        if keeper and argv[1:3] == ["image", "ls"]:
            command([argv[0], "rm", "--force", keeper])
            keeper = None
        return command(argv, **options)

    monkeypatch.setattr(immutable_ci, "_command", occupy_previous_mount_identity)
    try:
        assert immutable_ci.run_immutable_check(repo, at=NOW).status == "passed"
    finally:
        if keeper:
            command(["docker", "rm", "--force", keeper])


@pytest.mark.skipif(
    os.environ.get("INTENT_DOCKER_TEST") != "1", reason="explicit Docker release gate"
)
def test_detached_poisoner_cannot_observe_or_change_final_assurance(tmp_path, monkeypatch):
    from intent_engineering.integrations.immutable_ci import run_immutable_check

    repo, _trust = _restored(tmp_path, monkeypatch)
    poisoner = (
        "import os,time\nfrom pathlib import Path\n"
        "if os.fork(): os._exit(0)\nos.setsid()\n"
        "if os.fork(): os._exit(0)\n"
        "initial=set(Path('/output').glob('assurance-*'))\n"
        "Path('/output/poisoner-started').write_text(str(os.getpid()))\n"
        "deadline=time.monotonic()+10\n"
        "while time.monotonic()<deadline:\n"
        "    for workspace in set(Path('/output').glob('assurance-*'))-initial:\n"
        "        try: (workspace/'.intent/graph.yaml').write_text('poisoned final assurance')\n"
        "        except OSError: pass\n"
        "    time.sleep(0.001)\n"
    )
    (repo / "tools/test-runner").write_text(
        "#!/usr/bin/python3\nimport os,subprocess,sys,time\nfrom pathlib import Path\n"
        "assert 'INTENT_CI_SHARED_STATE_TRUST' not in os.environ\n"
        "for process in Path('/proc').iterdir():\n"
        "    if process.name.isdigit():\n"
        "        try: assert b'INTENT_CI_SHARED_STATE_TRUST=' not in (process/'environ').read_bytes()\n"
        "        except (PermissionError,FileNotFoundError,ProcessLookupError): pass\n"
        f"subprocess.Popen([sys.executable,'-I','-c',{poisoner!r}], "
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).wait()\n"
        "deadline=time.monotonic()+2\n"
        "while not Path('/output/poisoner-started').exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        "os.kill(int(Path('/output/poisoner-started').read_text()),0)\n"
    )
    (repo / "pyproject.toml").write_text(
        '[build-system]\nrequires=[]\nbuild-backend="trap"\nbackend-path=["."]\n'
    )
    (repo / "trap.py").write_text("raise AssertionError('PR package build hook executed')\n")
    git(repo, "add", "tools/test-runner", "pyproject.toml", "trap.py")
    git(repo, "commit", "-qm", "adversarial proposed backend and detached assurance poisoner")
    assert run_immutable_check(repo, at=NOW).status == "passed"


@pytest.mark.skipif(
    os.environ.get("INTENT_DOCKER_TEST") != "1", reason="explicit Docker release gate"
)
def test_incorrect_wheel_hash_fails_closed_before_project_execution(tmp_path, monkeypatch):
    from intent_engineering.integrations import immutable_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    monkeypatch.setattr(
        immutable_ci,
        "_requirements",
        lambda: b"annotated-doc==0.0.5 --hash=sha256:" + b"0" * 64 + b"\n",
    )
    with pytest.raises(ValueError, match="^immutable CI unavailable$"):
        immutable_ci.run_immutable_check(repo, at=NOW)
    assert not (repo / ".intent-ci/test-results.json").exists()
