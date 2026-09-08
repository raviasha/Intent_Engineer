"""Real immutable Git material and opt-in Docker execution regressions."""

import base64
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import traceback
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.e2e.test_test_evidence_bindings import _restored
from tests.helpers.shared_state import (
    NOW,
    build_state_payload,
    canonical_files,
    git,
    install_state_ref,
    seal_state_payload,
)


def _runner_local_provider(tmp_path, monkeypatch, repo):
    import keyring

    from intent_engineering.team_state.ci import CiKeyStore, CiTrustConfig, CiTrustProvider
    from tests.unit.team_state.test_ci_recipient import Backend

    backend = Backend()
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    lock_root = tmp_path / "ci-locks"
    recipient = CiKeyStore(
        "project",
        "github.com/acme/project",
        "release-01",
        backend=backend,
        lock_root=lock_root,
    ).provision()
    signer = Ed25519PrivateKey.from_private_bytes(b"s" * 32)
    release = seal_state_payload(
        build_state_payload(canonical_files(repo)),
        project_id="project",
        repository_id="github.com/acme/project",
        graph_version=1,
        parent_bundle_digest=None,
        created_at=NOW,
        recipient_public_keys={
            recipient.key_id: base64.urlsafe_b64decode(recipient.public_key + "=")
        },
        signing_private_keys={"signer:release": signer.private_bytes_raw()},
    )
    install_state_ref(repo, release)
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    config_path = protected / "trust.json"
    config_path.write_bytes(
        CiTrustConfig(
            recipient=recipient,
            signing_public_keys={
                "signer:release": base64.b64encode(signer.public_key().public_bytes_raw()).decode()
            },
        ).canonical_bytes()
    )
    config_path.chmod(0o600)
    monkeypatch.delenv("INTENT_CI_SHARED_STATE_TRUST", raising=False)
    provider = CiTrustProvider(
        config_path,
        backend=backend,
        lock_root=lock_root,
        checkout_root=repo,
    )
    return provider, tuple(backend.values.values())


def test_build_context_uses_git_objects_and_authenticated_baseline(tmp_path, monkeypatch):
    from intent_engineering.integrations.immutable_ci import build_context

    repo, _trust = _restored(tmp_path, monkeypatch)
    (repo / "feature.py").write_bytes(b"uncommitted contaminating code")
    (repo / ".intent/graph.yaml").write_bytes(b"unapproved contaminating intent")
    context = build_context(
        repo,
        at=NOW,
        nonce="6" * 32,
        owner="acme/project:123456789:2",
    )
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
    context = build_context(
        repo,
        at=NOW,
        nonce="6" * 32,
        owner="acme/project:123456789:2",
    )
    assert secret not in context
    with tarfile.open(fileobj=io.BytesIO(context)) as archive:
        dockerfile = archive.extractfile("Dependency.Dockerfile").read().decode()
        assert "FROM python@sha256:" in dockerfile
        assert 'intent.ephemeral-ci="acme/project:123456789:2"' in dockerfile
        assert 'intent.ephemeral-ci.nonce="' + "6" * 32 + '"' in dockerfile
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


def test_context_uses_runner_local_keyring_trust_without_serializing_private_key(
    tmp_path, monkeypatch
):
    from intent_engineering.integrations.immutable_ci import build_context

    repo, _trust = _restored(tmp_path, monkeypatch)
    provider, private_values = _runner_local_provider(tmp_path, monkeypatch, repo)
    tip = git(repo, "rev-parse", "refs/remotes/origin/intent-state").decode().strip()

    context = build_context(repo, at=NOW, trust_provider=provider, state_tip=tip)

    assert private_values
    assert all(value.encode() not in context for value in private_values)
    with tarfile.open(fileobj=io.BytesIO(context)) as archive:
        assert archive.extractfile("repository/.git/refs/remotes/origin/intent-state").read() == (
            tip.encode() + b"\n"
        )


def test_context_rejects_a_state_tip_other_than_the_authenticated_ref(tmp_path, monkeypatch):
    from intent_engineering.integrations.immutable_ci import build_context

    repo, _trust = _restored(tmp_path, monkeypatch)
    provider, _private_values = _runner_local_provider(tmp_path, monkeypatch, repo)

    with pytest.raises(ValueError, match="^immutable CI unavailable$"):
        build_context(repo, at=NOW, trust_provider=provider, state_tip="0" * 40)


def test_context_failure_traceback_retains_neither_private_key_nor_plaintext(tmp_path, monkeypatch):
    from intent_engineering.integrations import immutable_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    provider, private_values = _runner_local_provider(tmp_path, monkeypatch, repo)
    tip = git(repo, "rev-parse", "refs/remotes/origin/intent-state").decode().strip()
    monkeypatch.setattr(
        immutable_ci,
        "_git_material",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("after decryption")),
    )

    with pytest.raises(ValueError, match="^immutable CI unavailable$") as caught:
        immutable_ci.build_context(repo, at=NOW, trust_provider=provider, state_tip=tip)

    private_markers = tuple(value.encode() for value in private_values)
    for frame, _line in traceback.walk_tb(caught.value.__traceback__):
        if frame.f_globals.get("__name__") != "intent_engineering.integrations.immutable_ci":
            continue
        assert provider not in frame.f_locals.values()
        retained = repr(frame.f_locals).encode()
        assert all(marker not in retained for marker in private_markers)
        assert b"Approved shared-state baseline" not in retained


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


def test_runner_private_key_never_enters_build_or_consumer_and_images_are_removed(
    tmp_path, monkeypatch
):
    from intent_engineering.integrations import immutable_ci
    from intent_engineering.intent_workflow.check import TestResultArtifact

    repo, _trust = _restored(tmp_path, monkeypatch)
    provider, private_values = _runner_local_provider(tmp_path, monkeypatch, repo)
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    state_tip = git(repo, "rev-parse", "refs/remotes/origin/intent-state").decode().strip()
    artifact = TestResultArtifact(
        repository_id="project",
        commit_sha=commit,
        execution_snapshot="sha256:" + "1" * 64,
        intent_baseline="sha256:" + "2" * 64,
        reviewed_commands="sha256:" + "3" * 64,
        observed_at=NOW,
        status="passed",
        test_ids=("test:protected",),
        author="local:owner",
        acl=("local:owner",),
    ).canonical_bytes()
    dependency = "sha256:" + "4" * 64
    final = "sha256:" + "5" * 64
    images: list[str] = []
    build_count = 0
    consumer_request = None

    def command(argv, *, content=b"", **_options):
        nonlocal build_count
        if argv[1] == "build":
            assert all(value.encode() not in content for value in private_values)
            image = dependency if build_count == 0 else final
            build_count += 1
            images.append(image)
            return image.encode() + b"\n"
        if argv[1:3] == ["image", "ls"]:
            return ("\n".join(images) + ("\n" if images else "")).encode()
        if argv[1:3] == ["image", "rm"]:
            if argv[3] == dependency and final in images:
                raise ValueError("dependency still has a child")
            images.remove(argv[3])
            return b""
        raise AssertionError(argv)

    def stage(_docker, _image, stage, content, *, nonce, owner):
        nonlocal consumer_request
        assert len(nonce) == 32
        assert owner == nonce
        assert all(value.encode() not in content for value in private_values)
        if stage == "consume":
            consumer_request = json.loads(content)
        return artifact

    monkeypatch.setattr(immutable_ci.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(immutable_ci, "_command", command)
    monkeypatch.setattr(immutable_ci, "_run_stage", stage)

    result = immutable_ci.run_immutable_check(
        repo,
        at=NOW,
        trust_provider=provider,
        state_tip=state_tip,
    )

    assert result.canonical_bytes() == artifact
    assert consumer_request is not None
    assert set(consumer_request) == {"at", "baseline_digest", "result", "state_tip"}
    assert consumer_request["state_tip"] == state_tip
    assert images == []


def test_owned_janitor_removes_containers_before_images_with_bounded_exact_ids(monkeypatch):
    from intent_engineering.integrations import immutable_ci

    container = "1" * 64
    dependency = "sha256:" + "2" * 64
    child = "sha256:" + "3" * 64
    containers = [container]
    images = [dependency, child]
    commands: list[list[str]] = []

    def command(argv, **_options):
        commands.append(argv)
        if argv[1:3] == ["ps", "--all"]:
            return ("\n".join(containers) + ("\n" if containers else "")).encode()
        if argv[1:3] == ["rm", "--force"]:
            containers.remove(argv[3])
            return b""
        if argv[1:3] == ["image", "ls"]:
            assert not containers
            return ("\n".join(images) + ("\n" if images else "")).encode()
        if argv[1:3] == ["image", "rm"]:
            if argv[3] == dependency and child in images:
                raise ValueError("dependency still has a child")
            images.remove(argv[3])
            return b""
        raise AssertionError(argv)

    monkeypatch.setattr(immutable_ci.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(immutable_ci, "_command", command)

    immutable_ci.cleanup_ephemeral_ci("acme/project:123456789:2")

    assert containers == []
    assert images == []
    assert commands[0] == [
        "/usr/bin/docker",
        "ps",
        "--all",
        "--quiet",
        "--no-trunc",
        "--filter",
        "label=intent.ephemeral-ci=acme/project:123456789:2",
    ]
    first_image_inventory = next(
        index for index, argv in enumerate(commands) if argv[1:3] == ["image", "ls"]
    )
    assert all(argv[1:3] != ["image", "rm"] for argv in commands[:first_image_inventory])
    assert any(argv[1:3] == ["rm", "--force"] for argv in commands[:first_image_inventory])
    assert all("prune" not in argv for argv in commands)


def test_stage_labels_containers_with_exact_job_owner_and_separate_nonce(monkeypatch):
    from intent_engineering.integrations import immutable_ci

    commands: list[list[str]] = []

    def command(argv, **_options):
        commands.append(argv)
        if argv[1] == "run":
            return b"bounded result"
        if argv[1:3] == ["rm", "--force"]:
            return b""
        if argv[1:3] == ["ps", "--all"]:
            return b""
        raise AssertionError(argv)

    monkeypatch.setattr(immutable_ci, "_command", command)

    assert (
        immutable_ci._run_stage(
            "docker",
            "sha256:" + "1" * 64,
            "test",
            b"request",
            nonce="2" * 32,
            owner="acme/project:123456789:2",
        )
        == b"bounded result"
    )
    run = commands[0]
    assert "--label=intent.ephemeral-ci=acme/project:123456789:2" in run
    assert "--label=intent.ephemeral-ci.nonce=" + "2" * 32 in run


@pytest.mark.parametrize(
    "inventory",
    [
        b"short\n",
        (("4" * 64 + "\n") * 65).encode(),
        (("5" * 64 + "\n") * 2).encode(),
    ],
)
def test_owned_janitor_fails_closed_before_removing_invalid_or_unbounded_inventory(
    monkeypatch, inventory
):
    from intent_engineering.integrations import immutable_ci

    commands: list[list[str]] = []

    def command(argv, **_options):
        commands.append(argv)
        if argv[1:3] == ["ps", "--all"]:
            return inventory
        raise AssertionError("janitor attempted deletion from invalid inventory")

    monkeypatch.setattr(immutable_ci.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(immutable_ci, "_command", command)

    with pytest.raises(ValueError, match="^immutable CI unavailable$"):
        immutable_ci.cleanup_ephemeral_ci("acme/project:123456789:2")

    assert len(commands) == 1


@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGTERM])
def test_termination_signal_is_translated_without_traceback_by_the_python_entry_process(
    tmp_path, termination_signal
):
    root = Path(__file__).parents[2]
    program = f"""
import os
import signal
from intent_engineering.integrations.immutable_ci import _termination_as_exception

try:
    with _termination_as_exception():
        os.kill(os.getpid(), {termination_signal})
except ValueError as error:
    assert str(error) == "immutable CI unavailable"
    raise SystemExit(23)
raise SystemExit(24)
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 23
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_runner_images_are_removed_when_execution_is_cancelled(tmp_path, monkeypatch):
    from intent_engineering.integrations import immutable_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    provider, _private_values = _runner_local_provider(tmp_path, monkeypatch, repo)
    state_tip = git(repo, "rev-parse", "refs/remotes/origin/intent-state").decode().strip()
    dependency = "sha256:" + "4" * 64
    final = "sha256:" + "5" * 64
    images: list[str] = []
    build_count = 0

    def command(argv, *, content=b"", **_options):
        nonlocal build_count
        if argv[1] == "build":
            image = dependency if build_count == 0 else final
            build_count += 1
            images.append(image)
            return image.encode() + b"\n"
        if argv[1:3] == ["image", "ls"]:
            return ("\n".join(images) + ("\n" if images else "")).encode()
        if argv[1:3] == ["image", "rm"]:
            images.remove(argv[3])
            return b""
        raise AssertionError(argv)

    monkeypatch.setattr(immutable_ci.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(immutable_ci, "_command", command)
    monkeypatch.setattr(
        immutable_ci,
        "_run_stage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        immutable_ci.run_immutable_check(
            repo,
            at=NOW,
            trust_provider=provider,
            state_tip=state_tip,
        )
    assert images == []
    assert not (repo / ".intent-ci/test-results.json").exists()


def test_post_build_failure_traceback_retains_no_authenticated_plaintext(tmp_path, monkeypatch):
    from intent_engineering.integrations import immutable_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    provider, private_values = _runner_local_provider(tmp_path, monkeypatch, repo)
    state_tip = git(repo, "rev-parse", "refs/remotes/origin/intent-state").decode().strip()
    dependency = "sha256:" + "4" * 64
    images = [dependency]
    builds = 0

    def command(argv, *, content=b"", **_options):
        nonlocal builds
        if argv[1] == "build":
            builds += 1
            if builds == 1:
                return dependency.encode() + b"\n"
            assert b"Approved shared-state baseline" in content
            raise RuntimeError("after context creation")
        if argv[1:3] == ["image", "ls"]:
            return ("\n".join(images) + ("\n" if images else "")).encode()
        if argv[1:3] == ["image", "rm"]:
            images.remove(argv[3])
            return b""
        raise AssertionError(argv)

    monkeypatch.setattr(immutable_ci.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(immutable_ci, "_command", command)
    with pytest.raises(ValueError, match="^immutable CI unavailable$") as caught:
        immutable_ci.run_immutable_check(
            repo,
            at=NOW,
            trust_provider=provider,
            state_tip=state_tip,
        )

    private_markers = tuple(value.encode() for value in private_values)
    for frame, _line in traceback.walk_tb(caught.value.__traceback__):
        if frame.f_globals.get("__name__") != "intent_engineering.integrations.immutable_ci":
            continue
        assert provider not in frame.f_locals.values()
        retained = repr(frame.f_locals).encode()
        assert all(marker not in retained for marker in private_markers)
        assert b"Approved shared-state baseline" not in retained
    assert images == []


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
                assert b'"trust"' not in options["content"]
                assert secret not in options["content"]
                assert b'"baseline_digest"' in options["content"]
                assert b'"state_tip"' in options["content"]
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
