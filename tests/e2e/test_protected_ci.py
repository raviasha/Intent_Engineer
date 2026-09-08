"""Protected tooling reads proposed Git objects but never checks out/imports PR code."""

import io
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.e2e.test_test_evidence_bindings import _restored
from tests.helpers.shared_state import NOW, git


def _request(trust, base, proposed):
    repository = trust.repository_id.removeprefix("github.com/")
    event = {
        "repository": {"full_name": repository, "default_branch": "main"},
        "number": 17,
        "pull_request": {
            "head": {"sha": proposed},
            "base": {"ref": "main", "repo": {"full_name": repository}},
        },
    }
    environment = {
        "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_REPOSITORY": repository,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_RUN_ID": "123456789",
        "INTENT_CI_TOOLING_SHA": base,
    }
    return event, environment


def test_state_validator_uses_default_tooling_but_exact_state_target(tmp_path, monkeypatch):
    from intent_engineering.integrations.protected_ci import load_request

    repo, trust = _restored(tmp_path, monkeypatch)
    tooling = git(repo, "rev-parse", "HEAD").decode().strip()
    event, environment = _request(trust, tooling, "a" * 40)
    event["pull_request"]["base"].update({"ref": "intent-state", "sha": "b" * 40})
    request = load_request(repo, event, environment, state=True)
    assert request.revision == "a" * 40
    assert request.base == "b" * 40
    assert git(repo, "rev-parse", "HEAD").decode().strip() == tooling
    event["pull_request"]["base"]["ref"] = "main"
    with pytest.raises(ValueError):
        load_request(repo, event, environment, state=True)


def test_proposed_backend_and_package_are_only_git_data(tmp_path, monkeypatch):
    from intent_engineering.integrations.immutable_ci import build_context
    from intent_engineering.integrations.protected_ci import load_request

    repo, trust = _restored(tmp_path, monkeypatch)
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    trap = "raise AssertionError('proposed build/import executed on trusted host')\n"
    (repo / "trap.py").write_text(trap)
    (repo / "pyproject.toml").write_text(
        '[build-system]\nrequires=[]\nbuild-backend="trap"\nbackend-path=["."]\n'
    )
    git(repo, "add", "trap.py", "pyproject.toml")
    git(repo, "commit", "-qm", "untrusted proposed backend")
    proposed = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "checkout", "--detach", base)
    event, environment = _request(trust, base, proposed)
    request = load_request(repo, event, environment)
    context = build_context(repo, at=NOW, revision=request.revision)
    with tarfile.open(fileobj=io.BytesIO(context)) as archive:
        assert archive.extractfile("repository/trap.py").read() == trap.encode()
        assert archive.extractfile("repository/.git/HEAD").read() == proposed.encode() + b"\n"
    assert git(repo, "rev-parse", "HEAD").decode().strip() == base
    assert not (repo / "trap.py").exists()


@pytest.mark.parametrize("changed", ["event", "tooling", "ref", "base", "revision"])
def test_unprotected_or_ambiguous_execution_context_fails_closed(tmp_path, monkeypatch, changed):
    from intent_engineering.integrations.protected_ci import load_request

    repo, trust = _restored(tmp_path, monkeypatch)
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    event, environment = _request(trust, base, base)
    if changed == "event":
        environment["GITHUB_EVENT_NAME"] = "pull_request"
    elif changed == "tooling":
        environment["INTENT_CI_TOOLING_SHA"] = "0" * 40
    elif changed == "ref":
        environment["GITHUB_REF"] = "refs/heads/unprotected"
    elif changed == "base":
        event["pull_request"]["base"]["ref"] = "unprotected"
    else:
        event["pull_request"]["head"]["sha"] = "--upload-pack=trap"
    with pytest.raises(ValueError, match="^protected CI unavailable$"):
        load_request(repo, event, environment)


def test_code_fetch_explicitly_fetches_and_returns_the_exact_state_tip(tmp_path, monkeypatch):
    from intent_engineering.integrations import protected_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    monkeypatch.delenv("INTENT_CI_SHARED_STATE_TRUST")
    revision = "a" * 40
    state_tip = "b" * 40
    request = protected_ci.ProtectedRequest(revision, "acme/project", 17)
    observed: list[str] = []

    class Process:
        pid = 1

        def wait(self, timeout):
            return 0

        def poll(self):
            return 0

    def start(argv, **_options):
        observed.extend(argv)
        return Process()

    def read_ref(_root, arguments, _maximum):
        if arguments[-1] == "refs/intent-ci/proposed":
            return revision.encode() + b"\n"
        if arguments[-1] == "refs/remotes/origin/intent-state":
            return state_tip.encode() + b"\n"
        raise AssertionError(arguments)

    monkeypatch.setattr(protected_ci.subprocess, "Popen", start)
    monkeypatch.setattr(protected_ci, "_git", read_ref)
    monkeypatch.setattr(protected_ci, "_pin_git_executable", lambda: object())
    monkeypatch.setattr(protected_ci, "_git_pin_matches", lambda _pin: True)

    assert protected_ci.fetch_proposed_revision(repo, request, "token") == state_tip
    assert "+refs/heads/intent-state:refs/remotes/origin/intent-state" in observed


def test_manual_check_fetches_only_the_bounded_state_ref(tmp_path, monkeypatch):
    from intent_engineering.integrations import protected_ci

    repo, _trust = _restored(tmp_path, monkeypatch)
    monkeypatch.delenv("INTENT_CI_SHARED_STATE_TRUST")
    state_tip = "b" * 40
    request = protected_ci.ProtectedRequest("a" * 40, "acme/project", None)
    observed: list[str] = []

    class Process:
        pid = 1

        def wait(self, timeout):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(
        protected_ci.subprocess,
        "Popen",
        lambda argv, **_options: (observed.extend(argv), Process())[1],
    )
    monkeypatch.setattr(
        protected_ci,
        "_git",
        lambda _root, arguments, _maximum: (
            state_tip.encode() + b"\n"
            if arguments[-1] == "refs/remotes/origin/intent-state"
            else (_ for _ in ()).throw(AssertionError(arguments))
        ),
    )
    monkeypatch.setattr(protected_ci, "_pin_git_executable", lambda: object())
    monkeypatch.setattr(protected_ci, "_git_pin_matches", lambda _pin: True)

    assert protected_ci.fetch_proposed_revision(repo, request, "token") == state_tip
    assert "+refs/heads/intent-state:refs/remotes/origin/intent-state" in observed
    assert not any("refs/pull/" in str(item) for item in observed)


def test_code_check_resolves_runner_local_trust_and_rejects_legacy_private_json(
    tmp_path, monkeypatch
):
    from intent_engineering.integrations import protected_ci

    repo, trust = _restored(tmp_path, monkeypatch)
    tooling = git(repo, "rev-parse", "HEAD").decode().strip()
    event, environment = _request(trust, tooling, tooling)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    git(repo, "update-ref", "refs/intent-ci/proposed", tooling)
    for key, value in {
        **environment,
        "GITHUB_EVENT_PATH": str(event_path),
        "INTENT_CI_TRUST_PATH": str(tmp_path / "protected" / "trust.json"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["launch.py", "check"])
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("legacy trust reached immutable execution")

    monkeypatch.setattr(protected_ci, "run_immutable_check", forbidden)
    assert protected_ci.main(repo) == 1
    assert called is False

    marker = object()
    monkeypatch.delenv("INTENT_CI_SHARED_STATE_TRUST")
    trust_calls = []

    def runner_trust(root):
        trust_calls.append(root)
        return marker if root == repo else None

    monkeypatch.setattr(
        protected_ci,
        "ci_trust_from_environment",
        runner_trust,
    )
    state_tip = git(repo, "rev-parse", "refs/remotes/origin/intent-state").decode().strip()
    captured = {}

    def run(root, *, at, revision, trust_provider, state_tip, owner):
        captured.update(
            root=root,
            at=at,
            revision=revision,
            trust_provider=trust_provider,
            state_tip=state_tip,
            owner=owner,
        )

    monkeypatch.setattr(protected_ci, "run_immutable_check", run)
    result = protected_ci.main(repo)
    assert trust_calls, result
    assert captured, result
    assert result == 0
    assert captured["revision"] == tooling
    assert captured["trust_provider"] is marker
    assert captured["state_tip"] == state_tip
    assert captured["owner"] == "acme/project:123456789:2"


def test_cleanup_command_uses_only_the_trusted_owned_resource_janitor(tmp_path, monkeypatch):
    from intent_engineering.integrations import protected_ci

    repo, trust = _restored(tmp_path, monkeypatch)
    tooling = git(repo, "rev-parse", "HEAD").decode().strip()
    event, environment = _request(trust, tooling, tooling)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    for key, value in {
        **environment,
        "GITHUB_EVENT_PATH": str(event_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("INTENT_CI_SHARED_STATE_TRUST")
    monkeypatch.setattr("sys.argv", ["launch.py", "cleanup"])
    calls: list[str] = []
    monkeypatch.setattr(protected_ci, "cleanup_ephemeral_ci", calls.append)
    monkeypatch.setattr(
        protected_ci,
        "ci_trust_from_environment",
        lambda _root: (_ for _ in ()).throw(AssertionError("cleanup loaded private trust")),
    )

    assert protected_ci.main(repo) == 0
    assert calls == ["acme/project:123456789:2"]


@pytest.mark.parametrize(
    ("name", "value"),
    [("GITHUB_RUN_ID", "other-job"), ("GITHUB_RUN_ATTEMPT", "0")],
)
def test_cleanup_rejects_an_unbound_actions_job_owner(tmp_path, monkeypatch, name, value):
    from intent_engineering.integrations import protected_ci

    repo, trust = _restored(tmp_path, monkeypatch)
    tooling = git(repo, "rev-parse", "HEAD").decode().strip()
    event, environment = _request(trust, tooling, tooling)
    environment[name] = value
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    for key, item in {**environment, "GITHUB_EVENT_PATH": str(event_path)}.items():
        monkeypatch.setenv(key, item)
    monkeypatch.setattr("sys.argv", ["launch.py", "cleanup"])
    monkeypatch.setattr(
        protected_ci,
        "cleanup_ephemeral_ci",
        lambda _owner: (_ for _ in ()).throw(AssertionError("unbound cleanup started")),
    )

    assert protected_ci.main(repo) == 1


@pytest.mark.skipif(shutil.which("uv") is None, reason="offline wheel-build tool unavailable")
def test_installed_wheel_contains_the_protected_dependency_lock(tmp_path):
    root = Path(__file__).parents[2]
    subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=60,
    )
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        for name in ("ci-runtime.lock", "intent-state.yml", "intent-check.yml"):
            assert (
                archive.read("intent_engineering/integrations/" + name)
                == (root / "src/intent_engineering/integrations" / name).read_bytes()
            )
