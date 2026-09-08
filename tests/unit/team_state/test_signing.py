"""OS-keyring-backed publication signing authority."""

from __future__ import annotations

import base64
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from intent_engineering.team_state.signing import SigningKeyStore, SigningKeyStoreError


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


def test_signing_keys_stores_only_raw_private_key_and_returns_stable_authority(
    tmp_path: Path,
) -> None:
    """Catches metadata leakage or silent signer rotation between publication attempts."""
    backend = _Backend()
    store = SigningKeyStore(
        "alpha",
        "github.com/acme/alpha",
        "local:asha",
        backend=backend,
        lock_root=tmp_path / "locks",
    )

    first = store.signing_keys()
    second = store.signing_keys()

    assert first == second
    assert len(first) == 1
    [(signature_id, private)] = first.items()
    assert signature_id.startswith("signer:sha256:")
    assert len(private) == 32
    assert backend.values == {
        (
            "intent-engineering-signing/alpha",
            signature_id,
        ): base64.urlsafe_b64encode(private).rstrip(b"=").decode("ascii")
    }
    assert [call[0] for call in backend.calls].count("set") == 1
    assert "alpha" not in next(iter(backend.values.values()))
    assert "github.com" not in next(iter(backend.values.values()))
    assert "asha" not in next(iter(backend.values.values()))


def test_exact_project_repository_actor_binding_selects_distinct_accounts(tmp_path: Path) -> None:
    """Catches a signing key being reused across a different repository or actor authority."""
    backend = _Backend()
    bindings = (
        ("alpha", "github.com/acme/alpha", "local:asha"),
        ("alpha", "github.com/acme/beta", "local:asha"),
        ("alpha", "github.com/acme/alpha", "local:ben"),
        ("beta", "github.com/acme/alpha", "local:asha"),
    )

    ids = {
        next(
            iter(
                SigningKeyStore(
                    *binding,
                    backend=backend,
                    lock_root=tmp_path / "locks",
                ).signing_keys()
            )
        )
        for binding in bindings
    }

    assert len(ids) == len(bindings)
    assert len(backend.values) == len(bindings)


@pytest.mark.parametrize(
    ("project_id", "repository_id", "actor"),
    (
        ("", "github.com/acme/alpha", "local:asha"),
        ("alpha", "github.com/acme/alpha.git", "local:asha"),
        ("alpha", "GitHub.com/acme/alpha", "local:asha"),
        ("alpha", "github.com/acme/alpha", "Local:Asha"),
    ),
)
def test_invalid_or_ambiguous_binding_fails_before_keyring_access(
    project_id: str, repository_id: str, actor: str, tmp_path: Path
) -> None:
    """Catches malformed authority identifiers aliasing an approved keyring account."""
    backend = _Backend()

    with pytest.raises(ValueError, match="invalid signing binding"):
        SigningKeyStore(
            project_id,
            repository_id,
            actor,
            backend=backend,
            lock_root=tmp_path / "locks",
        )

    assert backend.calls == []


def test_invalid_stored_secret_fails_closed_without_replacing_it(tmp_path: Path) -> None:
    """Catches corrupted keyring material being silently replaced with new authority."""
    backend = _Backend()
    store = SigningKeyStore(
        "alpha",
        "github.com/acme/alpha",
        "local:asha",
        backend=backend,
        lock_root=tmp_path / "locks",
    )
    store.signing_keys()
    [(service, account)] = backend.values
    backend.values[(service, account)] = "private-secret-diagnostic"
    backend.calls.clear()

    with pytest.raises(SigningKeyStoreError, match="^signing key unavailable$") as caught:
        store.signing_keys()

    assert backend.values[(service, account)] == "private-secret-diagnostic"
    assert [call[0] for call in backend.calls].count("set") == 0
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-secret" not in repr(caught.value)


def test_backend_failure_is_secret_free(tmp_path: Path) -> None:
    """Catches keyring diagnostics escaping through the publication boundary."""
    backend = _Backend()
    backend.failure = RuntimeError("locked private-secret-diagnostic")
    store = SigningKeyStore(
        "alpha",
        "github.com/acme/alpha",
        "local:asha",
        backend=backend,
        lock_root=tmp_path / "locks",
    )

    with pytest.raises(SigningKeyStoreError, match="^signing key unavailable$") as caught:
        store.signing_keys()

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-secret" not in repr(caught.value)


def test_cancellation_type_is_preserved_without_backend_frames(tmp_path: Path) -> None:
    """Catches cancellation conversion or retention of secret-bearing backend frames."""

    class Cancelled(BaseException):
        pass

    backend = _Backend()
    backend.failure = Cancelled("cancel")
    store = SigningKeyStore(
        "alpha",
        "github.com/acme/alpha",
        "local:asha",
        backend=backend,
        lock_root=tmp_path / "locks",
    )

    with pytest.raises(Cancelled) as caught:
        store.signing_keys()

    frames = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "_raise" not in frames
    assert "get_password" not in frames


def test_concurrent_get_or_generate_writes_once(tmp_path: Path) -> None:
    """Catches two publishers racing to rotate the same stable signing authority."""

    class PausedBackend(_Backend):
        def __init__(self) -> None:
            super().__init__()
            self.first_write = threading.Event()
            self.release_write = threading.Event()

        def set_password(self, service: str, account: str, value: str) -> None:
            self.first_write.set()
            assert self.release_write.wait(timeout=3)
            super().set_password(service, account, value)

    backend = PausedBackend()
    stores = tuple(
        SigningKeyStore(
            "alpha",
            "github.com/acme/alpha",
            "local:asha",
            backend=backend,
            lock_root=tmp_path / "locks",
        )
        for _ in range(2)
    )
    results: list[dict[str, bytes]] = []

    first = threading.Thread(target=lambda: results.append(dict(stores[0].signing_keys())))
    first.start()
    assert backend.first_write.wait(timeout=3)
    second = threading.Thread(target=lambda: results.append(dict(stores[1].signing_keys())))
    second.start()
    backend.release_write.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive() and not second.is_alive()
    assert results[0] == results[1]
    assert [call[0] for call in backend.calls].count("set") == 1


def test_load_signing_keys_is_read_only_and_missing_key_fails_fixed(tmp_path: Path) -> None:
    """Catches publication preview silently creating authority before protection approval."""
    backend = _Backend()
    store = SigningKeyStore(
        "alpha",
        "github.com/acme/alpha",
        "local:asha",
        backend=backend,
        lock_root=tmp_path / "locks",
    )

    with pytest.raises(SigningKeyStoreError, match="^signing key unavailable$"):
        store.load_signing_keys()

    assert [call[0] for call in backend.calls] == ["get"]
    assert backend.values == {}


def test_load_and_public_keys_reuse_existing_signer_without_writes(tmp_path: Path) -> None:
    """Catches read-only publication authority rotating or deriving the wrong Ed25519 public key."""
    backend = _Backend()
    store = SigningKeyStore(
        "alpha",
        "github.com/acme/alpha",
        "local:asha",
        backend=backend,
        lock_root=tmp_path / "locks",
    )
    created = store.signing_keys()
    backend.calls.clear()

    loaded = store.load_signing_keys()
    public = store.public_keys()

    assert loaded == created
    assert tuple(public) == tuple(created)
    signature_id = next(iter(created))
    message = b"reviewed publication"
    private_signature = Ed25519PrivateKey.from_private_bytes(created[signature_id]).sign(message)
    Ed25519PublicKey.from_public_bytes(public[signature_id]).verify(private_signature, message)
    assert [call[0] for call in backend.calls] == ["get", "get"]
