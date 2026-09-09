"""Recipient key storage and verified enrollment binding contracts."""

from __future__ import annotations

import base64
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from intent_engineering.team_state.crypto import verify_recipient_possession_proof
from intent_engineering.team_state.keys import (
    DeviceEnrollmentBinding,
    GitHubIdentity,
    InMemoryRecipientKeyStore,
    KeyringDeviceKeyStore,
    KeyringRecipientKeyStore,
    RecipientEnrollmentBinding,
    RecipientKeyStore,
    RecipientKeyStoreError,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _device_traceback_locals(error: BaseException) -> list[dict[str, object]]:
    return [
        dict(frame.f_locals)
        for frame, _lineno in traceback.walk_tb(error.__traceback__)
        if frame.f_globals.get("__name__") == "intent_engineering.team_state.keys"
    ]


def _binding(**changes: object) -> RecipientEnrollmentBinding:
    material: dict[str, object] = {
        "project_id": "alpha",
        "repository_id": "github.com/acme/alpha",
        "actor": "local:asha",
        "github_identity": GitHubIdentity(account_id="101", login="asha"),
        "webauthn_credential_id": base64.urlsafe_b64encode(b"credential").rstrip(b"=").decode(),
        "webauthn_credential_public_key": base64.urlsafe_b64encode(b"public-key-material")
        .rstrip(b"=")
        .decode(),
        "enrolled_at": NOW,
    }
    material.update(changes)
    return RecipientEnrollmentBinding(**material)


class _Backend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str | None]] = []
        self.failure: BaseException | None = None

    def _raise(self) -> None:
        if self.failure is not None:
            raise self.failure

    def get_password(self, service: str, account: str) -> str | None:
        self.calls.append(("get", service, account))
        self._raise()
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.calls.append(("set", service, account))
        self._raise()
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.calls.append(("delete", service, account))
        self._raise()
        if (service, account) not in self.values:
            raise RuntimeError("missing")
        del self.values[(service, account)]


def test_in_memory_store_implements_public_protocol_and_round_trips_private_key() -> None:
    """Catches a fake that drifts from the exact public key-store interface."""
    store: RecipientKeyStore = InMemoryRecipientKeyStore(
        _binding(), private_key_source=lambda: b"k" * 32
    )

    record = store.generate("alpha", "local:asha")

    assert record.project_id == "alpha"
    assert record.repository_id == "github.com/acme/alpha"
    assert record.github_account_id == "101"
    assert record.github_login == "asha"
    assert store.private_key(record.key_id) == b"k" * 32


def test_keyring_store_writes_only_unpadded_private_key_to_exact_location() -> None:
    """Catches metadata, padding, or public material leaking into the OS credential value."""
    backend = _Backend()
    store = KeyringRecipientKeyStore(
        _binding(), backend=backend, private_key_source=lambda: b"s" * 32
    )

    record = store.generate("alpha", "local:asha")

    service = "intent-engineering/alpha"
    encoded = backend.values[(service, record.key_id)]
    assert encoded == "c3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3M"
    assert "=" not in encoded
    assert record.public_key not in encoded
    assert record.github_login not in encoded
    assert store.private_key(record.key_id) == b"s" * 32


def test_duplicate_enrollment_is_rejected_without_overwriting_private_key() -> None:
    """Catches a repeat enrollment silently rotating an approved recipient key."""
    backend = _Backend()
    store = KeyringRecipientKeyStore(
        _binding(), backend=backend, private_key_source=lambda: b"s" * 32
    )
    record = store.generate("alpha", "local:asha")

    with pytest.raises(RecipientKeyStoreError, match="recipient key unavailable"):
        store.generate("alpha", "local:asha")

    assert store.private_key(record.key_id) == b"s" * 32
    assert [call[0] for call in backend.calls].count("set") == 1


@pytest.mark.parametrize(
    ("project_id", "actor"),
    (("other", "local:asha"), ("alpha", "local:other"), ("", "local:asha")),
)
def test_generate_rejects_project_or_actor_outside_bound_enrollment(
    project_id: str, actor: str
) -> None:
    """Catches a verified identity being replayed into another project or actor policy slot."""
    store = InMemoryRecipientKeyStore(_binding(), private_key_source=lambda: b"k" * 32)

    with pytest.raises(RecipientKeyStoreError, match="recipient key unavailable"):
        store.generate(project_id, actor)


def test_delete_removes_exact_key_and_subsequent_reads_fail() -> None:
    """Catches recipient removal leaving usable private key material behind."""
    backend = _Backend()
    store = KeyringRecipientKeyStore(
        _binding(), backend=backend, private_key_source=lambda: b"s" * 32
    )
    record = store.generate("alpha", "local:asha")

    store.delete(record.key_id)

    assert ("intent-engineering/alpha", record.key_id) not in backend.values
    with pytest.raises(RecipientKeyStoreError, match="recipient key unavailable"):
        store.private_key(record.key_id)


@pytest.mark.parametrize("operation", ("generate", "private_key", "delete"))
def test_keyring_unavailable_or_locked_is_one_secret_free_failure(operation: str) -> None:
    """Catches backend diagnostics escaping the stable public key-store boundary."""
    backend = _Backend()
    store = KeyringRecipientKeyStore(
        _binding(), backend=backend, private_key_source=lambda: b"s" * 32
    )
    key_id = (
        "recipient:sha256:" + "a" * 64
        if operation == "generate"
        else store.generate("alpha", "local:asha").key_id
    )
    backend.failure = RuntimeError("locked keyring secret-private-value-32-bytes!!")

    with pytest.raises(RecipientKeyStoreError, match="^recipient key unavailable$") as caught:
        getattr(store, operation)(
            *("alpha", "local:asha") if operation == "generate" else (key_id,)
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret-private" not in repr(caught.value)


def test_cancellation_type_is_preserved_without_backend_traceback() -> None:
    """Catches cancellation being converted to a retryable keyring failure or retaining frames."""

    class Cancelled(BaseException):
        pass

    backend = _Backend()
    backend.failure = Cancelled("cancel")
    store = KeyringRecipientKeyStore(_binding(), backend=backend)

    with pytest.raises(Cancelled) as caught:
        store.generate("alpha", "local:asha")

    frames = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "_raise" not in frames
    assert "get_password" not in frames


def test_private_key_never_appears_in_record_repr_or_serialization() -> None:
    """Catches private recipient material entering reviewed team-state JSON."""
    private = b"q" * 32
    store = InMemoryRecipientKeyStore(_binding(), private_key_source=lambda: private)

    record = store.generate("alpha", "local:asha")
    rendered = record.model_dump_json()

    assert base64.urlsafe_b64encode(private).rstrip(b"=").decode() not in rendered
    assert base64.urlsafe_b64encode(private).rstrip(b"=").decode() not in repr(record)


def test_binding_rejects_noncanonical_or_incomplete_identity_and_credential() -> None:
    """Catches unverified or malformed provider/WebAuthn identity reaching key generation."""
    with pytest.raises(ValueError):
        GitHubIdentity(account_id="0101", login="asha")
    with pytest.raises(ValueError):
        GitHubIdentity(account_id="101", login="Asha")
    with pytest.raises(ValueError):
        _binding(webauthn_credential_public_key="short")


def test_concurrent_keyring_generation_has_exactly_one_winner(tmp_path: Path) -> None:
    """Catches check-then-set races overwriting a recipient created by another caller."""

    class PausedFirstWriteBackend(_Backend):
        def __init__(self) -> None:
            super().__init__()
            self.first_write = threading.Event()
            self.release_first_write = threading.Event()
            self.write_count = 0

        def set_password(self, service: str, account: str, value: str) -> None:
            self.write_count += 1
            if self.write_count == 1:
                self.first_write.set()
                assert self.release_first_write.wait(timeout=3)
            super().set_password(service, account, value)

    backend = PausedFirstWriteBackend()
    stores = tuple(
        KeyringRecipientKeyStore(
            _binding(),
            backend=backend,
            private_key_source=lambda: b"w" * 32,
            lock_root=tmp_path / "recipient-locks",
        )
        for _ in range(2)
    )
    results: list[str] = []
    second_finished = threading.Event()

    def generate(store: KeyringRecipientKeyStore, *, mark_finished: bool) -> None:
        try:
            store.generate("alpha", "local:asha")
            results.append("created")
        except RecipientKeyStoreError:
            results.append("duplicate")
        finally:
            if mark_finished:
                second_finished.set()

    first = threading.Thread(target=generate, args=(stores[0],), kwargs={"mark_finished": False})
    first.start()
    assert backend.first_write.wait(timeout=3)
    second = threading.Thread(target=generate, args=(stores[1],), kwargs={"mark_finished": True})
    second.start()
    second_finished.wait(timeout=1)
    backend.release_first_write.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive() and not second.is_alive()
    assert sorted(results) == ["created", "duplicate"]
    assert backend.write_count == 1


def test_in_memory_failure_is_scrubbed_like_production() -> None:
    """Catches the test fake leaking private-source diagnostics production suppresses."""

    def secret_source() -> bytes:
        raise RuntimeError("private-source-secret")

    store = InMemoryRecipientKeyStore(_binding(), private_key_source=secret_source)

    with pytest.raises(RecipientKeyStoreError, match="^recipient key unavailable$") as caught:
        store.generate("alpha", "local:asha")

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-source-secret" not in repr(caught.value)


def test_in_memory_cancellation_preserves_type_without_source_traceback() -> None:
    """Catches the fake retaining secret-producing frames on cancellation."""

    class Cancelled(BaseException):
        pass

    def secret_source() -> bytes:
        raise Cancelled("cancel")

    store = InMemoryRecipientKeyStore(_binding(), private_key_source=secret_source)

    with pytest.raises(Cancelled) as caught:
        store.generate("alpha", "local:asha")

    frames = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "secret_source" not in frames


def test_device_store_creates_separate_recipient_and_signing_keys(
    tmp_path: Path,
) -> None:
    """Catches key reuse or keyring namespace collision between device capabilities."""
    backend = _Backend()
    binding = DeviceEnrollmentBinding(
        project_id="alpha",
        repository_id="github.com/acme/alpha",
        actor="github:101",
        github_account_id=101,
        github_login="asha",
        device_id="device:" + "1" * 32,
    )
    store = KeyringDeviceKeyStore(
        binding,
        backend=backend,
        recipient_private_key_source=lambda: b"x" * 32,
        signing_private_key_source=lambda: b"s" * 32,
        lock_root=tmp_path / "device-locks",
    )

    public = store.create(binding)
    signed = store.sign(public.signature_id, b"state preimage")
    challenge = X25519PrivateKey.from_private_bytes(b"c" * 32)
    proof = store.prove_recipient_possession(
        public.recipient_key_id,
        challenge.public_key().public_bytes_raw(),
        b"join response subject",
    )

    Ed25519PublicKey.from_public_bytes(public.signing_public_key).verify(signed, b"state preimage")
    assert verify_recipient_possession_proof(
        public.recipient_public_key,
        challenge.private_bytes_raw(),
        b"join response subject",
        proof,
    )
    assert public.device_id == binding.device_id
    assert len(backend.values) == 2
    assert {service for service, _account in backend.values} == {
        "intent-engineering-device-recipient-v2/alpha",
        "intent-engineering-device-signing-v2/alpha",
    }
    assert public.recipient_key_id != public.signature_id


def test_device_store_exposes_only_its_public_enrollment_binding(tmp_path: Path) -> None:
    """Catches enrollment services reaching through the key boundary for private material."""
    backend = _Backend()
    binding = DeviceEnrollmentBinding(
        project_id="alpha",
        repository_id="github.com/acme/alpha",
        actor="github:101",
        github_account_id=101,
        github_login="asha",
        device_id="device:" + "1" * 32,
    )
    store = KeyringDeviceKeyStore(
        binding,
        backend=backend,
        recipient_private_key_source=lambda: b"x" * 32,
        signing_private_key_source=lambda: b"s" * 32,
        lock_root=tmp_path / "device-locks",
    )

    assert store.enrollment_binding() == binding
    assert "private" not in repr(store.enrollment_binding()).lower()
    assert backend.values == {}


def test_device_store_rejects_wrong_ids_without_leaking_private_values(tmp_path: Path) -> None:
    """Catches cross-device key selection and secret-bearing backend diagnostics."""
    backend = _Backend()
    binding = DeviceEnrollmentBinding(
        project_id="alpha",
        repository_id="github.com/acme/alpha",
        actor="github:101",
        github_account_id=101,
        github_login="asha",
        device_id="device:" + "1" * 32,
    )
    store = KeyringDeviceKeyStore(
        binding,
        backend=backend,
        recipient_private_key_source=lambda: b"x" * 32,
        signing_private_key_source=lambda: b"s" * 32,
        lock_root=tmp_path / "device-locks",
    )
    store.create(binding)

    with pytest.raises(RecipientKeyStoreError, match="^recipient key unavailable$"):
        store.prove_recipient_possession(
            "recipient:sha256:" + "0" * 64,
            X25519PrivateKey.generate().public_key().public_bytes_raw(),
            b"subject",
        )

    backend.failure = RuntimeError("device-private-token-value")
    with pytest.raises(RecipientKeyStoreError, match="^recipient key unavailable$") as caught:
        store.sign("signer:sha256:" + "0" * 64, b"state")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "device-private-token" not in repr(caught.value)

    locals_by_frame = _device_traceback_locals(caught.value)
    assert all(store not in values.values() for values in locals_by_frame)
    assert all(backend not in values.values() for values in locals_by_frame)
    assert all("caught" not in values for values in locals_by_frame)


@pytest.mark.parametrize(
    "failing_service",
    (
        "intent-engineering-device-recipient-v2/alpha",
        "intent-engineering-device-signing-v2/alpha",
    ),
)
def test_device_creation_rolls_back_a_durable_write_then_raise_for_either_key(
    tmp_path: Path,
    failing_service: str,
) -> None:
    """Catches a failed signing-key write stranding a usable recipient half-key."""

    class DurableWriteThenFails(_Backend):
        def set_password(self, service: str, account: str, value: str) -> None:
            super().set_password(service, account, value)
            if service == failing_service:
                raise RuntimeError("ambiguous durable keyring write")

    backend = DurableWriteThenFails()
    binding = DeviceEnrollmentBinding(
        project_id="alpha",
        repository_id="github.com/acme/alpha",
        actor="github:101",
        github_account_id=101,
        github_login="asha",
        device_id="device:" + "1" * 32,
    )
    store = KeyringDeviceKeyStore(
        binding,
        backend=backend,
        recipient_private_key_source=lambda: b"x" * 32,
        signing_private_key_source=lambda: b"s" * 32,
        lock_root=tmp_path / "device-locks",
    )

    with pytest.raises(RecipientKeyStoreError, match="^recipient key unavailable$"):
        store.create(binding)

    assert backend.values == {}
    deleted_services = {
        service for operation, service, _account in backend.calls if operation == "delete"
    }
    assert deleted_services == {
        "intent-engineering-device-recipient-v2/alpha",
        "intent-engineering-device-signing-v2/alpha",
    }
    for service in deleted_services:
        assert (
            sum(
                operation == "get" and called_service == service
                for operation, called_service, _account in backend.calls
            )
            >= 2
        )


def test_device_creation_fails_closed_when_rollback_cannot_verify_absence(
    tmp_path: Path,
) -> None:
    """Catches cleanup reporting success while an ambiguously written private key remains."""

    class UndeletableWrite(_Backend):
        def set_password(self, service: str, account: str, value: str) -> None:
            super().set_password(service, account, value)
            raise RuntimeError("ambiguous durable keyring write")

        def delete_password(self, service: str, account: str) -> None:
            self.calls.append(("delete", service, account))
            raise RuntimeError("cleanup unavailable")

    backend = UndeletableWrite()
    binding = DeviceEnrollmentBinding(
        project_id="alpha",
        repository_id="github.com/acme/alpha",
        actor="github:101",
        github_account_id=101,
        github_login="asha",
        device_id="device:" + "1" * 32,
    )
    store = KeyringDeviceKeyStore(binding, backend=backend, lock_root=tmp_path / "locks")

    with pytest.raises(RecipientKeyStoreError, match="^recipient key unavailable$"):
        store.create(binding)

    assert backend.values
    assert {service for operation, service, _account in backend.calls if operation == "delete"} == {
        "intent-engineering-device-recipient-v2/alpha",
        "intent-engineering-device-signing-v2/alpha",
    }


def test_device_rollback_preserves_cleanup_cancellation_identity(tmp_path: Path) -> None:
    """Catches a cancellation during ambiguous-write cleanup being converted or ignored."""

    class Cancelled(BaseException):
        pass

    cancellation = Cancelled("cleanup-private-token")

    class CancellationDuringRollback(_Backend):
        def set_password(self, service: str, account: str, value: str) -> None:
            super().set_password(service, account, value)
            raise RuntimeError("ambiguous durable keyring write")

        def delete_password(self, service: str, account: str) -> None:
            self.calls.append(("delete", service, account))
            raise cancellation

    backend = CancellationDuringRollback()
    binding = DeviceEnrollmentBinding(
        project_id="alpha",
        repository_id="github.com/acme/alpha",
        actor="github:101",
        github_account_id=101,
        github_login="asha",
        device_id="device:" + "1" * 32,
    )
    store = KeyringDeviceKeyStore(binding, backend=backend, lock_root=tmp_path / "locks")

    with pytest.raises(Cancelled) as caught:
        store.create(binding)

    assert caught.value is cancellation
    assert caught.value.args == ()


def test_device_rollback_never_deletes_a_preexisting_partial_slot(tmp_path: Path) -> None:
    """Catches incomplete historical state being mistaken for this attempt's empty slots."""
    original = base64.urlsafe_b64encode(b"p" * 32).rstrip(b"=").decode()

    class PreexistingPartialSlot(_Backend):
        def get_password(self, service: str, account: str) -> str | None:
            self.calls.append(("get", service, account))
            if service == "intent-engineering-device-recipient-v2/alpha":
                return original
            return None

    backend = PreexistingPartialSlot()
    binding = DeviceEnrollmentBinding(
        project_id="alpha",
        repository_id="github.com/acme/alpha",
        actor="github:101",
        github_account_id=101,
        github_login="asha",
        device_id="device:" + "1" * 32,
    )
    store = KeyringDeviceKeyStore(binding, backend=backend, lock_root=tmp_path / "locks")

    with pytest.raises(RecipientKeyStoreError, match="^recipient key unavailable$"):
        store.create(binding)

    assert (
        backend.get_password("intent-engineering-device-recipient-v2/alpha", "ignored") == original
    )
    assert all(operation != "delete" for operation, _service, _account in backend.calls)


def test_same_device_id_under_different_github_identity_uses_distinct_slots(
    tmp_path: Path,
) -> None:
    """Catches a device identifier replay reusing another GitHub member's private keys."""
    backend = _Backend()
    first = DeviceEnrollmentBinding(
        project_id="alpha",
        repository_id="github.com/acme/alpha",
        actor="github:101",
        github_account_id=101,
        github_login="asha",
        device_id="device:" + "1" * 32,
    )
    second = DeviceEnrollmentBinding(
        project_id="alpha",
        repository_id="github.com/acme/alpha",
        actor="github:202",
        github_account_id=202,
        github_login="ben",
        device_id=first.device_id,
    )
    first_store = KeyringDeviceKeyStore(first, backend=backend, lock_root=tmp_path / "locks")
    second_store = KeyringDeviceKeyStore(second, backend=backend, lock_root=tmp_path / "locks")

    first_store.create(first)
    second_store.create(second)

    assert len(backend.values) == 4
    assert len({account for _service, account in backend.values}) == 2


def test_device_cancellation_scrubs_args_store_backend_and_private_locals(
    tmp_path: Path,
) -> None:
    """Catches cancellation retaining device-key source secrets in production frames."""

    class Cancelled(BaseException):
        pass

    cancellation = Cancelled("device-source-private-token")

    def cancel() -> bytes:
        raise cancellation

    backend = _Backend()
    binding = DeviceEnrollmentBinding(
        project_id="alpha",
        repository_id="github.com/acme/alpha",
        actor="github:101",
        github_account_id=101,
        github_login="asha",
        device_id="device:" + "1" * 32,
    )
    store = KeyringDeviceKeyStore(
        binding,
        backend=backend,
        recipient_private_key_source=cancel,
        lock_root=tmp_path / "locks",
    )

    with pytest.raises(Cancelled) as caught:
        store.create(binding)

    assert caught.value is cancellation
    assert caught.value.args == ()
    locals_by_frame = _device_traceback_locals(caught.value)
    assert all(store not in values.values() for values in locals_by_frame)
    assert all(backend not in values.values() for values in locals_by_frame)
    assert all("caught" not in values and "recipient" not in values for values in locals_by_frame)
