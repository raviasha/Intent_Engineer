"""OS-keyring-backed Ed25519 authority for team-state publication."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Protocol

import keyring
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ConfigDict, Field, field_validator, model_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.team_state.authority import (
    derive_recipient_key_id,
    derive_root_key_id,
    derive_signature_id,
)
from intent_engineering.team_state.keys import DeviceEnrollmentBinding, DevicePublicMaterial
from intent_engineering.team_state.models import (
    TeamRootTrustV2,
    TeamStateManifest,
    TeamStateManifestV2,
    canonical_manifest_bytes,
)

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


class RootEnrollmentBinding(StrictModel):
    """Exact stable-root authority context approved for local key creation."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )

    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    authority_epoch: Annotated[int, Field(ge=1, le=2**31 - 1)]
    created_at: datetime
    predecessor_root_key_id: str | None = None

    @field_validator("authority_epoch", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid root enrollment epoch")
        return value

    @field_validator("repository_id")
    @classmethod
    def require_repository(cls, value: str) -> str:
        _validate_binding("root", value, "root:authority")
        return value

    @field_validator("created_at")
    @classmethod
    def require_utc_second(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
            raise ValueError("invalid root enrollment time")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_epoch_predecessor(self) -> RootEnrollmentBinding:
        predecessor = self.predecessor_root_key_id
        if (self.authority_epoch == 1) != (predecessor is None):
            raise ValueError("invalid root enrollment predecessor")
        if (
            predecessor is not None
            and re.fullmatch(r"root:sha256:[0-9a-f]{64}", predecessor) is None
        ):
            raise ValueError("invalid root enrollment predecessor")
        return self


class TeamRootKeyStore(Protocol):
    """Non-exporting root signing boundary."""

    def create(self, binding: RootEnrollmentBinding) -> TeamRootTrustV2: ...

    def sign(self, root_key_id: str, preimage: bytes) -> bytes: ...

    def root_trust(self) -> TeamRootTrustV2: ...


class ExistingRecipientDeviceSigner(Protocol):
    """Non-exporting v2 signer paired with a separately held legacy recipient."""

    def create_for_existing_recipient(
        self,
        binding: DeviceEnrollmentBinding,
        recipient_public_key: bytes,
    ) -> DevicePublicMaterial: ...

    def sign(self, signature_id: str, preimage: bytes) -> bytes: ...


class KeyringExistingRecipientDeviceSigner:
    """Create only a new v2 Ed25519 signer while reusing a public X25519 recipient."""

    def __init__(
        self,
        binding: DeviceEnrollmentBinding,
        *,
        backend: _KeyringBackend = keyring,
        private_key_source: Callable[[], bytes] = _private_key_bytes,
        lock_root: Path | None = None,
    ) -> None:
        self._binding = DeviceEnrollmentBinding.model_validate(binding.model_dump(mode="python"))
        self._backend = backend
        self._private_key_source = private_key_source
        self._service = f"intent-engineering-device-signing-v2/{binding.project_id}"
        account_binding = json.dumps(
            {
                **binding.model_dump(mode="json"),
                "schema": "intent.migration-device-signing-storage.v2",
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        self._account = f"migration-device:sha256:{hashlib.sha256(account_binding).hexdigest()}"
        self._lock_target = _lock_target(
            lock_root if lock_root is not None else _default_lock_root(),
            self._service,
            self._account,
        )

    def _private(self, *, create: bool = False) -> bytes:
        with same_path_lock(self._lock_target):
            stored = self._backend.get_password(self._service, self._account)
            if stored is None:
                if not create:
                    raise ValueError("device signing key missing")
                private = self._private_key_source()
                if type(private) is not bytes or len(private) != 32:
                    raise ValueError("invalid device signing key source")
                Ed25519PrivateKey.from_private_bytes(private)
                encoded = _b64url(private)
                self._backend.set_password(self._service, self._account, encoded)
                if self._backend.get_password(self._service, self._account) != encoded:
                    raise ValueError("device signing key write mismatch")
                return private
            return _decode_private(stored)

    def create_for_existing_recipient(
        self,
        binding: DeviceEnrollmentBinding,
        recipient_public_key: bytes,
    ) -> DevicePublicMaterial:
        failure: BaseException | None = None
        private = b""
        try:
            value = DeviceEnrollmentBinding.model_validate(binding.model_dump(mode="python"))
            if (
                value != self._binding
                or type(recipient_public_key) is not bytes
                or len(recipient_public_key) != 32
            ):
                raise ValueError("migration device binding changed")
            private = self._private(create=True)
            signing_public = (
                Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
            )
            return DevicePublicMaterial(
                device_id=value.device_id,
                recipient_key_id=derive_recipient_key_id(
                    value.project_id, value.repository_id, _b64url(recipient_public_key)
                ),
                recipient_public_key=recipient_public_key,
                signature_id=derive_signature_id(
                    value.project_id, value.repository_id, _b64url(signing_public)
                ),
                signing_public_key=signing_public,
            )
        except BaseException as error:  # noqa: BLE001
            failure = _prepare_root_failure(error)
        finally:
            private = b""
        assert failure is not None
        raise failure.with_traceback(None)

    def sign(self, signature_id: str, preimage: bytes) -> bytes:
        failure: BaseException | None = None
        private = b""
        try:
            if type(preimage) is not bytes or not preimage or len(preimage) > 256 * 1024:
                raise ValueError("invalid device signing preimage")
            private = self._private()
            public = Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
            expected = derive_signature_id(
                self._binding.project_id,
                self._binding.repository_id,
                _b64url(public),
            )
            if signature_id != expected:
                raise ValueError("device signer binding changed")
            return Ed25519PrivateKey.from_private_bytes(private).sign(preimage)
        except BaseException as error:  # noqa: BLE001
            failure = _prepare_root_failure(error)
        finally:
            private = b""
        assert failure is not None
        raise failure.with_traceback(None)

    def __repr__(self) -> str:
        return "KeyringExistingRecipientDeviceSigner()"


class LegacyMigrationRelease(Protocol):
    """Public portion of the exact verified v1 release being bridged."""

    @property
    def manifest(self) -> TeamStateManifest: ...

    @property
    def manifest_bytes(self) -> bytes: ...


def canonical_v1_migration_preimage(
    current: LegacyMigrationRelease,
    manifest: TeamStateManifestV2,
) -> bytes:
    """Bind legacy approval to one exact first-v2 manifest under a distinct domain."""
    if type(current.manifest) is not TeamStateManifest or type(manifest) is not TeamStateManifestV2:
        raise TypeError("invalid migration release")
    legacy = TeamStateManifest.model_validate(current.manifest.model_dump(mode="python"))
    next_manifest = TeamStateManifestV2.model_validate(manifest.model_dump(mode="python"))
    legacy_bytes = canonical_manifest_bytes(legacy)
    if type(current.manifest_bytes) is not bytes or current.manifest_bytes != legacy_bytes:
        raise ValueError("legacy manifest binding changed")
    return json.dumps(
        {
            "authority_digest": next_manifest.authority_digest,
            "bundle_digest": next_manifest.bundle_digest,
            "domain": "intent.team-state-v1-migration.v2",
            "manifest_digest": "sha256:"
            + hashlib.sha256(next_manifest.canonical_bytes()).hexdigest(),
            "parent_bundle_digest": next_manifest.parent_bundle_digest,
            "prior_manifest_digest": "sha256:" + hashlib.sha256(legacy_bytes).hexdigest(),
            "project_id": next_manifest.project_id,
            "repository_id": next_manifest.repository_id,
            "root_key_id": next_manifest.root_key_id,
            "schema_version": 2,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _prepare_root_failure(error: BaseException) -> BaseException:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    error.args = ()
    if isinstance(error, Exception):
        return SigningKeyStoreError()
    return error


def _validated_root_binding(value: RootEnrollmentBinding) -> RootEnrollmentBinding:
    if type(value) is not RootEnrollmentBinding:
        raise ValueError("invalid root enrollment binding")
    return RootEnrollmentBinding.model_validate(value.model_dump(mode="python"))


def _root_account(binding: RootEnrollmentBinding) -> str:
    content = json.dumps(
        {
            "authority_epoch": binding.authority_epoch,
            "project_id": binding.project_id,
            "repository_id": binding.repository_id,
            "schema": "intent.team-root-storage.v2",
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"root-slot:sha256:{hashlib.sha256(content).hexdigest()}"


class KeyringTeamRootKeyStore:
    """Create and use one stable Ed25519 team root without exporting its private key."""

    def __init__(
        self,
        binding: RootEnrollmentBinding,
        *,
        backend: _KeyringBackend = keyring,
        private_key_source: Callable[[], bytes] = _private_key_bytes,
        lock_root: Path | None = None,
    ) -> None:
        self._binding = _validated_root_binding(binding)
        self._backend = backend
        self._private_key_source = private_key_source
        self._service = f"intent-engineering-root-v2/{binding.project_id}"
        self._account = _root_account(binding)
        self._lock_target = _lock_target(
            lock_root if lock_root is not None else _default_lock_root(),
            self._service,
            self._account,
        )

    def _private(self, *, create: bool) -> bytes:
        with same_path_lock(self._lock_target):
            stored = self._backend.get_password(self._service, self._account)
            if stored is None:
                if not create:
                    raise ValueError("root key missing")
                generated = self._private_key_source()
                if type(generated) is not bytes or len(generated) != 32:
                    raise ValueError("invalid root private key source")
                encoded = _b64url(generated)
                self._backend.set_password(self._service, self._account, encoded)
                if self._backend.get_password(self._service, self._account) != encoded:
                    raise ValueError("root key write mismatch")
                return generated
            return _decode_private(stored)

    def _trust(self, private: bytes) -> TeamRootTrustV2:
        public = Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
        public_text = _b64url(public)
        return TeamRootTrustV2(
            project_id=self._binding.project_id,
            repository_id=self._binding.repository_id,
            authority_epoch=self._binding.authority_epoch,
            root_key_id=derive_root_key_id(
                self._binding.project_id, self._binding.repository_id, public_text
            ),
            root_public_key=public_text,
            created_at=self._binding.created_at,
            predecessor_root_key_id=self._binding.predecessor_root_key_id,
        )

    def create(self, binding: RootEnrollmentBinding) -> TeamRootTrustV2:
        """Create the bound root once, or return the existing root in the same slot."""
        failure: BaseException | None = None
        private = b""
        try:
            if _validated_root_binding(binding) != self._binding:
                raise ValueError("root enrollment binding mismatch")
            private = self._private(create=True)
            return self._trust(private)
        except BaseException as error:  # noqa: BLE001
            failure = _prepare_root_failure(error)
        finally:
            private = b""
        assert failure is not None
        del private, binding, self
        raise failure.with_traceback(None)

    def root_trust(self) -> TeamRootTrustV2:
        """Return public trust derived from the existing root secret."""
        failure: BaseException | None = None
        private = b""
        try:
            private = self._private(create=False)
            return self._trust(private)
        except BaseException as error:  # noqa: BLE001
            failure = _prepare_root_failure(error)
        finally:
            private = b""
        assert failure is not None
        del private, self
        raise failure.with_traceback(None)

    def sign(self, root_key_id: str, preimage: bytes) -> bytes:
        """Sign an exact bounded root-authority preimage."""
        failure: BaseException | None = None
        private = b""
        try:
            if type(preimage) is not bytes or not preimage or len(preimage) > 256 * 1024:
                raise ValueError("invalid root signing preimage")
            private = self._private(create=False)
            trust = self._trust(private)
            if type(root_key_id) is not str or root_key_id != trust.root_key_id:
                raise ValueError("root key binding mismatch")
            return Ed25519PrivateKey.from_private_bytes(private).sign(preimage)
        except BaseException as error:  # noqa: BLE001
            failure = _prepare_root_failure(error)
        finally:
            private = b""
        assert failure is not None
        del private, root_key_id, preimage, self
        raise failure.with_traceback(None)

    def __repr__(self) -> str:
        return "KeyringTeamRootKeyStore()"


__all__ = [
    "ExistingRecipientDeviceSigner",
    "KeyringExistingRecipientDeviceSigner",
    "KeyringTeamRootKeyStore",
    "LegacyMigrationRelease",
    "RootEnrollmentBinding",
    "SigningKeyStore",
    "SigningKeyStoreError",
    "TeamRootKeyStore",
    "canonical_v1_migration_preimage",
]
