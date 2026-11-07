"""Independent self-hosted runner encryption keys and reviewed, public CI trust."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, NoReturn

import keyring
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ConfigDict, Field, field_validator, model_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.team_state.models import (
    CiRecipientRecord,
    _canonical_json,
    _decode_base64url,
)
from intent_engineering.team_state.restore import SharedStateTrust, TrustedSigningKey
from intent_engineering.team_state.signing import _KeyringBackend, _lock_target

CI_TRUST_PATH_ENV = "INTENT_CI_TRUST_PATH"
MAX_CI_PUBLIC_BYTES = 32 * 1024


class CiTrustError(ValueError):
    """Public actionable boundary, without keyring or document details."""

    def __init__(self) -> None:
        super().__init__(
            "CI key unavailable; provision a dedicated self-hosted runner with "
            "intent team ci provision and install its reviewed public trust config "
            "outside the checkout; set INTENT_CI_TRUST_PATH on the runner service"
        )


def _failure(caught: BaseException) -> NoReturn:
    caught.__traceback__ = None
    caught.__cause__ = None
    caught.__context__ = None
    if isinstance(caught, Exception):
        raise CiTrustError() from None
    raise caught.with_traceback(None) from None


def _safe_signal(error: BaseException) -> BaseException:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    return CiTrustError() if isinstance(error, Exception) else error


def _binding(project_id: str, repository_id: str, runner_id: str) -> dict[str, str]:
    # Validate the public scope without generating or reading any secret.
    record = CiRecipientRecord(
        project_id=project_id,
        repository_id=repository_id,
        runner_id=runner_id,
        public_key=base64.urlsafe_b64encode(b"\x01" * 32).rstrip(b"=").decode(),
    )
    if not record.repository_id.startswith("github.com/"):
        raise ValueError("invalid CI repository")
    return {
        "project_id": record.project_id,
        "repository_id": record.repository_id,
        "runner_id": record.runner_id,
    }


def provision_preview(project_id: str, repository_id: str, runner_id: str) -> dict[str, str]:
    """Network/keyring-free exact scope review before provisioning the runner key."""
    payload = {**_binding(project_id, repository_id, runner_id), "schema": "intent.ci-provision.v1"}
    return {
        **payload,
        "state": "confirmation_required",
        "preview_digest": "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest(),
    }


class CiKeyStore:
    """One stable X25519 secret per machine scope, stored only in the OS keyring."""

    def __init__(
        self,
        project_id: str,
        repository_id: str,
        runner_id: str,
        *,
        backend: _KeyringBackend = keyring,
        lock_root: Path | None = None,
    ) -> None:
        self._binding = _binding(project_id, repository_id, runner_id)
        self._backend = backend
        self._service = "intent-engineering-ci/" + project_id
        self._account = hashlib.sha256(_canonical_json(self._binding)).hexdigest()
        self._lock = _lock_target(
            lock_root
            if lock_root is not None
            else Path(tempfile.gettempdir()).resolve() / f"intent-engineering-ci-{os.getuid()}",
            self._service,
            self._account,
        )

    def _load(self, *, create: bool) -> bytes:
        with same_path_lock(self._lock):
            encoded = self._backend.get_password(self._service, self._account)
            if encoded is None and create:
                private = X25519PrivateKey.generate().private_bytes_raw()
                encoded = base64.urlsafe_b64encode(private).rstrip(b"=").decode()
                self._backend.set_password(self._service, self._account, encoded)
                if self._backend.get_password(self._service, self._account) != encoded:
                    raise ValueError("CI key write mismatch")
            if type(encoded) is not str:
                raise ValueError("CI key missing")
            return _decode_base64url(encoded, label="CI key", maximum_encoded=64, exact_decoded=32)

    def _descriptor(self, private: bytes) -> CiRecipientRecord:
        public = X25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
        return CiRecipientRecord(
            project_id=self._binding["project_id"],
            repository_id=self._binding["repository_id"],
            runner_id=self._binding["runner_id"],
            public_key=base64.urlsafe_b64encode(public).rstrip(b"=").decode(),
        )

    def provision(self) -> CiRecipientRecord:
        """Create/reuse the independent machine key, returning public material only."""
        caught: BaseException
        try:
            return self._descriptor(self._load(create=True))
        except BaseException as error:  # noqa: BLE001 - scrub secret frames, preserve cancellation
            caught = _safe_signal(error)
        _failure(caught)

    def _trust(self, config: CiTrustConfig) -> SharedStateTrust:
        private = self._load(create=False)
        if self._descriptor(private) != config.recipient:
            raise ValueError("CI key binding changed")
        return SharedStateTrust(
            config.recipient.project_id,
            config.recipient.repository_id,
            config.recipient.key_id,
            private,
            config.trusted_signing_keys(),
        )

    def __repr__(self) -> str:
        return "CiKeyStore()"


class CiTrustConfig(StrictModel):
    """Operator-reviewed public trust installed outside all runner checkouts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    recipient: CiRecipientRecord
    signing_public_keys: dict[str, str] = Field(min_length=1, max_length=64)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid CI trust version")
        return value

    def trusted_signing_keys(self) -> tuple[TrustedSigningKey, ...]:
        keys = []
        for name, encoded in sorted(self.signing_public_keys.items()):
            if len(encoded) != 44:
                raise ValueError("invalid signing public key")
            public = base64.b64decode(encoded, validate=True)
            if base64.b64encode(public).decode() != encoded:
                raise ValueError("invalid signing public key")
            keys.append(TrustedSigningKey(name, public))
        return tuple(keys)

    @model_validator(mode="after")
    def require_public_authority(self) -> CiTrustConfig:
        CiRecipientRecord.model_validate(self.recipient.model_dump(mode="python"))
        self.trusted_signing_keys()
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json"))


def read_ci_recipient(path: Path) -> CiRecipientRecord:
    """Read only a bounded public descriptor; importing never accesses the keyring."""
    caught: BaseException
    try:
        directory = SecureDirectory.open(path.absolute().parent)
        try:
            target = directory.file(path.name)
            try:
                content = target.read_optional_nonblocking(max_bytes=MAX_CI_PUBLIC_BYTES)
                if content is None:
                    raise ValueError("missing descriptor")
                loads_strict_object(content.decode())
                return CiRecipientRecord.model_validate_json(content)
            finally:
                target.close()
        finally:
            directory.close()
    except BaseException as error:  # noqa: BLE001 - untrusted document diagnostics
        caught = _safe_signal(error)
    _failure(caught)


class CiTrustProvider:
    """Read protected public configuration and reconstruct only the runner's own key."""

    def __init__(
        self,
        public_config_path: Path,
        *,
        backend: _KeyringBackend = keyring,
        lock_root: Path | None = None,
        checkout_root: Path | None = None,
    ) -> None:
        self._path = public_config_path
        self._backend = backend
        self._lock_root = lock_root
        self._checkout_root = checkout_root

    def _load(self) -> SharedStateTrust:
        path = Path(os.path.abspath(self._path))
        if not self._path.is_absolute() or (
            self._checkout_root is not None and path.is_relative_to(self._checkout_root.resolve())
        ):
            raise ValueError("CI trust is not protected")
        directory = SecureDirectory.open(path.parent)
        try:
            parent = os.fstat(directory.descriptor)
            if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
                raise ValueError("CI trust directory is writable")
            fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory.descriptor
            )
            try:
                metadata = os.fstat(fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                    or metadata.st_size > MAX_CI_PUBLIC_BYTES
                ):
                    raise ValueError("CI trust file is writable")
                content = os.read(fd, MAX_CI_PUBLIC_BYTES + 1)
                if len(content) > MAX_CI_PUBLIC_BYTES:
                    raise ValueError("CI trust too large")
            finally:
                os.close(fd)
        finally:
            directory.close()
        loads_strict_object(content.decode())
        config = CiTrustConfig.model_validate_json(content)
        if content != config.canonical_bytes():
            raise ValueError("CI trust is not canonical")
        recipient = config.recipient
        return CiKeyStore(
            recipient.project_id,
            recipient.repository_id,
            recipient.runner_id,
            backend=self._backend,
            lock_root=self._lock_root,
        )._trust(config)

    def load(self) -> SharedStateTrust:
        caught: BaseException
        try:
            return self._load()
        except BaseException as error:  # noqa: BLE001 - scrub keyring/private frames
            caught = _safe_signal(error)
        _failure(caught)


def ci_trust_from_environment(
    checkout_root: Path, environment: Mapping[str, str] | None = None
) -> CiTrustProvider:
    """Production state validation has no environment-private-key fallback."""
    selected = os.environ if environment is None else environment
    value = selected.get(CI_TRUST_PATH_ENV)
    if (
        "INTENT_CI_SHARED_STATE_TRUST" in selected
        or not value
        or len(value) > 4096
        or "\x00" in value
    ):
        raise CiTrustError()
    return CiTrustProvider(Path(value), checkout_root=checkout_root)
