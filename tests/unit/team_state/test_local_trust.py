"""Public local enrollment survives restarts without exporting private keys."""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime

import keyring
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.team_state.keys import (
    GitHubIdentity,
    KeyringRecipientKeyStore,
    RecipientEnrollmentBinding,
)
from intent_engineering.team_state.restore import TRUST_ENVIRONMENT_VARIABLE


class Backend:
    """Only the external credential service is replaced; crypto and stores are real."""

    def __init__(self):
        self.values = {}
        self.failure = None

    def get_password(self, service, account):
        if self.failure is not None:
            raise self.failure
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[service, account] = value

    def delete_password(self, service, account):
        del self.values[service, account]


@pytest.fixture
def enrolled(tmp_path, monkeypatch):
    root = tmp_path / "alpha"
    root.mkdir()
    initialize_project(root)
    runtime = load_runtime(root)
    binding = RecipientEnrollmentBinding(
        project_id=runtime.config.project_id,
        repository_id="github.com/acme/alpha",
        actor="local:asha",
        github_identity=GitHubIdentity(account_id="101", login="asha"),
        webauthn_credential_id="Y3JlZGVudGlhbA",
        webauthn_credential_public_key="cHVibGljLWtleS1tYXRlcmlhbA",
        enrolled_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )
    backend = Backend()
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    store = KeyringRecipientKeyStore(binding, private_key_source=lambda: b"s" * 32)
    recipient = store.generate(binding.project_id, binding.actor)
    public = Ed25519PrivateKey.from_private_bytes(b"e" * 32).public_key().public_bytes_raw()
    yield runtime, recipient, {"signer:asha": public}, backend
    runtime.close()


def test_durable_public_trust_recovers_keyring_recipient_after_restart(enrolled):
    """Catches missing binding persistence and private keys leaking into local JSON."""
    from intent_engineering.team_state.local_trust import (
        LocalTrustProvider,
        load_local_trust,
        save_local_trust,
    )

    runtime, recipient, keys, _ = enrolled
    save_local_trust(runtime, recipient, keys)
    path = runtime.workspace / "team-trust.json"
    before = path.stat()
    content = path.read_bytes()
    save_local_trust(runtime, recipient, keys)
    assert path.stat().st_ino == before.st_ino
    assert path.read_bytes() == content
    assert base64.b64encode(b"s" * 32).rstrip(b"=") not in content
    assert b"private" not in content
    config = load_local_trust(runtime.root)
    assert config.recipient == recipient
    trust = LocalTrustProvider(runtime.root).load()
    assert trust.recipient_private_key == b"s" * 32
    assert trust.recipient_key_id == recipient.key_id
    assert trust.repository_id == "github.com/acme/alpha"
    assert trust.signing_keys[0].public_key == keys["signer:asha"]


def test_missing_trust_is_read_only_and_restore_bootstrap_needs_no_canonical_files(enrolled):
    """Catches canonical-runtime loading during bootstrap and missing-file creation."""
    from intent_engineering.team_state.local_trust import (
        LocalTrustProvider,
        load_local_trust,
        save_local_trust,
    )

    runtime, recipient, keys, _ = enrolled
    untouched = runtime.root / "fresh"
    untouched.mkdir()
    assert load_local_trust(untouched) is None
    assert list(untouched.iterdir()) == []
    save_local_trust(runtime, recipient, keys)
    bootstrap = runtime.root / "bootstrap"
    (bootstrap / ".intent").mkdir(parents=True)
    (bootstrap / ".intent/team-trust.json").write_bytes(
        (runtime.workspace / "team-trust.json").read_bytes()
    )
    assert LocalTrustProvider(bootstrap).load().recipient_private_key == b"s" * 32


@pytest.mark.parametrize(
    "change",
    [
        "project",
        "repository",
        "recipient_id",
        "binding",
        "unknown",
        "duplicate",
        "oversize",
        "signer_id",
        "signer_key",
        "empty_keys",
        "boolean_schema",
    ],
)
def test_tampered_trust_fails_closed_without_sensitive_diagnostics(enrolled, change):
    """Catches foreign bindings, JSON ambiguity, unbounded inputs and unreviewed keys."""
    from intent_engineering.team_state.local_trust import (
        LocalTrustError,
        load_local_trust,
        save_local_trust,
    )

    runtime, recipient, keys, _ = enrolled
    save_local_trust(runtime, recipient, keys)
    path = runtime.workspace / "team-trust.json"
    document = json.loads(path.read_bytes())
    if change == "project":
        document["project_id"] = "foreign"
    elif change == "repository":
        document["repository_id"] = "github.com/other/repo"
    elif change == "recipient_id":
        document["recipient_key_id"] = "recipient:sha256:" + "0" * 64
    elif change == "binding":
        document["recipient"]["github_account_id"] = "999"
    elif change == "unknown":
        document["private_key"] = "secret-sentinel"
    elif change == "signer_id":
        document["signing_public_keys"] = {"bad key": "A" * 44}
    elif change == "signer_key":
        document["signing_public_keys"] = {"signer:asha": "secret-sentinel"}
    elif change == "empty_keys":
        document["signing_public_keys"] = {}
    elif change == "boolean_schema":
        document["schema_version"] = True
    encoded = json.dumps(document).encode()
    if change == "duplicate":
        encoded = encoded[:-1] + b',"project_id":"secret-sentinel"}'
    if change == "oversize":
        encoded += b" " * 65536
    path.write_bytes(encoded)
    with pytest.raises(LocalTrustError) as caught:
        load_local_trust(runtime.root)
    assert "secret-sentinel" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "workspace_symlink"])
def test_unsafe_trust_paths_are_rejected(enrolled, kind):
    """Catches following redirected files or blocking on special files."""
    from intent_engineering.team_state.local_trust import (
        LocalTrustError,
        load_local_trust,
        save_local_trust,
    )

    runtime, recipient, keys, _ = enrolled
    save_local_trust(runtime, recipient, keys)
    path = runtime.workspace / "team-trust.json"
    source = runtime.root / "original.json"
    path.rename(source)
    if kind == "symlink":
        path.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        runtime.workspace.rename(runtime.root / "moved")
        runtime.workspace.symlink_to(runtime.root / "moved", target_is_directory=True)
    with pytest.raises(LocalTrustError):
        load_local_trust(runtime.root)


def test_conflicting_save_preserves_reviewed_trust(enrolled):
    """Catches silently replacing a previously reviewed signing key."""
    from intent_engineering.team_state.local_trust import LocalTrustError, save_local_trust

    runtime, recipient, keys, _ = enrolled
    save_local_trust(runtime, recipient, keys)
    path = runtime.workspace / "team-trust.json"
    content = path.read_bytes()
    with pytest.raises(LocalTrustError):
        save_local_trust(runtime, recipient, {"signer:asha": b"x" * 32})
    assert path.read_bytes() == content


def test_missing_keyring_key_is_unavailable_and_cancellation_is_preserved(enrolled):
    """Catches regenerating lost recipient keys and swallowing cancellation."""
    from intent_engineering.team_state.local_trust import LocalTrustProvider, save_local_trust

    runtime, recipient, keys, backend = enrolled
    save_local_trust(runtime, recipient, keys)
    backend.values.clear()
    assert LocalTrustProvider(runtime.root).load() is None
    backend.failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught:
        LocalTrustProvider(runtime.root).load()
    assert caught.value.__context__ is None


def test_explicit_environment_takes_precedence_even_when_empty(enrolled):
    """Catches silently falling back to local trust after invalid explicit CI trust."""
    from intent_engineering.team_state.local_trust import (
        local_or_environment_trust,
        save_local_trust,
    )

    runtime, recipient, keys, _ = enrolled
    save_local_trust(runtime, recipient, keys)
    assert local_or_environment_trust(runtime.root, {}).load().recipient_private_key == b"s" * 32
    with pytest.raises(ValueError):
        local_or_environment_trust(runtime.root, {TRUST_ENVIRONMENT_VARIABLE: ""}).load()


def test_loading_absent_trust_never_creates_lock_files(enrolled):
    """Catches read-only status checks modifying an existing workspace."""
    from intent_engineering.team_state.local_trust import load_local_trust

    runtime, _, _, _ = enrolled
    before = set(runtime.workspace.iterdir())
    assert load_local_trust(runtime.root) is None
    assert set(runtime.workspace.iterdir()) == before


def test_replaced_private_key_cannot_inherit_reviewed_recipient(enrolled):
    """Catches trusting a replacement secret under the original enrollment key ID."""
    from intent_engineering.team_state.local_trust import (
        LocalTrustError,
        LocalTrustProvider,
        save_local_trust,
    )

    runtime, recipient, keys, backend = enrolled
    save_local_trust(runtime, recipient, keys)
    backend.values["intent-engineering/alpha", recipient.key_id] = (
        base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
    )
    with pytest.raises(LocalTrustError):
        LocalTrustProvider(runtime.root).load()


def test_project_config_cannot_be_rebound_to_another_project(enrolled):
    """Catches accepting a valid enrollment from a different local canonical project."""
    from intent_engineering.team_state.local_trust import (
        LocalTrustError,
        load_local_trust,
        save_local_trust,
    )

    runtime, recipient, keys, _ = enrolled
    save_local_trust(runtime, recipient, keys)
    config = runtime.workspace / "config.yaml"
    config.write_text(config.read_text().replace("project_id: alpha", "project_id: foreign"))
    with pytest.raises(LocalTrustError):
        load_local_trust(runtime.root)


def test_trust_round_trips_when_project_has_declared_source_roles(enrolled):
    """Catches rejecting valid serialized enum roles in an onboarded project config."""
    from intent_engineering.team_state.local_trust import load_local_trust, save_local_trust

    runtime, recipient, keys, _ = enrolled
    document = runtime.config.model_dump(mode="json")
    document["source_roles"] = [
        {
            "connector_id": "markdown",
            "scope": "docs/prd.md",
            "role": "declared_intent",
            "inherited": False,
        }
    ]
    (runtime.workspace / "config.yaml").write_text(json.dumps(document))
    save_local_trust(runtime, recipient, keys)
    assert load_local_trust(runtime.root).recipient == recipient
