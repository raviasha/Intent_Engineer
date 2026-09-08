"""Normal developer entrypoints restore authentic team state from local enrollment."""

from __future__ import annotations

import base64
import json
import os
import socket
from pathlib import Path

import keyring
import pytest
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from intent_engineering.cli import dev as dev_cli
from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.team_state import restore as restore_module
from intent_engineering.team_state.keys import (
    GitHubIdentity,
    KeyringRecipientKeyStore,
    RecipientEnrollmentBinding,
)
from intent_engineering.team_state.local_trust import LocalTrustProvider, save_local_trust
from intent_engineering.team_state.restore import (
    TRUST_ENVIRONMENT_VARIABLE,
    SharedStateRestoreStatus,
    build_state_payload,
    seal_state_payload,
)
from tests.helpers.shared_state import (
    NOW,
    REPOSITORY_ID,
    SIGNER_ID,
    canonical_files,
    init_repository,
    install_state_ref,
    ready_project,
    trust_environment,
)


@pytest.fixture
def local_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    """Replace only the OS keyring and GitHub transport; all crypto and Git are real."""
    request.addfinalizer(structlog.reset_defaults)
    monkeypatch.delenv(TRUST_ENVIRONMENT_VARIABLE, raising=False)
    monkeypatch.setattr(dev_cli, "_governance_registry_root", lambda: tmp_path / "governance")
    root = init_repository(tmp_path / "project")
    ready_project(root)
    credentials = {}
    monkeypatch.setattr(
        keyring, "get_password", lambda service, account: credentials.get((service, account))
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, account, value: credentials.__setitem__((service, account), value),
    )
    binding = RecipientEnrollmentBinding(
        project_id="project",
        repository_id=REPOSITORY_ID,
        actor="local:owner",
        github_identity=GitHubIdentity(account_id="101", login="owner"),
        webauthn_credential_id="Y3JlZGVudGlhbA",
        webauthn_credential_public_key="cHVibGljLWtleS1tYXRlcmlhbA",
        enrolled_at=NOW,
    )
    store = KeyringRecipientKeyStore(binding, private_key_source=lambda: b"s" * 32)
    recipient = store.generate("project", "local:owner")
    signer = Ed25519PrivateKey.from_private_bytes(b"e" * 32)
    runtime = load_runtime(root)
    try:
        save_local_trust(runtime, recipient, {SIGNER_ID: signer.public_key().public_bytes_raw()})
    finally:
        runtime.close()
    trust = LocalTrustProvider(root).load()
    assert trust is not None
    release = seal_state_payload(
        build_state_payload(canonical_files(root)),
        project_id="project",
        repository_id=REPOSITORY_ID,
        graph_version=1,
        parent_bundle_digest=None,
        created_at=NOW,
        recipient_public_keys={
            recipient.key_id: base64.urlsafe_b64decode(recipient.public_key + "=")
        },
        signing_private_keys={SIGNER_ID: signer.private_bytes_raw()},
    )
    commit = install_state_ref(root, release)

    def transport(repository_id):
        if repository_id != REPOSITORY_ID:
            raise ValueError("foreign repository")
        return restore_module._GitRefReader(root)

    monkeypatch.setattr(restore_module, "_refresh_state_ref", transport)
    return root, commit, trust, credentials


def test_offline_developer_restore_uses_local_keyring_and_retains_stale_status(local_release):
    """Catches dev/ensure's early environment-only branch ignoring durable enrollment."""
    root, commit, _, _ = local_release
    result = dev_cli._shared_restore(root, refresh_remote=False)
    assert result.status is SharedStateRestoreStatus.STALE
    marker = json.loads((root / ".intent/cache/shared-state.json").read_bytes())
    assert marker["ref_commit"] == commit
    assert marker["graph_version"] == 1


def test_ensure_restores_local_team_state_before_background_launch(local_release, monkeypatch):
    """Catches normal ensure failing to authenticate its locally enrolled recipient."""
    root, commit, _, _ = local_release
    monkeypatch.setattr(dev_cli, "_start_or_reuse_background_service", lambda *_args: True)
    result = CliRunner().invoke(app, ["ensure", "--project", str(root), "--format", "json"])
    assert result.exit_code == 0, result.output
    marker = json.loads((root / ".intent/cache/shared-state.json").read_bytes())
    assert marker["ref_commit"] == commit
    assert json.loads(result.stdout)["status"] == "ready"


def test_direct_dev_starts_with_authenticated_local_team_state(local_release, monkeypatch):
    """Catches the foreground dev command starting as local-only after team enrollment."""
    root, commit, _, _ = local_release
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
    except PermissionError:
        pytest.skip("host policy forbids the control-plane listener")
    finally:
        listener.close()
    observed = []
    monkeypatch.setattr(
        dev_cli, "_wait_for_exit", lambda started: observed.append(started.service.status())
    )
    result = CliRunner().invoke(app, ["dev", "--project", str(root), "--no-open"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert observed[0]["status"] == "ready"
    assert (
        json.loads((root / ".intent/cache/shared-state.json").read_bytes())["ref_commit"] == commit
    )


def test_automatic_child_reconstructs_local_keyring_without_private_provenance(
    local_release, monkeypatch
):
    """Catches automatic children treating the local launch frame as authority-free forever."""
    root, commit, _, _ = local_release
    packet = dev_cli._background_provenance_bytes()
    assert len(packet) == dev_cli._PROVENANCE_HEADER_BYTES
    assert TRUST_ENVIRONMENT_VARIABLE not in dev_cli._background_environment()
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, packet)
    finally:
        os.close(write_fd)
    monkeypatch.setattr(dev_cli, "_inherited_provenance_descriptors", lambda: (read_fd,))
    result = dev_cli._automatic_shared_restore(root)
    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert (
        json.loads((root / ".intent/cache/shared-state.json").read_bytes())["ref_commit"] == commit
    )


def test_plain_check_restores_local_team_state_through_its_readiness_gate(local_release):
    """Catches plain check bypassing shared restore because require_shared is only a CI flag."""
    root, commit, _, _ = local_release
    result = CliRunner().invoke(app, ["check", "--project", str(root), "--sources", "markdown"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert (
        json.loads((root / ".intent/cache/shared-state.json").read_bytes())["ref_commit"] == commit
    )


@pytest.mark.parametrize("command", ["ensure", "check"])
def test_invalid_explicit_environment_never_falls_back_to_valid_local_trust(
    local_release, monkeypatch, command
):
    """Catches an explicit invalid trust override inheriting local recipient authority."""
    root, _, _, _ = local_release
    monkeypatch.setenv(TRUST_ENVIRONMENT_VARIABLE, "{")
    monkeypatch.setattr(dev_cli, "_start_or_reuse_background_service", lambda *_args: True)
    result = CliRunner().invoke(app, [command, "--project", str(root), "--format", "json"])
    assert not (root / ".intent/cache/shared-state.json").exists()
    payload = json.loads(result.stdout)
    assert (payload.get("readiness_status") or payload.get("status")) == "shared_state_invalid"


def test_ci_check_requires_explicit_environment_even_with_local_enrollment(local_release):
    """Catches machine CI silently using a developer's OS-keyring identity."""
    root, _, _, _ = local_release
    result = CliRunner().invoke(
        app, ["check", "--ci", "--project", str(root), "--sources", "markdown"]
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["readiness_status"] == "shared_state_unavailable"
    assert not (root / ".intent/cache/shared-state.json").exists()


def test_explicit_environment_can_restore_when_local_keyring_is_missing(local_release, monkeypatch):
    """Catches local keyring availability overriding deliberately supplied trust."""
    root, commit, trust, credentials = local_release
    credentials.clear()
    monkeypatch.setenv(TRUST_ENVIRONMENT_VARIABLE, trust_environment(trust))
    assert dev_cli._shared_restore(root).status is SharedStateRestoreStatus.VERIFIED
    assert (
        json.loads((root / ".intent/cache/shared-state.json").read_bytes())["ref_commit"] == commit
    )


def test_missing_local_keyring_fails_closed_before_check_capture(local_release):
    """Catches treating established local enrollment as ungoverned after keyring loss."""
    root, _, _, credentials = local_release
    credentials.clear()
    before = (root / ".intent/evidence/evidence.jsonl").read_bytes()
    result = CliRunner().invoke(app, ["check", "--project", str(root), "--sources", "markdown"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["readiness_status"] == "shared_state_unavailable"
    assert (root / ".intent/evidence/evidence.jsonl").read_bytes() == before
