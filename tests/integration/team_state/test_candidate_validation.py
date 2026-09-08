"""Read-only protected tooling validates inert candidate Git objects."""

import json

import pytest
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.team_state.restore import TRUST_ENVIRONMENT_VARIABLE
from tests.helpers.shared_state import (
    artifacts,
    canonical_files,
    git,
    init_repository,
    install_state_ref,
    keys,
    ready_project,
    trust_environment,
)


@pytest.mark.parametrize("change", [None, "base", "extra", "signature"])
def test_validator_requires_exact_linear_signed_artifacts_and_never_restores(
    tmp_path, monkeypatch, change
):
    source = tmp_path / "source" / "project"
    source.mkdir(parents=True)
    ready_project(source)
    repo = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    tree = git(repo, "mktree", input_bytes=b"").decode().strip()
    base = git(repo, "commit-tree", tree, input_bytes=b"bootstrap\n").decode().strip()
    release = artifacts(canonical_files(source), recipient, signer)
    if change == "signature":
        from dataclasses import replace

        release = replace(
            release, signatures=release.signatures.replace(b'"signature":"', b'"signature":"A')
        )
    head = install_state_ref(repo, release, parent=base)
    if change == "extra":
        git(repo, "read-tree", head)
        blob = (
            git(repo, "hash-object", "-w", "--stdin", input_bytes=b"do not execute\n")
            .decode()
            .strip()
        )
        git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, "extra.py")
        tree = git(repo, "write-tree").decode().strip()
        head = git(repo, "commit-tree", tree, "-p", base, input_bytes=b"extra\n").decode().strip()
    git(repo, "update-ref", "refs/remotes/origin/intent-state", base)
    argument_base = "0" * 40 if change == "base" else base
    result = CliRunner().invoke(
        app,
        ["team", "validate-state", "--project", str(repo), "--base", argument_base, "--head", head],
        env={TRUST_ENVIRONMENT_VARIABLE: trust_environment(trust)},
    )
    assert result.exit_code == (0 if change is None else 1), result.output
    assert not (repo / ".intent").exists()
    assert git(repo, "rev-parse", "refs/remotes/origin/intent-state").decode().strip() == base
    assert "recipient_private_key" not in result.output
    if change is None:
        from intent_engineering.integrations.protected_ci import main

        tooling = git(repo, "rev-parse", "HEAD").decode().strip()
        event = {
            "repository": {"full_name": "acme/project", "default_branch": "main"},
            "number": 1,
            "pull_request": {
                "head": {"sha": head},
                "base": {"ref": "intent-state", "sha": base, "repo": {"full_name": "acme/project"}},
            },
        }
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(event))
        for key, value in {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_REPOSITORY": "acme/project",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "pull_request_target",
            "GITHUB_SERVER_URL": "https://github.com",
            "INTENT_CI_TOOLING_SHA": tooling,
            TRUST_ENVIRONMENT_VARIABLE: trust_environment(trust),
        }.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr("sys.argv", ["launch.py", "validate-state"])
        assert main(repo) == 0
        assert not (repo / ".intent").exists()
