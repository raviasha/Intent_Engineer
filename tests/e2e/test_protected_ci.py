"""Protected tooling reads proposed Git objects but never checks out/imports PR code."""

import io
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
        assert (
            archive.read("intent_engineering/integrations/ci-runtime.lock")
            == (root / "src/intent_engineering/integrations/ci-runtime.lock").read_bytes()
        )
