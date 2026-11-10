"""Canonical X25519/AES-256-GCM multi-recipient bundle envelope."""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal, Never

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.team_state.models import (
    ENCRYPTION_ALGORITHM,
    MAX_BUNDLE_BYTES,
    MAX_MANIFEST_BYTES,
    EncryptionRecipient,
    validate_encryption_recipient,
)

ALGORITHM: Final = ENCRYPTION_ALGORITHM
MAX_RECIPIENTS: Final = 64
_NONCE_BYTES: Final = 12
_KEY_BYTES: Final = 32
_MAX_POSSESSION_SUBJECT_BYTES: Final = 64 * 1024
_KEY_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPOSITORY_ID = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _b64encode(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def _b64decode(
    value: str,
    *,
    expected_size: int | None = None,
    allow_empty: bool = False,
) -> bytes:
    if (
        type(value) is not str
        or (not value and not allow_empty)
        or len(value) > MAX_BUNDLE_BYTES * 2
    ):
        raise ValueError("invalid encrypted bundle encoding")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (UnicodeError, ValueError) as error:
        raise ValueError("invalid encrypted bundle encoding") from error
    if _b64encode(decoded) != value or (
        expected_size is not None and len(decoded) != expected_size
    ):
        raise ValueError("invalid encrypted bundle encoding")
    return decoded


def _json_tuple(value: object, info: ValidationInfo) -> object:
    if info.mode == "json" and type(value) is list:
        return tuple(value)
    if info.mode == "python" and type(value) is not tuple:
        raise ValueError("encrypted bundle collections must be tuples")
    return value


class _EnvelopeModel(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        strict=True,
        validate_default=True,
    )


class WrappedContentKey(_EnvelopeModel):
    recipient_key_id: Annotated[str, Field(pattern=_KEY_ID.pattern)]
    nonce: str
    ciphertext: str

    @model_validator(mode="after")
    def require_canonical_fields(self) -> WrappedContentKey:
        _b64decode(self.nonce, expected_size=_NONCE_BYTES)
        # A wrapped 256-bit AES key carries the 16-byte GCM tag.
        _b64decode(self.ciphertext, expected_size=_KEY_BYTES + 16)
        return self


class AuthenticatedBundleContext(_EnvelopeModel):
    """Exact canonical authority carried as AEAD additional authenticated data."""

    schema_version: Literal[1] = 1
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    graph_version: Annotated[int, Field(ge=1)]
    parent_bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)] | None = None
    encryption_algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"] = ALGORITHM
    recipient_key_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=MAX_RECIPIENTS)]
    required_signature_ids: Annotated[
        tuple[str, ...], Field(min_length=1, max_length=MAX_RECIPIENTS)
    ]
    created_at: datetime

    @field_validator("graph_version", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid authenticated bundle graph version")
        return value

    @field_validator("recipient_key_ids", "required_signature_ids", mode="before")
    @classmethod
    def require_identifier_tuples(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @field_validator("recipient_key_ids", "required_signature_ids")
    @classmethod
    def require_sorted_unique_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            values != tuple(sorted(values))
            or len(values) != len(set(values))
            or any(_KEY_ID.fullmatch(value) is None for value in values)
        ):
            raise ValueError("authenticated bundle identifiers must be sorted and unique")
        return values

    @field_validator("created_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("authenticated bundle time must be UTC")
        return value.astimezone(UTC)

    @field_validator("repository_id")
    @classmethod
    def require_canonical_repository_id(cls, value: str) -> str:
        _host, owner, repository = value.split("/")
        if owner in {".", ".."} or repository in {".", ".."} or repository.endswith(".git"):
            raise ValueError("invalid authenticated bundle repository identity")
        return value


class AuthenticatedBundleContextV2(_EnvelopeModel):
    """Final authority-bound AAD for version-two state archives."""

    schema_version: Literal[2] = 2
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    graph_version: Annotated[int, Field(ge=1)]
    parent_bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    encryption_algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"] = ALGORITHM
    recipient_key_ids: Annotated[tuple[str, ...], Field(min_length=2, max_length=MAX_RECIPIENTS)]
    authority_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    authority_epoch: Annotated[int, Field(ge=1, le=2**31 - 1)]
    authority_sequence: Annotated[int, Field(ge=1, le=2**63 - 1)]
    root_key_id: Annotated[str, Field(pattern=_KEY_ID.pattern)]
    created_at: datetime

    @field_validator("graph_version", "authority_epoch", "authority_sequence", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid authenticated bundle integer")
        return value

    @field_validator("recipient_key_ids", mode="before")
    @classmethod
    def require_identifier_tuple(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @field_validator("recipient_key_ids")
    @classmethod
    def require_sorted_unique_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            values != tuple(sorted(values))
            or len(values) != len(set(values))
            or any(_KEY_ID.fullmatch(value) is None for value in values)
        ):
            raise ValueError("authenticated bundle identifiers must be sorted and unique")
        return values

    @field_validator("created_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
            raise ValueError("authenticated bundle time must be UTC-second precision")
        return value.astimezone(UTC)

    @field_validator("repository_id")
    @classmethod
    def require_canonical_repository_id(cls, value: str) -> str:
        _host, owner, repository = value.split("/")
        if owner in {".", ".."} or repository in {".", ".."} or repository.endswith(".git"):
            raise ValueError("invalid authenticated bundle repository identity")
        return value


AuthenticatedContext = AuthenticatedBundleContext | AuthenticatedBundleContextV2


def canonical_authenticated_context_bytes(context: AuthenticatedContext) -> bytes:
    """Return the only AAD encoding accepted by public encryption boundaries."""
    if type(context) is AuthenticatedBundleContext:
        validated: AuthenticatedContext = AuthenticatedBundleContext.model_validate(
            context.model_dump(mode="python")
        )
    elif type(context) is AuthenticatedBundleContextV2:
        validated = AuthenticatedBundleContextV2.model_validate(context.model_dump(mode="python"))
    else:
        raise TypeError("context must be a supported AuthenticatedBundleContext")
    content = _canonical_json(validated.model_dump(mode="json"))
    if not content or len(content) > MAX_MANIFEST_BYTES:
        raise ValueError("invalid encrypted bundle AAD")
    return content


def _parse_authenticated_context(aad: bytes) -> AuthenticatedContext:
    if type(aad) is not bytes or not aad or len(aad) > MAX_MANIFEST_BYTES:
        raise ValueError("invalid encrypted bundle AAD")
    try:
        raw = loads_strict_object(aad.decode("utf-8"))
        schema_version = raw.get("schema_version")
        if type(schema_version) is not int:
            raise ValueError("invalid encrypted bundle AAD")
        if schema_version == 1:
            context: AuthenticatedContext = AuthenticatedBundleContext.model_validate_json(aad)
        elif schema_version == 2:
            context = AuthenticatedBundleContextV2.model_validate_json(aad)
        else:
            raise ValueError("invalid encrypted bundle AAD")
    except (TypeError, UnicodeError, ValueError, ValidationError) as error:
        raise ValueError("invalid encrypted bundle AAD") from error
    if canonical_authenticated_context_bytes(context) != aad:
        raise ValueError("invalid noncanonical encrypted bundle AAD")
    return context


class EncryptedBundle(_EnvelopeModel):
    schema_version: Literal[1] = 1
    algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"] = ALGORITHM
    ephemeral_public_key: str
    nonce: str
    ciphertext: str
    wrapped_keys: Annotated[
        tuple[WrappedContentKey, ...], Field(min_length=1, max_length=MAX_RECIPIENTS)
    ]

    @field_validator("wrapped_keys", mode="before")
    @classmethod
    def require_wrapped_tuple(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @model_validator(mode="after")
    def require_canonical_fields(self) -> EncryptedBundle:
        ids = tuple(item.recipient_key_id for item in self.wrapped_keys)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("encrypted bundle recipients must be sorted and unique")
        _b64decode(self.ephemeral_public_key, expected_size=_KEY_BYTES)
        payload_nonce = _b64decode(self.nonce, expected_size=_NONCE_BYTES)
        ciphertext = _b64decode(self.ciphertext)
        if len(ciphertext) < 16 or len(ciphertext) > MAX_BUNDLE_BYTES:
            raise ValueError("invalid encrypted bundle ciphertext")
        wrapped_nonces = tuple(
            _b64decode(item.nonce, expected_size=_NONCE_BYTES) for item in self.wrapped_keys
        )
        if len({payload_nonce, *wrapped_nonces}) != len(wrapped_nonces) + 1:
            raise ValueError("encrypted bundle nonces must be unique")
        return self

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: object | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> EncryptedBundle:
        if isinstance(json_data, str):
            content = json_data.encode("utf-8")
        elif isinstance(json_data, (bytes, bytearray)):
            content = bytes(json_data)
        else:
            raise TypeError("encrypted bundle JSON must be bytes or text")
        if not content or len(content) > MAX_BUNDLE_BYTES:
            raise ValueError("invalid encrypted bundle")
        try:
            loads_strict_object(content.decode("utf-8"))
        except (TypeError, UnicodeError, ValueError) as error:
            raise ValueError("invalid encrypted bundle") from error
        parsed = super().model_validate_json(
            content,
            strict=strict,
            extra=extra,  # type: ignore[arg-type]
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )
        if canonical_encrypted_bundle_bytes(parsed) != content:
            raise ValueError("noncanonical encrypted bundle")
        return parsed


# Historical name used by the hardened restore implementation.
EncryptedStateBundle = EncryptedBundle


def canonical_encrypted_bundle_bytes(bundle: EncryptedBundle) -> bytes:
    """Return the exact schema-version-1 envelope bytes bound by the manifest."""
    if not isinstance(bundle, EncryptedBundle):
        raise TypeError("bundle must be an EncryptedBundle")
    try:
        validated = EncryptedBundle.model_validate(bundle.model_dump(mode="python"))
    except ValidationError as error:
        raise ValueError("invalid encrypted bundle") from error
    content = _canonical_json(validated.model_dump(mode="json"))
    if not content or len(content) > MAX_BUNDLE_BYTES:
        raise ValueError("encrypted bundle is oversized")
    return content


def _derive_wrapping_key(shared_secret: bytes, aad: bytes, recipient_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=None,
        info=b"intent.shared-state.wrap.v1\0" + aad + b"\0" + recipient_id.encode(),
    ).derive(shared_secret)


def _derive_possession_proof(shared_secret: bytes, subject: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=hashes.Hash(hashes.SHA256()).finalize(),
        info=b"intent.recipient-possession.v2\0" + subject,
    ).derive(shared_secret)


def _prepare_possession_failure(error: BaseException) -> BaseException:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    error.args = ()
    if isinstance(error, Exception):
        return ValueError("unable to prove recipient possession")
    return error


def recipient_possession_proof(
    recipient_private_key: bytes,
    challenge_public_key: bytes,
    subject: bytes,
) -> bytes:
    """Prove possession of one X25519 recipient key for an exact public subject."""
    failure: BaseException | None = None
    private: X25519PrivateKey | None = None
    challenge: X25519PublicKey | None = None
    try:
        if (
            type(recipient_private_key) is not bytes
            or len(recipient_private_key) != _KEY_BYTES
            or type(challenge_public_key) is not bytes
            or len(challenge_public_key) != _KEY_BYTES
            or type(subject) is not bytes
            or not subject
            or len(subject) > _MAX_POSSESSION_SUBJECT_BYTES
        ):
            raise ValueError("invalid recipient possession inputs")
        private = X25519PrivateKey.from_private_bytes(recipient_private_key)
        challenge = X25519PublicKey.from_public_bytes(challenge_public_key)
        return _derive_possession_proof(private.exchange(challenge), subject)
    except BaseException as error:  # noqa: BLE001 - scrub and preserve cancellation
        failure = _prepare_possession_failure(error)
    finally:
        private = None
        challenge = None
    assert failure is not None
    del private, challenge, recipient_private_key, challenge_public_key, subject
    raise failure.with_traceback(None)


def verify_recipient_possession_proof(
    recipient_public_key: bytes,
    challenge_private_key: bytes,
    subject: bytes,
    proof: bytes,
) -> bool:
    """Verify one domain-separated recipient possession proof without revealing a key."""
    try:
        if (
            type(recipient_public_key) is not bytes
            or len(recipient_public_key) != _KEY_BYTES
            or type(challenge_private_key) is not bytes
            or len(challenge_private_key) != _KEY_BYTES
            or type(subject) is not bytes
            or not subject
            or len(subject) > _MAX_POSSESSION_SUBJECT_BYTES
            or type(proof) is not bytes
            or len(proof) != _KEY_BYTES
        ):
            return False
        challenge = X25519PrivateKey.from_private_bytes(challenge_private_key)
        recipient = X25519PublicKey.from_public_bytes(recipient_public_key)
        expected = _derive_possession_proof(challenge.exchange(recipient), subject)
        return hmac.compare_digest(expected, proof)
    except (TypeError, ValueError):
        return False
    finally:
        del challenge_private_key


def _random_bytes(size: int) -> bytes:
    return os.urandom(size)


def _fresh_nonce(used: set[bytes]) -> bytes:
    for _attempt in range(16):
        nonce = _random_bytes(_NONCE_BYTES)
        if nonce not in used:
            used.add(nonce)
            return nonce
    raise ValueError("unable to generate a unique encrypted bundle nonce")


class _InvalidRecipients(ValueError):
    pass


class _InvalidAuthenticatedContext(ValueError):
    pass


def _validate_inputs(plaintext: bytes, aad: bytes) -> AuthenticatedContext:
    if type(plaintext) is not bytes or len(plaintext) > MAX_BUNDLE_BYTES - 16:
        raise ValueError("invalid encrypted bundle plaintext")
    try:
        return _parse_authenticated_context(aad)
    except ValueError as error:
        raise _InvalidAuthenticatedContext("invalid encrypted bundle AAD") from error


def _encrypt_bundle_for_public_keys(
    plaintext: bytes,
    recipient_public_keys: Mapping[str, bytes],
    aad: bytes,
) -> EncryptedBundle:
    context = _validate_inputs(plaintext, aad)
    if type(recipient_public_keys) is not dict:
        raise _InvalidRecipients("invalid recipient set")
    recipient_ids = tuple(recipient_public_keys)
    if (
        not recipient_ids
        or len(recipient_ids) > MAX_RECIPIENTS
        or recipient_ids != tuple(sorted(recipient_ids))
        or len(recipient_ids) != len(set(recipient_ids))
        or any(_KEY_ID.fullmatch(item) is None for item in recipient_ids)
        or recipient_ids != context.recipient_key_ids
    ):
        raise _InvalidRecipients("invalid recipient set")

    content_key = AESGCM.generate_key(bit_length=256)
    used_nonces: set[bytes] = set()
    payload_nonce = _fresh_nonce(used_nonces)
    ephemeral = X25519PrivateKey.generate()
    wrapped: list[WrappedContentKey] = []
    for recipient_id in recipient_ids:
        public_bytes = recipient_public_keys[recipient_id]
        if type(public_bytes) is not bytes or len(public_bytes) != _KEY_BYTES:
            raise _InvalidRecipients("invalid recipient public key")
        public = X25519PublicKey.from_public_bytes(public_bytes)
        shared = ephemeral.exchange(public)
        wrapping_key = _derive_wrapping_key(shared, aad, recipient_id)
        nonce = _fresh_nonce(used_nonces)
        wrapped.append(
            WrappedContentKey(
                recipient_key_id=recipient_id,
                nonce=_b64encode(nonce),
                ciphertext=_b64encode(
                    AESGCM(wrapping_key).encrypt(
                        nonce,
                        content_key,
                        aad + b"\0" + recipient_id.encode(),
                    )
                ),
            )
        )
    return EncryptedBundle(
        ephemeral_public_key=_b64encode(ephemeral.public_key().public_bytes_raw()),
        nonce=_b64encode(payload_nonce),
        ciphertext=_b64encode(AESGCM(content_key).encrypt(payload_nonce, plaintext, aad)),
        wrapped_keys=tuple(wrapped),
    )


def _raise_safely(
    pending: BaseException,
    *,
    operation: str,
    invalid_recipients: bool = False,
    invalid_aad: bool = False,
) -> Never:
    pending.__traceback__ = None
    if not isinstance(pending, Exception):
        raise pending.with_traceback(None)
    if invalid_recipients:
        raise ValueError("invalid encrypted bundle recipient set")
    if invalid_aad:
        raise ValueError("invalid encrypted bundle AAD or recipient authority")
    raise ValueError(f"unable to {operation} encrypted bundle")


def encrypt_bundle(
    plaintext: bytes,
    recipients: tuple[EncryptionRecipient, ...],
    aad: bytes,
) -> EncryptedBundle:
    """Encrypt exact plaintext once and wrap its random key for every recipient."""
    pending: BaseException | None = None
    invalid_recipients = False
    invalid_aad = False
    try:
        if type(recipients) is not tuple or not recipients:
            raise _InvalidRecipients("invalid recipient set")
        validated = tuple(validate_encryption_recipient(item) for item in recipients)
        ids = tuple(item.key_id for item in validated)
        authorities = {(item.project_id, item.repository_id) for item in validated}
        try:
            context = _parse_authenticated_context(aad)
        except ValueError as error:
            raise _InvalidAuthenticatedContext from error
        if (
            ids != tuple(sorted(ids))
            or len(ids) != len(set(ids))
            or len(authorities) != 1
            or authorities != {(context.project_id, context.repository_id)}
            or ids != context.recipient_key_ids
        ):
            raise _InvalidRecipients("invalid recipient set")
        public_keys = {
            item.key_id: _b64decode(item.public_key, expected_size=_KEY_BYTES) for item in validated
        }
        return _encrypt_bundle_for_public_keys(plaintext, public_keys, aad)
    except BaseException as error:  # noqa: BLE001 - scrub and preserve cancellation
        error.__traceback__ = None
        pending = error
        invalid_recipients = isinstance(error, (_InvalidRecipients, ValidationError))
        invalid_aad = isinstance(error, _InvalidAuthenticatedContext)
    finally:
        # Remove caller-owned plaintext from any public-wrapper traceback frame.
        del plaintext
    assert pending is not None
    _raise_safely(
        pending,
        operation="encrypt",
        invalid_recipients=invalid_recipients,
        invalid_aad=invalid_aad,
    )


def _decrypt_for_recipient(
    bundle: EncryptedBundle,
    private_key: bytes,
    aad: bytes,
    recipient_key_id: str,
) -> bytes:
    value = EncryptedBundle.model_validate(bundle.model_dump(mode="python"))
    context = _validate_inputs(b"", aad)
    if type(private_key) is not bytes or len(private_key) != _KEY_BYTES:
        raise ValueError("invalid recipient private key")
    wrapped = {item.recipient_key_id: item for item in value.wrapped_keys}
    if tuple(wrapped) != context.recipient_key_ids:
        raise InvalidTag
    selected = wrapped.get(recipient_key_id)
    if selected is None:
        raise InvalidTag
    private = X25519PrivateKey.from_private_bytes(private_key)
    ephemeral = X25519PublicKey.from_public_bytes(
        _b64decode(value.ephemeral_public_key, expected_size=_KEY_BYTES)
    )
    shared = private.exchange(ephemeral)
    wrapping_key = _derive_wrapping_key(shared, aad, recipient_key_id)
    content_key = AESGCM(wrapping_key).decrypt(
        _b64decode(selected.nonce, expected_size=_NONCE_BYTES),
        _b64decode(selected.ciphertext),
        aad + b"\0" + recipient_key_id.encode(),
    )
    return AESGCM(content_key).decrypt(
        _b64decode(value.nonce, expected_size=_NONCE_BYTES),
        _b64decode(value.ciphertext),
        aad,
    )


def decrypt_bundle_for_recipient(
    bundle: EncryptedBundle,
    private_key: bytes,
    aad: bytes,
    recipient_key_id: str,
) -> bytes:
    """Decrypt one exact reviewed recipient entry for the restore trust boundary."""
    pending: BaseException | None = None
    try:
        return _decrypt_for_recipient(bundle, private_key, aad, recipient_key_id)
    except BaseException as error:  # noqa: BLE001 - scrub and preserve cancellation
        error.__traceback__ = None
        pending = error
    finally:
        del private_key
    assert pending is not None
    _raise_safely(pending, operation="decrypt")


def _decrypt_any_recipient(bundle: EncryptedBundle, private_key: bytes, aad: bytes) -> bytes:
    value = EncryptedBundle.model_validate(bundle.model_dump(mode="python"))
    context = _validate_inputs(b"", aad)
    if type(private_key) is not bytes or len(private_key) != _KEY_BYTES:
        raise ValueError("invalid recipient private key")
    if tuple(item.recipient_key_id for item in value.wrapped_keys) != context.recipient_key_ids:
        raise InvalidTag
    for item in value.wrapped_keys:
        try:
            return _decrypt_for_recipient(value, private_key, aad, item.recipient_key_id)
        except (InvalidTag, ValueError):
            continue
    raise InvalidTag


def decrypt_bundle(bundle: EncryptedBundle, private_key: bytes, aad: bytes) -> bytes:
    """Decrypt for the single matching recipient without revealing key identity."""
    pending: BaseException | None = None
    try:
        return _decrypt_any_recipient(bundle, private_key, aad)
    except BaseException as error:  # noqa: BLE001 - scrub and preserve cancellation
        error.__traceback__ = None
        pending = error
    finally:
        # Remove caller-owned private material from any public-wrapper traceback frame.
        del private_key
    assert pending is not None
    _raise_safely(pending, operation="decrypt")


__all__ = [
    "ALGORITHM",
    "AuthenticatedBundleContext",
    "AuthenticatedBundleContextV2",
    "EncryptedBundle",
    "EncryptedStateBundle",
    "WrappedContentKey",
    "canonical_authenticated_context_bytes",
    "canonical_encrypted_bundle_bytes",
    "decrypt_bundle",
    "decrypt_bundle_for_recipient",
    "encrypt_bundle",
    "recipient_possession_proof",
    "verify_recipient_possession_proof",
]
