"""Public local enrollment survives restarts without exporting private keys."""

from __future__ import annotations

import base64
import json
import os
import stat
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


def test_versioned_trust_cancellation_is_identity_preserving_and_secret_scrubbed(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cancellation args, attributes, or dependency-frame locals retaining secrets."""
    from pathlib import Path

    from intent_engineering.storage.secure import SecureDirectory
    from intent_engineering.team_state.local_trust import LocalTrustProvider

    class Cancellation(BaseException):
        pass

    assert isinstance(tmp_path, Path)
    cancellation = Cancellation("secret-sentinel")
    cancellation.secret_marker = "secret-sentinel"

    def cancel_open(_path: object) -> SecureDirectory:
        secret_local = "secret-sentinel"
        assert secret_local
        raise cancellation

    monkeypatch.setattr(SecureDirectory, "open", cancel_open)
    with pytest.raises(Cancellation) as caught:
        LocalTrustProvider(tmp_path).load_versioned()

    assert caught.value is cancellation
    assert caught.value.args == ()
    assert caught.value.__dict__ == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    frames = []
    trace = caught.value.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_name == "cancel_open":
            frames.append(dict(trace.tb_frame.f_locals))
        trace = trace.tb_next
    assert all("secret-sentinel" not in repr(frame) for frame in frames)


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


def _v2_receipts(tmp_path):
    """Use the real enrollment ceremony so persistence tests never mirror its models."""
    from intent_engineering.team_state.authority import authority_digest
    from tests.unit.team_state.test_enrollment import _join

    _a_service, _b_service, state, invite, response = _join(tmp_path)
    return state, invite, response, authority_digest(state.authority)


def _v2_trust(state, invite, response):
    from intent_engineering.team_state.local_trust import LocalTrustConfigV2

    return LocalTrustConfigV2(
        project_id=invite.project_id,
        repository_id=invite.repository_id,
        root=invite.root,
        member_id=response.proposed_member.member_id,
        device_certificate_id="certificate:sha256:" + "e" * 64,
        recipient_key_id=response.recipient_key_id,
        signature_id=response.signature_id,
        accepted_authority_digest="sha256:" + "a" * 64,
        accepted_authority_sequence=state.authority.sequence + 1,
        accepted_bundle_digest="sha256:" + "c" * 64,
    )


def test_version_two_trust_requires_owner_only_workspace_directory(tmp_path: object) -> None:
    """Catches v2 trust inheriting the deliberately permissive v1 directory policy."""
    from pathlib import Path

    from intent_engineering.team_state.local_trust import LocalTrustError, LocalTrustProvider

    assert isinstance(tmp_path, Path)
    state, invite, response, _before = _v2_receipts(tmp_path)
    trust = _v2_trust(state, invite, response)
    root = tmp_path / "joiner"
    workspace = root / ".intent"
    workspace.mkdir(parents=True, mode=0o700)
    target = workspace / "team-trust.json"
    target.write_bytes(trust.canonical_bytes())
    target.chmod(0o600)
    workspace.chmod(0o755)

    with pytest.raises(LocalTrustError, match="local team trust unavailable"):
        LocalTrustProvider(root).load_versioned()


def test_versioned_load_never_exposes_trust_while_activation_journal_exists(
    tmp_path: object,
) -> None:
    """Catches a prepared activation exposing v2 trust before canonical state commits."""
    from pathlib import Path

    from intent_engineering.team_state.local_trust import LocalTrustError, LocalTrustProvider

    assert isinstance(tmp_path, Path)
    state, invite, response, _before = _v2_receipts(tmp_path)
    trust = _v2_trust(state, invite, response)
    root = tmp_path / "joiner"
    workspace = root / ".intent"
    workspace.mkdir(parents=True, mode=0o700)
    target = workspace / "team-trust.json"
    target.write_bytes(trust.canonical_bytes())
    target.chmod(0o600)
    (workspace / "join-activation.json").write_bytes(b"prepared")

    with pytest.raises(LocalTrustError, match="local team trust unavailable"):
        LocalTrustProvider(root).load_versioned()


@pytest.mark.parametrize("kind", ["pending", "versioned"])
def test_versioned_reads_validate_bytes_and_metadata_on_one_locked_descriptor(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Catches reopening a validated trust path after its content was already read."""
    from pathlib import Path

    from intent_engineering.team_state.local_trust import (
        LocalTrustProvider,
        PendingJoinTrustV2,
    )

    assert isinstance(tmp_path, Path)
    state, invite, response, before = _v2_receipts(tmp_path)
    root = tmp_path / "joiner"
    workspace = root / ".intent"
    workspace.mkdir(parents=True, mode=0o700)
    provider = LocalTrustProvider(root)
    if kind == "pending":
        expected = PendingJoinTrustV2(
            phase="response-ready",
            invite=invite,
            response=response,
            local_recipient_key_id=response.recipient_key_id,
            local_signature_id=response.signature_id,
            expected_root_key_id=invite.root.root_key_id,
            expected_authority_before_digest=before,
            external_write_attempted=False,
        )
        provider.save_pending_join(expected)
        filename = "team-join-pending.json"
    else:
        expected = _v2_trust(state, invite, response)
        filename = "team-trust.json"
        target = workspace / filename
        target.write_bytes(expected.canonical_bytes())
        target.chmod(0o600)

    real_open = os.open
    opens = 0

    def one_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal opens
        if path == filename:
            opens += 1
            if opens > 1:
                raise AssertionError("trust path reopened")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", one_open)
    loaded = provider.load_pending_join() if kind == "pending" else provider.load_versioned()
    assert loaded == expected
    assert opens == 1


@pytest.mark.parametrize("kind", ["pending", "versioned"])
def test_versioned_reads_reject_same_length_in_place_rewrite_with_restored_mtime(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Catches a noncooperating writer hiding a same-inode rewrite behind restored mtime."""
    from pathlib import Path

    from intent_engineering.team_state.local_trust import (
        LocalTrustError,
        LocalTrustProvider,
        PendingJoinTrustV2,
    )

    assert isinstance(tmp_path, Path)
    state, invite, response, before = _v2_receipts(tmp_path)
    root = tmp_path / "joiner"
    workspace = root / ".intent"
    workspace.mkdir(parents=True, mode=0o700)
    provider = LocalTrustProvider(root)
    if kind == "pending":
        pending = PendingJoinTrustV2(
            phase="response-ready",
            invite=invite,
            response=response,
            local_recipient_key_id=response.recipient_key_id,
            local_signature_id=response.signature_id,
            expected_root_key_id=invite.root.root_key_id,
            expected_authority_before_digest=before,
            external_write_attempted=False,
        )
        provider.save_pending_join(pending)
        target = workspace / "team-join-pending.json"
    else:
        trust = _v2_trust(state, invite, response)
        target = workspace / "team-trust.json"
        target.write_bytes(trust.canonical_bytes())
        target.chmod(0o600)

    original = target.read_bytes()
    original_metadata = target.stat()
    target_inode = original_metadata.st_ino
    real_read = os.read
    rewritten = False

    def rewrite_after_read(descriptor: int, maximum: int) -> bytes:
        nonlocal rewritten
        content = real_read(descriptor, maximum)
        if content and not rewritten and os.fstat(descriptor).st_ino == target_inode:
            rewritten = True
            target.write_bytes(b"!" + original[1:])
            os.utime(
                target,
                ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
            )
        return content

    monkeypatch.setattr(os, "read", rewrite_after_read)
    with pytest.raises(LocalTrustError, match="local team trust unavailable"):
        if kind == "pending":
            provider.load_pending_join()
        else:
            provider.load_versioned()
    assert rewritten


def test_versioned_provider_persists_only_canonical_public_join_metadata(
    tmp_path: object,
) -> None:
    """Catches a pending join exporting secrets or becoming active trust prematurely."""
    from pathlib import Path

    from intent_engineering.team_state.local_trust import (
        LocalTrustProvider,
        PendingJoinTrustV2,
    )

    assert isinstance(tmp_path, Path)
    _state, invite, response, before = _v2_receipts(tmp_path)
    root = tmp_path / "joiner"
    (root / ".intent").mkdir(parents=True, mode=0o700)
    provider = LocalTrustProvider(root)
    pending = PendingJoinTrustV2(
        phase="response-ready",
        invite=invite,
        response=response,
        local_recipient_key_id=response.recipient_key_id,
        local_signature_id=response.signature_id,
        expected_root_key_id=invite.root.root_key_id,
        expected_authority_before_digest=before,
        external_write_attempted=False,
    )

    provider.save_pending_join(pending)

    target = root / ".intent/team-join-pending.json"
    content = target.read_bytes()
    assert provider.load_pending_join() == pending
    assert provider.load_versioned() is None
    assert stat.S_IMODE((root / ".intent").stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert (
        content
        == json.dumps(
            pending.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    assert b"private" not in content
    assert b"secret-sentinel" not in content


def test_activate_join_delegates_one_exact_atomic_state_and_trust_transition(
    tmp_path: object,
) -> None:
    """Catches active trust advancing separately from the restored canonical state."""
    from pathlib import Path

    from intent_engineering.team_state.local_trust import (
        LocalTrustConfigV2,
        LocalTrustProvider,
        PendingJoinTrustV2,
    )

    assert isinstance(tmp_path, Path)
    state, invite, response, before = _v2_receipts(tmp_path)
    root = tmp_path / "joiner"
    (root / ".intent").mkdir(parents=True, mode=0o700)
    provider = LocalTrustProvider(root)
    pending = PendingJoinTrustV2(
        phase="awaiting-merge",
        invite=invite,
        response=response,
        local_recipient_key_id=response.recipient_key_id,
        local_signature_id=response.signature_id,
        expected_root_key_id=invite.root.root_key_id,
        expected_authority_before_digest=before,
        external_write_attempted=True,
    )
    provider.save_pending_join(pending)
    trust = LocalTrustConfigV2(
        project_id=invite.project_id,
        repository_id=invite.repository_id,
        root=invite.root,
        member_id=response.proposed_member.member_id,
        device_certificate_id="certificate:sha256:" + "e" * 64,
        recipient_key_id=response.recipient_key_id,
        signature_id=response.signature_id,
        accepted_authority_digest="sha256:" + "a" * 64,
        accepted_authority_sequence=state.authority.sequence + 1,
        accepted_bundle_digest="sha256:" + "c" * 64,
    )

    class Install:
        def __init__(self) -> None:
            self.calls = []

        def recover_with_trust(self, **kwargs):
            assert set(kwargs) == {"active_target", "pending_target"}

        def matches_journal(self, target):
            return target.name == "join-activation.json"

        def install_with_trust(self, **kwargs):
            self.calls.append(kwargs)
            assert kwargs["pending_preimage"] == pending.canonical_bytes()
            kwargs["active_target"].atomic_write(kwargs["trust_content"])
            kwargs["pending_target"].atomic_write(b"")
            os.chmod(kwargs["active_target"].path, 0o600)
            os.chmod(kwargs["pending_target"].path, 0o600)

    install = Install()
    provider.activate_join(pending_preimage=pending, trust=trust, install=install)

    assert len(install.calls) == 1
    assert provider.load_versioned() == trust
    assert provider.load_pending_join() is None

    provider.activate_join(pending_preimage=pending, trust=trust, install=install)
    assert len(install.calls) == 1


def test_state_install_transaction_rolls_back_state_and_trust_on_cancellation(
    tmp_path: object,
) -> None:
    """Catches a crash exposing restored state without its matching stable-root trust."""
    from pathlib import Path

    from intent_engineering.storage.secure import SecureDirectory
    from intent_engineering.team_state.local_trust import (
        LocalTrustConfigV2,
        LocalTrustProvider,
        PendingJoinTrustV2,
        StateInstallTransaction,
    )

    assert isinstance(tmp_path, Path)
    state, invite, response, before = _v2_receipts(tmp_path)
    root = tmp_path / "joiner"
    (root / ".intent").mkdir(parents=True, mode=0o700)
    provider = LocalTrustProvider(root)
    pending = PendingJoinTrustV2(
        phase="awaiting-merge",
        invite=invite,
        response=response,
        local_recipient_key_id=response.recipient_key_id,
        local_signature_id=response.signature_id,
        expected_root_key_id=invite.root.root_key_id,
        expected_authority_before_digest=before,
        external_write_attempted=True,
    )
    provider.save_pending_join(pending)
    trust = LocalTrustConfigV2(
        project_id=invite.project_id,
        repository_id=invite.repository_id,
        root=invite.root,
        member_id=response.proposed_member.member_id,
        device_certificate_id="certificate:sha256:" + "e" * 64,
        recipient_key_id=response.recipient_key_id,
        signature_id=response.signature_id,
        accepted_authority_digest="sha256:" + "a" * 64,
        accepted_authority_sequence=state.authority.sequence + 1,
        accepted_bundle_digest="sha256:" + "c" * 64,
    )
    workspace = SecureDirectory.open(root / ".intent")
    journal = workspace.file("join-activation.json")
    state_target = workspace.file("installed-state.marker")
    state_target.atomic_write(b"before")
    try:
        failed = StateInstallTransaction(
            journal,
            {"installed_state": state_target},
            lambda transaction: transaction.write("installed_state", b"after"),
            fault_hook=lambda phase: (
                (_ for _ in ()).throw(KeyboardInterrupt()) if phase == "target:team_trust" else None
            ),
        )
        with pytest.raises(KeyboardInterrupt):
            provider.activate_join(pending_preimage=pending, trust=trust, install=failed)
        failed.close()
        assert state_target.read_bytes() == b"before"
        assert provider.load_versioned() is None
        assert provider.load_pending_join() == pending

        completed = StateInstallTransaction(
            journal,
            {"installed_state": state_target},
            lambda transaction: transaction.write("installed_state", b"after"),
        )
        provider.activate_join(pending_preimage=pending, trust=trust, install=completed)
        completed.close()
        assert state_target.read_bytes() == b"after"
        assert provider.load_versioned() == trust
        assert provider.load_pending_join() is None
    finally:
        state_target.close()
        journal.close()
        workspace.close()


def test_state_install_transaction_recovers_process_exit_before_owner_mode_hardening(
    tmp_path: object,
) -> None:
    """Catches strict trust parsing preventing recovery of a prepared activation journal."""
    from pathlib import Path

    from intent_engineering.storage.secure import SecureDirectory
    from intent_engineering.team_state.local_trust import (
        LocalTrustConfigV2,
        LocalTrustProvider,
        PendingJoinTrustV2,
        StateInstallTransaction,
    )

    assert isinstance(tmp_path, Path)
    state, invite, response, before = _v2_receipts(tmp_path)
    root = tmp_path / "joiner"
    (root / ".intent").mkdir(parents=True, mode=0o700)
    provider = LocalTrustProvider(root)
    pending = PendingJoinTrustV2(
        phase="awaiting-merge",
        invite=invite,
        response=response,
        local_recipient_key_id=response.recipient_key_id,
        local_signature_id=response.signature_id,
        expected_root_key_id=invite.root.root_key_id,
        expected_authority_before_digest=before,
        external_write_attempted=True,
    )
    provider.save_pending_join(pending)
    trust = LocalTrustConfigV2(
        project_id=invite.project_id,
        repository_id=invite.repository_id,
        root=invite.root,
        member_id=response.proposed_member.member_id,
        device_certificate_id="certificate:sha256:" + "e" * 64,
        recipient_key_id=response.recipient_key_id,
        signature_id=response.signature_id,
        accepted_authority_digest="sha256:" + "a" * 64,
        accepted_authority_sequence=state.authority.sequence + 1,
        accepted_bundle_digest="sha256:" + "c" * 64,
    )
    workspace = SecureDirectory.open(root / ".intent")
    journal = workspace.file("join-activation.json")
    state_target = workspace.file("installed-state.marker")
    state_target.atomic_write(b"before")
    child = os.fork()
    if child == 0:
        interrupted = StateInstallTransaction(
            journal,
            {"installed_state": state_target},
            lambda transaction: transaction.write("installed_state", b"after"),
            fault_hook=lambda phase: os._exit(79) if phase == "target:team_trust" else None,
        )
        provider.activate_join(pending_preimage=pending, trust=trust, install=interrupted)
        os._exit(0)

    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 79
    try:
        restarted = StateInstallTransaction(
            journal,
            {"installed_state": state_target},
            lambda transaction: transaction.write("installed_state", b"after"),
        )
        provider.activate_join(pending_preimage=pending, trust=trust, install=restarted)
        restarted.close()
        assert state_target.read_bytes() == b"after"
        assert provider.load_versioned() == trust
        assert provider.load_pending_join() is None
    finally:
        state_target.close()
        journal.close()
        workspace.close()


def test_migration_device_signer_reuses_only_public_recipient_and_never_deletes_legacy_key(
    tmp_path: object,
) -> None:
    """Catches copying a legacy private recipient into the new v2 device-signing slot."""
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from intent_engineering.team_state.keys import DeviceEnrollmentBinding
    from intent_engineering.team_state.signing import KeyringExistingRecipientDeviceSigner

    assert isinstance(tmp_path, Path)
    backend = Backend()
    binding = DeviceEnrollmentBinding(
        project_id="project",
        repository_id="github.com/acme/project",
        actor="github:123",
        github_account_id=123,
        github_login="alice",
        device_id="device:" + "1" * 32,
    )
    recipient_public = (
        X25519PrivateKey.from_private_bytes(b"a" * 32).public_key().public_bytes_raw()
    )
    store = KeyringExistingRecipientDeviceSigner(
        binding,
        backend=backend,
        private_key_source=lambda: b"s" * 32,
        lock_root=tmp_path / "locks",
    )

    material = store.create_for_existing_recipient(binding, recipient_public)
    signature = store.sign(material.signature_id, b"migration-preimage")

    assert material.recipient_public_key == recipient_public
    assert len(backend.values) == 1
    ((service, _account), encoded_private) = next(iter(backend.values.items()))
    assert service == "intent-engineering-device-signing-v2/project"
    assert encoded_private == base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode()
    assert base64.urlsafe_b64encode(b"a" * 32).rstrip(b"=").decode() not in backend.values.values()
    Ed25519PublicKey.from_public_bytes(material.signing_public_key).verify(
        signature, b"migration-preimage"
    )


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode", "owner"])
def test_v2_local_trust_rejects_unsafe_owner_or_path_metadata(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Catches local authority following aliases or accepting non-owner-only storage."""
    from pathlib import Path

    from intent_engineering.team_state.local_trust import (
        LocalTrustError,
        LocalTrustProvider,
        PendingJoinTrustV2,
    )

    assert isinstance(tmp_path, Path)
    _state, invite, response, before = _v2_receipts(tmp_path)
    root = tmp_path / "joiner"
    (root / ".intent").mkdir(parents=True, mode=0o700)
    provider = LocalTrustProvider(root)
    pending = PendingJoinTrustV2(
        phase="response-ready",
        invite=invite,
        response=response,
        local_recipient_key_id=response.recipient_key_id,
        local_signature_id=response.signature_id,
        expected_root_key_id=invite.root.root_key_id,
        expected_authority_before_digest=before,
        external_write_attempted=False,
    )
    provider.save_pending_join(pending)
    target = root / ".intent/team-join-pending.json"
    original = root / "original.json"
    if kind == "mode":
        target.chmod(0o644)
    elif kind == "owner":
        real_fstat = os.fstat

        def foreign_owner(descriptor):
            result = real_fstat(descriptor)
            if stat.S_ISREG(result.st_mode):
                values = list(result)
                values[4] = result.st_uid + 1
                return os.stat_result(values)
            return result

        monkeypatch.setattr(os, "fstat", foreign_owner)
    else:
        target.rename(original)
        if kind == "symlink":
            target.symlink_to(original)
        else:
            os.link(original, target)
    with pytest.raises(LocalTrustError):
        provider.load_pending_join()
