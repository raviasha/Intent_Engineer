"""OS-keyring-backed Ed25519 authority for team-state publication."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

import keyring
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from intent_engineering.storage._atomic import same_path_lock

_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ACTOR = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_REPOSITORY_ID = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


class SigningKeyStoreError(ValueError):
    """Fixed public failure for unavailable publication signing authority."""

    def __init__(self) -> None:
        super().__init__("signing key unavailable")


class _KeyringBackend(Protocol):
    def get_password(self, service: str, account: str) -> str | None: ...

    def set_password(self, service: str, account: str, value: str) -> None: ...


def _private_key_bytes() -> bytes:
    return Ed25519PrivateKey.generate().private_bytes_raw()


def _b64url(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def _decode_private(value: object) -> bytes:
    if type(value) is not str or not value or "=" in value or _BASE64URL.fullmatch(value) is None:
        raise ValueError("invalid private key")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(decoded) != 32 or _b64url(decoded) != value:
        raise ValueError("invalid private key")
    Ed25519PrivateKey.from_private_bytes(decoded)
    return decoded


def _validate_binding(project_id: object, repository_id: object, actor: object) -> None:
    if type(project_id) is not str or type(repository_id) is not str or type(actor) is not str:
        raise ValueError("invalid signing binding")
    repository_parts = repository_id.split("/")
    if (
        _PROJECT_ID.fullmatch(project_id) is None
        or _REPOSITORY_ID.fullmatch(repository_id) is None
        or repository_parts[0] != repository_parts[0].lower()
        or repository_parts[-1].endswith(".git")
        or any(part in {".", ".."} for part in repository_parts[1:])
        or _ACTOR.fullmatch(actor) is None
    ):
        raise ValueError("invalid signing binding")


def _signature_id(project_id: str, repository_id: str, actor: str) -> str:
    content = json.dumps(
        {
            "actor": actor,
            "project_id": project_id,
            "repository_id": repository_id,
            "schema": "intent.publication-signing.v1",
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"signer:sha256:{hashlib.sha256(content).hexdigest()}"


def _default_lock_root() -> Path:
    return Path(tempfile.gettempdir()).resolve() / f"intent-engineering-signing-{os.getuid()}"


def _lock_target(root: Path, service: str, account: str) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("invalid signing lock root")
    root.mkdir(mode=0o700, parents=False, exist_ok=True)
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("invalid signing lock root")
    digest = hashlib.sha256(f"{service}\0{account}".encode()).hexdigest()
    return root / digest


def _raise_failure(caught: BaseException) -> None:
    if isinstance(caught, Exception):
        failure = SigningKeyStoreError()
        failure.__cause__ = None
        failure.__context__ = None
        raise failure from None
    raise caught.with_traceback(None)


class SigningKeyStore:
    """Get or create one stable, context-bound Ed25519 key in the OS keyring."""

    def __init__(
        self,
        project_id: str,
        repository_id: str,
        actor: str,
        *,
        backend: _KeyringBackend = keyring,
        lock_root: Path | None = None,
    ) -> None:
        _validate_binding(project_id, repository_id, actor)
        self._backend = backend
        self._service = f"intent-engineering-signing/{project_id}"
        self._signature_id = _signature_id(project_id, repository_id, actor)
        self._lock_target = _lock_target(
            lock_root if lock_root is not None else _default_lock_root(),
            self._service,
            self._signature_id,
        )

    def _signing_keys(self) -> Mapping[str, bytes]:
        private = b""
        encoded = ""
        try:
            with same_path_lock(self._lock_target):
                stored = self._backend.get_password(self._service, self._signature_id)
                if stored is None:
                    private = _private_key_bytes()
                    if type(private) is not bytes or len(private) != 32:
                        raise ValueError("invalid private key source")
                    Ed25519PrivateKey.from_private_bytes(private)
                    encoded = _b64url(private)
                    self._backend.set_password(self._service, self._signature_id, encoded)
                    if self._backend.get_password(self._service, self._signature_id) != encoded:
                        raise ValueError("signing key write mismatch")
                else:
                    private = _decode_private(stored)
                return MappingProxyType({self._signature_id: bytes(private)})
        finally:
            private = b""
            encoded = ""

    def signing_keys(self) -> Mapping[str, bytes]:
        """Return the stable raw private signing-key mapping required by publication."""
        caught: BaseException | None = None
        try:
            return self._signing_keys()
        except BaseException as error:  # noqa: BLE001 - preserve cancellation type
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            caught = error
        if caught is not None:
            _raise_failure(caught)
        raise AssertionError("signing key lookup must return or raise")

    def _load_signing_keys(self) -> Mapping[str, bytes]:
        with same_path_lock(self._lock_target):
            private = _decode_private(self._backend.get_password(self._service, self._signature_id))
            return MappingProxyType({self._signature_id: bytes(private)})

    def load_signing_keys(self) -> Mapping[str, bytes]:
        """Read existing publication authority without creating or rotating it."""
        caught: BaseException | None = None
        try:
            return self._load_signing_keys()
        except BaseException as error:  # noqa: BLE001 - preserve cancellation type
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            caught = error
        if caught is not None:
            _raise_failure(caught)
        raise AssertionError("signing key read must return or raise")

    def public_keys(self) -> Mapping[str, bytes]:
        """Derive raw Ed25519 public authority from the existing keyring secret."""
        signing = self.load_signing_keys()
        return MappingProxyType(
            {
                signature_id: Ed25519PrivateKey.from_private_bytes(private)
                .public_key()
                .public_bytes_raw()
                for signature_id, private in signing.items()
            }
        )

    def __repr__(self) -> str:
        return "SigningKeyStore()"


__all__ = ["SigningKeyStore", "SigningKeyStoreError"]
