"""Operating-system recipient keys bound to verified team enrollment."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol, cast

import keyring
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from intent_engineering.core.models._base import StrictModel
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


def _b64url(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def _decode_private(value: object) -> bytes:
    if type(value) is not str or not value or "=" in value or _BASE64URL.fullmatch(value) is None:
        raise ValueError("invalid private key")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(decoded) != 32 or _b64url(decoded) != value:
        raise ValueError("invalid private key")
    return decoded


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
    ) -> None:
        self._binding = _validated_binding(binding)
        self._backend = backend
        self._private_key_source = private_key_source
        self._service = f"intent-engineering/{self._binding.project_id}"
        self._key_id = _key_id(self._binding)

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
            if self._backend.get_password(self._service, self._key_id) is not None:
                raise ValueError("duplicate recipient")
            private = self._private_key_source()
            if type(private) is not bytes or len(private) != 32:
                raise ValueError("invalid private key source")
            encoded = _b64url(private)
            self._backend.set_password(self._service, self._key_id, encoded)
            if self._backend.get_password(self._service, self._key_id) != encoded:
                try:
                    self._backend.delete_password(self._service, self._key_id)
                except Exception as cleanup_error:  # noqa: BLE001 - scrub backend detail
                    cleanup_error.__traceback__ = None
                    cleanup_error.__cause__ = None
                    cleanup_error.__context__ = None
                raise ValueError("recipient key write mismatch")
            return _record(self._binding, private)
        finally:
            private = b""
            encoded = ""

    @staticmethod
    def _raise_failure(caught: BaseException) -> None:
        if isinstance(caught, Exception):
            failure = RecipientKeyStoreError()
            failure.__cause__ = None
            failure.__context__ = None
            raise failure from None
        raise caught.with_traceback(None)

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
            self._raise_failure(caught)
        raise AssertionError("recipient key generation must return or raise")

    def _private_key(self, key_id: str) -> bytes:
        if type(key_id) is not str or _KEY_ID.fullmatch(key_id) is None or key_id != self._key_id:
            raise ValueError("recipient binding mismatch")
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
            self._raise_failure(caught)
        raise AssertionError("recipient key read must return or raise")

    def _delete(self, key_id: str) -> None:
        if type(key_id) is not str or _KEY_ID.fullmatch(key_id) is None or key_id != self._key_id:
            raise ValueError("recipient binding mismatch")
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
            self._raise_failure(caught)


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
        finally:
            private = b""

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


__all__ = [
    "GitHubIdentity",
    "GitHubIdentityVerifier",
    "InMemoryRecipientKeyStore",
    "KeyringRecipientKeyStore",
    "RecipientEnrollmentBinding",
    "RecipientKeyStore",
    "RecipientKeyStoreError",
    "RecipientKeyStoreFactory",
    "keyring_recipient_store",
]
