"""Operating-system recipient keys bound to verified team enrollment."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Protocol, cast

import keyring
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.team_state.authority import derive_recipient_key_id, derive_signature_id
from intent_engineering.team_state.crypto import recipient_possession_proof
from intent_engineering.team_state.models import RecipientRecord

_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ACTOR = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_REPOSITORY_ID = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_ACCOUNT_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_GITHUB_LOGIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_KEY_ID = re.compile(r"^recipient:sha256:[0-9a-f]{64}$")
_MAX_CREDENTIAL_ID = 1024
_MAX_CREDENTIAL_PUBLIC_KEY = 4096


class RecipientKeyStoreError(ValueError):
    """Fixed public failure for an unavailable recipient key boundary."""

    def __init__(self) -> None:
        super().__init__("recipient key unavailable")


class GitHubIdentity(StrictModel):
    """Exact non-secret identity returned by a verified GitHub OAuth flow."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    account_id: Annotated[str, Field(pattern=_GITHUB_ACCOUNT_ID.pattern)]
    login: Annotated[str, Field(pattern=_GITHUB_LOGIN.pattern)]

    @field_validator("login")
    @classmethod
    def reject_ambiguous_login(cls, value: str) -> str:
        if "--" in value:
            raise ValueError("invalid GitHub identity")
        return value


class GitHubIdentityVerifier(Protocol):
    """Provider port that consumes an opaque proof and returns one exact GitHub actor."""

    def verify(self, proof: bytes) -> GitHubIdentity: ...


class RecipientEnrollmentBinding(StrictModel):
    """Complete authority context required before a private recipient key may exist."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    actor: Annotated[str, Field(pattern=_ACTOR.pattern)]
    github_identity: GitHubIdentity
    webauthn_credential_id: Annotated[str, Field(max_length=_MAX_CREDENTIAL_ID)]
    webauthn_credential_public_key: Annotated[str, Field(max_length=_MAX_CREDENTIAL_PUBLIC_KEY)]
    enrolled_at: datetime

    @field_validator("repository_id")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        host, owner, repository = value.split("/")
        if (
            host != host.lower()
            or owner in {".", ".."}
            or repository in {".", ".."}
            or repository.endswith(".git")
        ):
            raise ValueError("invalid team repository")
        return value

    @field_validator("webauthn_credential_id", "webauthn_credential_public_key")
    @classmethod
    def validate_webauthn_material(cls, value: str, info: ValidationInfo) -> str:
        credential_id = info.field_name == "webauthn_credential_id"
        maximum = _MAX_CREDENTIAL_ID if credential_id else _MAX_CREDENTIAL_PUBLIC_KEY
        if not value or len(value) > maximum or _BASE64URL.fullmatch(value) is None:
            raise ValueError("invalid WebAuthn credential")
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (UnicodeError, ValueError) as error:
            raise ValueError("invalid WebAuthn credential") from error
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if canonical != value or len(decoded) < (1 if credential_id else 16):
            raise ValueError("invalid WebAuthn credential")
        return value

    @field_validator("enrolled_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("enrollment time must be UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def revalidate_identity(self) -> RecipientEnrollmentBinding:
        GitHubIdentity.model_validate(self.github_identity.model_dump(mode="python"))
        return self


class RecipientKeyStore(Protocol):
    """The exact secret-storage surface used by restore and enrollment."""

    def generate(self, project_id: str, actor: str) -> RecipientRecord: ...

    def private_key(self, key_id: str) -> bytes: ...

    def delete(self, key_id: str) -> None: ...


class DeviceEnrollmentBinding(StrictModel):
    """Exact public identity and device slot allowed to own paired v2 keys."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )

    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    actor: Annotated[str, Field(pattern=_ACTOR.pattern)]
    github_account_id: Annotated[int, Field(gt=0)]
    github_login: Annotated[str, Field(pattern=_GITHUB_LOGIN.pattern)]
    device_id: Annotated[str, Field(pattern=r"^device:[0-9a-f]{32}$")]

    @field_validator("github_account_id", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid device GitHub identity")
        return value

    @field_validator("repository_id")
    @classmethod
    def validate_device_repository(cls, value: str) -> str:
        host, owner, repository = value.split("/")
        if (
            host != host.lower()
            or owner in {".", ".."}
            or repository in {".", ".."}
            or repository.endswith(".git")
        ):
            raise ValueError("invalid device repository")
        return value

    @model_validator(mode="after")
    def bind_actor_to_identity(self) -> DeviceEnrollmentBinding:
        if self.actor != f"github:{self.github_account_id}" or "--" in self.github_login:
            raise ValueError("invalid device identity binding")
        return self


@dataclass(frozen=True, slots=True)
class DevicePublicMaterial:
    device_id: str
    recipient_key_id: str
    recipient_public_key: bytes
    signature_id: str
    signing_public_key: bytes


@dataclass(frozen=True, slots=True)
class DeviceSealedIdentityProof:
    """Ciphertext-only result of sealing one bounded identity proof to the sponsor."""

    nonce: bytes
    ciphertext: bytes


class DeviceKeyStore(Protocol):
    """Non-exporting device recipient and signing key boundary."""

    def enrollment_binding(self) -> DeviceEnrollmentBinding: ...

    def create(self, binding: DeviceEnrollmentBinding) -> DevicePublicMaterial: ...

    def sign(self, signature_id: str, preimage: bytes) -> bytes: ...

    def decrypt_bundle(self, recipient_key_id: str, bundle: bytes, aad: bytes) -> bytes: ...

    def prove_recipient_possession(
        self, recipient_key_id: str, challenge_public_key: bytes, subject: bytes
    ) -> bytes: ...

    def seal_identity_proof(
        self,
        recipient_key_id: str,
        challenge_public_key: bytes,
        plaintext: bytes,
        aad: bytes,
    ) -> DeviceSealedIdentityProof: ...


def _prepare_device_failure(error: BaseException) -> BaseException:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    error.args = ()
    if isinstance(error, Exception):
        return RecipientKeyStoreError()
    return error


class _KeyringBackend(Protocol):
    def get_password(self, service: str, account: str) -> str | None: ...

    def set_password(self, service: str, account: str, value: str) -> None: ...

    def delete_password(self, service: str, account: str) -> None: ...


def _private_key_bytes() -> bytes:
    return X25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _signing_private_key_bytes() -> bytes:
    return Ed25519PrivateKey.generate().private_bytes_raw()


def _b64url(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def _decode_private(value: object) -> bytes:
    if type(value) is not str or not value or "=" in value or _BASE64URL.fullmatch(value) is None:
        raise ValueError("invalid private key")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(decoded) != 32 or _b64url(decoded) != value:
        raise ValueError("invalid private key")
    return decoded


def _raise_key_failure(caught: BaseException) -> None:
    if isinstance(caught, Exception):
        failure = RecipientKeyStoreError()
        failure.__cause__ = None
        failure.__context__ = None
        raise failure from None
    raise caught.with_traceback(None)


def _validated_binding(value: RecipientEnrollmentBinding) -> RecipientEnrollmentBinding:
    if type(value) is not RecipientEnrollmentBinding:
        raise ValueError("invalid enrollment binding")
    return RecipientEnrollmentBinding.model_validate(value.model_dump(mode="python"))


def _key_id(binding: RecipientEnrollmentBinding) -> str:
    content = json.dumps(
        {
            "schema": "intent.recipient-enrollment.v1",
            "project_id": binding.project_id,
            "repository_id": binding.repository_id,
            "actor": binding.actor,
            "github_account_id": binding.github_identity.account_id,
            "github_login": binding.github_identity.login,
            "webauthn_credential_id": binding.webauthn_credential_id,
            "webauthn_credential_public_key": binding.webauthn_credential_public_key,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"recipient:sha256:{hashlib.sha256(content).hexdigest()}"


def _default_lock_root() -> Path:
    return Path(tempfile.gettempdir()).resolve() / f"intent-engineering-recipient-{os.getuid()}"


def _lock_target(root: Path, service: str, key_id: str) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("invalid recipient lock root")
    root.mkdir(mode=0o700, parents=False, exist_ok=True)
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("invalid recipient lock root")
    digest = hashlib.sha256(f"{service}\0{key_id}".encode()).hexdigest()
    return root / digest


def _record(binding: RecipientEnrollmentBinding, private_key: bytes) -> RecipientRecord:
    public = (
        X25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    return RecipientRecord(
        key_id=_key_id(binding),
        project_id=binding.project_id,
        repository_id=binding.repository_id,
        actor=binding.actor,
        github_account_id=binding.github_identity.account_id,
        github_login=binding.github_identity.login,
        public_key=_b64url(public),
        webauthn_credential_id=binding.webauthn_credential_id,
        webauthn_credential_public_key=binding.webauthn_credential_public_key,
        enrolled_at=binding.enrolled_at,
    )


class KeyringRecipientKeyStore:
    """Store exactly one context-bound X25519 private key in the operating-system keyring."""

    def __init__(
        self,
        binding: RecipientEnrollmentBinding,
        *,
        backend: _KeyringBackend = keyring,
        private_key_source: Callable[[], bytes] = _private_key_bytes,
        lock_root: Path | None = None,
    ) -> None:
        self._binding = _validated_binding(binding)
        self._backend = backend
        self._private_key_source = private_key_source
        self._service = f"intent-engineering/{self._binding.project_id}"
        self._key_id = _key_id(self._binding)
        self._lock_target = _lock_target(
            lock_root if lock_root is not None else _default_lock_root(),
            self._service,
            self._key_id,
        )

    def _check_call(self, project_id: object, actor: object) -> None:
        if (
            type(project_id) is not str
            or type(actor) is not str
            or project_id != self._binding.project_id
            or actor != self._binding.actor
        ):
            raise ValueError("enrollment binding mismatch")

    def _generate(self, project_id: str, actor: str) -> RecipientRecord:
        private = b""
        encoded = ""
        try:
            self._check_call(project_id, actor)
            with same_path_lock(self._lock_target):
                if self._backend.get_password(self._service, self._key_id) is not None:
                    raise ValueError("duplicate recipient")
                private = self._private_key_source()
                if type(private) is not bytes or len(private) != 32:
                    raise ValueError("invalid private key source")
                encoded = _b64url(private)
                self._backend.set_password(self._service, self._key_id, encoded)
                if self._backend.get_password(self._service, self._key_id) != encoded:
                    raise ValueError("recipient key write mismatch")
                return _record(self._binding, private)
        finally:
            private = b""
            encoded = ""

    def generate(self, project_id: str, actor: str) -> RecipientRecord:
        """Generate and durably store one non-exportable-by-contract recipient key."""
        caught: BaseException | None = None
        try:
            return self._generate(project_id, actor)
        except BaseException as error:  # noqa: BLE001 - preserve cancellation type
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            caught = error
        if caught is not None:
            _raise_key_failure(caught)
        raise AssertionError("recipient key generation must return or raise")

    def _private_key(self, key_id: str) -> bytes:
        if type(key_id) is not str or _KEY_ID.fullmatch(key_id) is None or key_id != self._key_id:
            raise ValueError("recipient binding mismatch")
        with same_path_lock(self._lock_target):
            return _decode_private(self._backend.get_password(self._service, key_id))

    def private_key(self, key_id: str) -> bytes:
        """Read exactly one verified raw X25519 private key."""
        caught: BaseException | None = None
        try:
            return self._private_key(key_id)
        except BaseException as error:  # noqa: BLE001 - preserve cancellation type
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            caught = error
        if caught is not None:
            _raise_key_failure(caught)
        raise AssertionError("recipient key read must return or raise")

    def _delete(self, key_id: str) -> None:
        if type(key_id) is not str or _KEY_ID.fullmatch(key_id) is None or key_id != self._key_id:
            raise ValueError("recipient binding mismatch")
        with same_path_lock(self._lock_target):
            if self._backend.get_password(self._service, key_id) is None:
                raise ValueError("recipient key missing")
            self._backend.delete_password(self._service, key_id)
            if self._backend.get_password(self._service, key_id) is not None:
                raise ValueError("recipient key deletion mismatch")

    def delete(self, key_id: str) -> None:
        """Remove exactly the context-bound private key and verify absence."""
        caught: BaseException | None = None
        try:
            self._delete(key_id)
            return
        except BaseException as error:  # noqa: BLE001 - preserve cancellation type
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            caught = error
        if caught is not None:
            _raise_key_failure(caught)


class InMemoryRecipientKeyStore:
    """Deterministic process-local fake with production-equivalent validation."""

    def __init__(
        self,
        binding: RecipientEnrollmentBinding,
        *,
        private_key_source: Callable[[], bytes] = _private_key_bytes,
    ) -> None:
        self._binding = _validated_binding(binding)
        self._private_key_source = private_key_source
        self._keys: dict[str, bytes] = {}
        self._key_id = _key_id(self._binding)

    def generate(self, project_id: str, actor: str) -> RecipientRecord:
        private = b""
        caught: BaseException | None = None
        try:
            if (
                type(project_id) is not str
                or type(actor) is not str
                or project_id != self._binding.project_id
                or actor != self._binding.actor
                or self._key_id in self._keys
            ):
                raise RecipientKeyStoreError()
            private = self._private_key_source()
            if type(private) is not bytes or len(private) != 32:
                raise RecipientKeyStoreError()
            self._keys[self._key_id] = private
            return _record(self._binding, private)
        except BaseException as error:  # noqa: BLE001 - mirror production cancellation boundary
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            caught = error
        finally:
            private = b""
        if caught is not None:
            _raise_key_failure(caught)
        raise AssertionError("recipient key generation must return or raise")

    def private_key(self, key_id: str) -> bytes:
        if type(key_id) is not str or key_id != self._key_id or key_id not in self._keys:
            raise RecipientKeyStoreError()
        return bytes(self._keys[key_id])

    def delete(self, key_id: str) -> None:
        if type(key_id) is not str or key_id != self._key_id or key_id not in self._keys:
            raise RecipientKeyStoreError()
        del self._keys[key_id]

    def __repr__(self) -> str:
        return "InMemoryRecipientKeyStore()"


type RecipientKeyStoreFactory = Callable[[RecipientEnrollmentBinding], RecipientKeyStore]


def keyring_recipient_store(binding: RecipientEnrollmentBinding) -> RecipientKeyStore:
    """Construct the production OS-keyring adapter for one verified binding."""
    return cast(RecipientKeyStore, KeyringRecipientKeyStore(binding))


def restore_recipient(
    binding: RecipientEnrollmentBinding,
    store: RecipientKeyStore,
) -> RecipientRecord:
    """Reconstruct public recipient material from durable enrollment and its keyring key."""
    validated = _validated_binding(binding)
    private = b""
    try:
        private = store.private_key(_key_id(validated))
        if type(private) is not bytes or len(private) != 32:
            raise RecipientKeyStoreError()
        return _record(validated, private)
    finally:
        private = b""


def _validated_device_binding(value: DeviceEnrollmentBinding) -> DeviceEnrollmentBinding:
    if type(value) is not DeviceEnrollmentBinding:
        raise ValueError("invalid device enrollment binding")
    return DeviceEnrollmentBinding.model_validate(value.model_dump(mode="python"))


def _device_account(binding: DeviceEnrollmentBinding) -> str:
    content = json.dumps(
        {
            "actor": binding.actor,
            "device_id": binding.device_id,
            "github_account_id": binding.github_account_id,
            "github_login": binding.github_login,
            "project_id": binding.project_id,
            "repository_id": binding.repository_id,
            "schema": "intent.team-device-storage.v2",
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"device-slot:sha256:{hashlib.sha256(content).hexdigest()}"


class KeyringDeviceKeyStore:
    """Own one device's independent X25519 recipient and Ed25519 signing keys."""

    def __init__(
        self,
        binding: DeviceEnrollmentBinding,
        *,
        backend: _KeyringBackend = keyring,
        recipient_private_key_source: Callable[[], bytes] = _private_key_bytes,
        signing_private_key_source: Callable[[], bytes] = _signing_private_key_bytes,
        lock_root: Path | None = None,
    ) -> None:
        self._binding = _validated_device_binding(binding)
        self._backend = backend
        self._recipient_source = recipient_private_key_source
        self._signing_source = signing_private_key_source
        self._recipient_service = f"intent-engineering-device-recipient-v2/{binding.project_id}"
        self._signing_service = f"intent-engineering-device-signing-v2/{binding.project_id}"
        self._account = _device_account(binding)
        self._lock_target = _lock_target(
            lock_root if lock_root is not None else _default_lock_root(),
            f"{self._recipient_service}\0{self._signing_service}",
            self._account,
        )

    def _load_or_create(self, *, create: bool) -> tuple[bytes, bytes]:
        with same_path_lock(self._lock_target):
            recipient_text = self._backend.get_password(self._recipient_service, self._account)
            signing_text = self._backend.get_password(self._signing_service, self._account)
            if (recipient_text is None) != (signing_text is None):
                raise ValueError("incomplete device key pair")
            if recipient_text is None or signing_text is None:
                if not create:
                    raise ValueError("device key pair missing")
                recipient = self._recipient_source()
                signing = self._signing_source()
                if (
                    type(recipient) is not bytes
                    or len(recipient) != 32
                    or type(signing) is not bytes
                    or len(signing) != 32
                ):
                    raise ValueError("invalid device private key source")
                X25519PrivateKey.from_private_bytes(recipient)
                Ed25519PrivateKey.from_private_bytes(signing)
                recipient_encoded = _b64url(recipient)
                signing_encoded = _b64url(signing)
                try:
                    self._backend.set_password(
                        self._recipient_service, self._account, recipient_encoded
                    )
                    self._backend.set_password(
                        self._signing_service, self._account, signing_encoded
                    )
                    if (
                        self._backend.get_password(self._recipient_service, self._account)
                        != recipient_encoded
                        or self._backend.get_password(self._signing_service, self._account)
                        != signing_encoded
                    ):
                        raise ValueError("device key write mismatch")
                except BaseException as write_error:  # noqa: BLE001
                    self._rollback_known_empty_slots()
                    raise write_error.with_traceback(None)
                return recipient, signing
            return _decode_private(recipient_text), _decode_private(signing_text)

    def _rollback_known_empty_slots(self) -> None:
        services = (self._recipient_service, self._signing_service)
        cancellation: BaseException | None = None
        for service in services:
            try:
                self._backend.delete_password(service, self._account)
            except BaseException as cleanup_error:  # noqa: BLE001
                cleanup_error.__traceback__ = None
                cleanup_error.__cause__ = None
                cleanup_error.__context__ = None
                cleanup_error.args = ()
                if not isinstance(cleanup_error, Exception) and cancellation is None:
                    cancellation = cleanup_error
        absent = True
        for service in services:
            try:
                absent = self._backend.get_password(service, self._account) is None and absent
            except BaseException as verify_error:  # noqa: BLE001
                verify_error.__traceback__ = None
                verify_error.__cause__ = None
                verify_error.__context__ = None
                verify_error.args = ()
                if not isinstance(verify_error, Exception) and cancellation is None:
                    cancellation = verify_error
                absent = False
        if cancellation is not None:
            raise cancellation.with_traceback(None)
        if not absent:
            raise ValueError("device key rollback failed")

    def _material(self, recipient: bytes, signing: bytes) -> DevicePublicMaterial:
        recipient_public = (
            X25519PrivateKey.from_private_bytes(recipient).public_key().public_bytes_raw()
        )
        signing_public = (
            Ed25519PrivateKey.from_private_bytes(signing).public_key().public_bytes_raw()
        )
        return DevicePublicMaterial(
            device_id=self._binding.device_id,
            recipient_key_id=derive_recipient_key_id(
                self._binding.project_id,
                self._binding.repository_id,
                _b64url(recipient_public),
            ),
            recipient_public_key=recipient_public,
            signature_id=derive_signature_id(
                self._binding.project_id,
                self._binding.repository_id,
                _b64url(signing_public),
            ),
            signing_public_key=signing_public,
        )

    def enrollment_binding(self) -> DeviceEnrollmentBinding:
        """Return the immutable public slot binding without touching keyring material."""
        return DeviceEnrollmentBinding.model_validate(self._binding.model_dump(mode="python"))

    def create(self, binding: DeviceEnrollmentBinding) -> DevicePublicMaterial:
        """Create both private keys locally and return only their public material."""
        failure: BaseException | None = None
        recipient = b""
        signing = b""
        try:
            if _validated_device_binding(binding) != self._binding:
                raise ValueError("device enrollment binding mismatch")
            recipient, signing = self._load_or_create(create=True)
            return self._material(recipient, signing)
        except BaseException as error:  # noqa: BLE001
            failure = _prepare_device_failure(error)
        finally:
            recipient = b""
            signing = b""
        assert failure is not None
        del recipient, signing, binding, self
        raise failure.with_traceback(None)

    def sign(self, signature_id: str, preimage: bytes) -> bytes:
        """Sign one bounded preimage without returning device private material."""
        failure: BaseException | None = None
        recipient = b""
        signing = b""
        try:
            if type(preimage) is not bytes or not preimage or len(preimage) > 256 * 1024:
                raise ValueError("invalid device signing preimage")
            recipient, signing = self._load_or_create(create=False)
            if (
                type(signature_id) is not str
                or signature_id != self._material(recipient, signing).signature_id
            ):
                raise ValueError("device signing key mismatch")
            return Ed25519PrivateKey.from_private_bytes(signing).sign(preimage)
        except BaseException as error:  # noqa: BLE001
            failure = _prepare_device_failure(error)
        finally:
            recipient = b""
            signing = b""
        assert failure is not None
        del recipient, signing, signature_id, preimage, self
        raise failure.with_traceback(None)

    def decrypt_bundle(self, recipient_key_id: str, bundle: bytes, aad: bytes) -> bytes:
        """Decrypt bounded authenticated bytes without exporting a device key."""
        from intent_engineering.storage.jsonl.strict import loads_strict_object
        from intent_engineering.team_state.crypto import (
            EncryptedBundle,
            canonical_encrypted_bundle_bytes,
            decrypt_bundle,
        )
        from intent_engineering.team_state.models import MAX_BUNDLE_BYTES

        failure: BaseException | None = None
        recipient = signing = b""
        try:
            if (
                type(bundle) is not bytes
                or not bundle
                or len(bundle) > MAX_BUNDLE_BYTES
                or type(aad) is not bytes
                or not aad
                or len(aad) > 64 * 1024
            ):
                raise ValueError("invalid device decryption input")
            loads_strict_object(bundle.decode("utf-8"))
            envelope = EncryptedBundle.model_validate_json(bundle)
            if canonical_encrypted_bundle_bytes(envelope) != bundle:
                raise ValueError("invalid device decryption input")
            recipient, signing = self._load_or_create(create=False)
            if recipient_key_id != self._material(recipient, signing).recipient_key_id:
                raise ValueError("device recipient key mismatch")
            return decrypt_bundle(envelope, recipient, aad)
        except BaseException as error:  # noqa: BLE001 - secret-bearing boundary
            import traceback

            if error.__traceback__ is not None:
                traceback.clear_frames(error.__traceback__)
            error.__dict__.clear()
            failure = _prepare_device_failure(error)
        finally:
            recipient = signing = b""
        assert failure is not None
        del self, bundle, aad, recipient_key_id, recipient, signing
        raise failure.with_traceback(None)

    def prove_recipient_possession(
        self, recipient_key_id: str, challenge_public_key: bytes, subject: bytes
    ) -> bytes:
        """Produce a response-bound proof without exposing the recipient private key."""
        failure: BaseException | None = None
        recipient = b""
        signing = b""
        try:
            recipient, signing = self._load_or_create(create=False)
            if (
                type(recipient_key_id) is not str
                or recipient_key_id != self._material(recipient, signing).recipient_key_id
            ):
                raise ValueError("device recipient key mismatch")
            return recipient_possession_proof(recipient, challenge_public_key, subject)
        except BaseException as error:  # noqa: BLE001
            failure = _prepare_device_failure(error)
        finally:
            recipient = b""
            signing = b""
        assert failure is not None
        del recipient, signing, recipient_key_id, challenge_public_key, subject, self
        raise failure.with_traceback(None)

    def seal_identity_proof(
        self,
        recipient_key_id: str,
        challenge_public_key: bytes,
        plaintext: bytes,
        aad: bytes,
    ) -> DeviceSealedIdentityProof:
        """Encrypt an opaque one-time GitHub proof without exporting the recipient key."""
        failure: BaseException | None = None
        recipient = signing = shared = key = b""
        try:
            if (
                type(challenge_public_key) is not bytes
                or len(challenge_public_key) != 32
                or type(plaintext) is not bytes
                or not plaintext
                or len(plaintext) > 16 * 1024
                or type(aad) is not bytes
                or not aad
                or len(aad) > 64 * 1024
            ):
                raise ValueError("invalid identity proof sealing input")
            recipient, signing = self._load_or_create(create=False)
            if (
                type(recipient_key_id) is not str
                or recipient_key_id != self._material(recipient, signing).recipient_key_id
            ):
                raise ValueError("device recipient key mismatch")
            shared = X25519PrivateKey.from_private_bytes(recipient).exchange(
                X25519PublicKey.from_public_bytes(challenge_public_key)
            )
            key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=b"intent.team-enrollment-github-proof.v2\0" + aad,
            ).derive(shared)
            nonce = os.urandom(12)
            return DeviceSealedIdentityProof(
                nonce=nonce,
                ciphertext=AESGCM(key).encrypt(nonce, plaintext, aad),
            )
        except BaseException as error:  # noqa: BLE001
            failure = _prepare_device_failure(error)
        finally:
            recipient = signing = shared = key = plaintext = b""
        assert failure is not None
        del recipient_key_id, challenge_public_key, aad, self
        raise failure.with_traceback(None)

    def __repr__(self) -> str:
        return "KeyringDeviceKeyStore()"


__all__ = [
    "DeviceEnrollmentBinding",
    "DeviceKeyStore",
    "DevicePublicMaterial",
    "DeviceSealedIdentityProof",
    "GitHubIdentity",
    "GitHubIdentityVerifier",
    "InMemoryRecipientKeyStore",
    "KeyringDeviceKeyStore",
    "KeyringRecipientKeyStore",
    "RecipientEnrollmentBinding",
    "RecipientKeyStore",
    "RecipientKeyStoreError",
    "RecipientKeyStoreFactory",
    "keyring_recipient_store",
    "restore_recipient",
]
