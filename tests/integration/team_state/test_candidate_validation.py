"""Read-only protected tooling validates inert candidate Git objects."""

import base64
import json

import pytest
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from tests.helpers.shared_state import (
    canonical_files,
    git,
    init_repository,
    install_state_ref,
    keys,
    ready_project,
)


@pytest.mark.parametrize("change", [None, "base", "extra", "signature"])
def test_validator_requires_exact_linear_signed_artifacts_and_never_restores(
    tmp_path, monkeypatch, change
):
    source = tmp_path / "source" / "project"
    source.mkdir(parents=True)
    ready_project(source)
    repo = init_repository(tmp_path / "target" / "project")
    _recipient, signer, _trust = keys()
    import keyring

    from intent_engineering.team_state.ci import CiKeyStore, CiTrustConfig
    from intent_engineering.team_state.restore import build_state_payload, seal_state_payload
    from tests.helpers.shared_state import NOW
    from tests.unit.team_state.test_ci_recipient import Backend

    backend = Backend()
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    machine = CiKeyStore(
        "project",
        "github.com/acme/project",
        "release-01",
        backend=backend,
        lock_root=tmp_path / "ci-locks",
    ).provision()
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    config_path = protected / "trust.json"
    config_path.write_bytes(
        CiTrustConfig(
            recipient=machine,
            signing_public_keys={
                "signer:release": base64.b64encode(signer.public_key().public_bytes_raw()).decode()
            },
        ).canonical_bytes()
    )
    config_path.chmod(0o600)
    tree = git(repo, "mktree", input_bytes=b"").decode().strip()
    base = git(repo, "commit-tree", tree, input_bytes=b"bootstrap\n").decode().strip()
    release = seal_state_payload(
        build_state_payload(canonical_files(source)),
        project_id="project",
        repository_id="github.com/acme/project",
        graph_version=1,
        parent_bundle_digest=None,
        created_at=NOW,
        recipient_public_keys={machine.key_id: base64.urlsafe_b64decode(machine.public_key + "=")},
        signing_private_keys={"signer:release": signer.private_bytes_raw()},
    )
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
        env={"INTENT_CI_TRUST_PATH": str(config_path)},
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
            "INTENT_CI_TRUST_PATH": str(config_path),
        }.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr("sys.argv", ["launch.py", "validate-state"])
        assert main(repo) == 0
        assert not (repo / ".intent").exists()
        backend.values.clear()
        missing = CliRunner().invoke(
            app,
            ["team", "validate-state", "--project", str(repo), "--base", base, "--head", head],
            env={"INTENT_CI_TRUST_PATH": str(config_path)},
        )
        assert missing.exit_code == 1
        assert "intent team ci provision" in missing.output
